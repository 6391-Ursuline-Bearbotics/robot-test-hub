"""Opt-in genuine synthetic WPILOG + encoded footage + actual HTTP qualification.

No robot/camera connection. The recordings are generated, not real-world evidence.
"""
import hashlib
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from robot_test_hub.collector import RobotStatus
from robot_test_hub.config import Config
from robot_test_hub.importer import Importer
from robot_test_hub.runs import rebuild_from_imports
from robot_test_hub.server import create_http_server
from robot_test_hub.service import HubService
from robot_test_hub.video_media import MediaToolsConfig
from robot_test_hub.wpilog import extract, PROFILE
from test_video_media_native import FFMPEG, FFPROBE, VERSION, generated_original, digest


class QuietSource:
    source_type = 'synthetic_incident_qualification'

    def status(self):
        return RobotStatus(True, time.monotonic(), 'generated-status-boot', 1, False)

    def list_closed_files(self):
        raise AssertionError('Enabled qualification forbids bulk source calls')

    def read(self, *args, **kwargs):
        raise AssertionError('Original downloads must use local archived bytes')


@unittest.skipUnless(FFMPEG and FFPROBE, 'Explicit opt-in FFmpeg/FFprobe paths required')
class IncidentLogIntegrationTests(unittest.TestCase):
    def test_pinned_run_original_and_clip_survive_catalog_change_and_tools_disabled_restart(self):
        self._qualify_original('SYNTHETIC')

    def test_manually_imported_original_retains_source_and_pins_through_restart(self):
        self._qualify_original('MANUAL_LOCAL')

    def _qualify_original(self, source_type):
        fixture = Path(__file__).parent/'fixtures'/'synthetic'/'alpha7-main.wpilog'
        raw = fixture.read_bytes()
        expected_sha = hashlib.sha256(raw).hexdigest()
        tools = MediaToolsConfig(FFMPEG,FFPROBE,VERSION,digest(FFMPEG),digest(FFPROBE),
            operation_timeout=20,minimum_free_bytes=1048576)
        with tempfile.TemporaryDirectory(prefix='hub-incident-log-native-') as directory:
            root = Path(directory)/'hub'
            folder = root/'video'/'generated'/'practice'/'capture'
            folder.mkdir(parents=True)
            original, frames = generated_original(folder)
            source_sha = digest(original)
            manifest = dict(schema_version=1, segment_id='capture-000000', camera_id='generated',
                session_id='practice', capture_id='capture', relative_path=original.name,
                source_type='SYNTHETIC', state='closed_verified', sha256=source_sha,
                size_bytes=original.stat().st_size, container='matroska', codec='ffv1', time_base='1/1000',
                start_pts_ns=str(frames[0][0]), end_pts_ns=str(sum(frames[-1])),
                frames=[dict(pts_ns=str(p), duration_ns=str(d)) for p,d in frames],
                utc_basis='unmapped_recording_pts', utc_uncertainty_ns=None, provenance={})
            original.with_name(original.name+'.json').write_text(json.dumps(manifest), encoding='utf-8')
            service = server = thread = None

            def start(config):
                nonlocal service, server, thread
                source = QuietSource()
                service = HubService(Config(data_dir=str(root)), source, video_media_config=config)
                service.start()
                server = create_http_server(service, source, 0)
                thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval':.01})
                thread.start()

            def close():
                nonlocal service, server, thread
                if server is not None:
                    server.shutdown(); server.server_close(); thread.join(3)
                    self.assertFalse(thread.is_alive())
                    server = None
                if service is not None:
                    self.assertTrue(service.close())
                    service = None

            def request(method, path, payload=None, headers=None):
                connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)
                try:
                    connection.request(method, path, None if payload is None else json.dumps(payload),
                        headers=headers or {'Content-Type':'application/json','X-Hub-Request':'1'})
                    response = connection.getresponse()
                    return response.status, dict(response.getheaders()), response.read()
                finally:
                    connection.close()

            def wait(path, predicate):
                deadline = time.monotonic()+15
                while time.monotonic()<deadline:
                    status, _, body = request('GET', path)
                    if status == 200 and predicate(json.loads(body)):
                        return json.loads(body)
                    time.sleep(.03)
                self.fail('Generated incident qualification did not reach expected state: '+body.decode())

            try:
                start(tools)
                wait('/api/v1/video/media-tools', lambda data:data['state']=='ready')
                wait('/api/v1/video', lambda data:data['recovery']['verified_segments']==1)
                with service.settings_lock:
                    importer = Importer(root, service.settings_db)
                    job = importer.import_file(fixture, source_type=source_type)
                    self.assertEqual(job['state'], 'succeeded')
                    catalog = rebuild_from_imports(root, service.settings_db)
                selected_run = next(run for run in catalog['runs'] if run['start_monotonic_ns']=='1020000123')
                self.assertEqual(selected_run['segment_ids'], [job['id']])

                def anchor(index, robot):
                    return dict(robot_ns=str(robot), segment_id='capture-000000', source_sha256=source_sha,
                        frame_index=index, robot_uncertainty_ns='0', video_uncertainty_ns='0',
                        evidence_label='Generated fixture clock assumption; not physical synchronization')

                calibration = dict(alignment_id='incident-log-native', revision=1, expected_previous_sha256=None,
                    robot_id=selected_run['robot_id'], boot_id=selected_run['boot_id'], camera_id='generated',
                    session_id='practice', capture_id='capture', segment_ids=['capture-000000'],
                    windows=[dict(continuity_id='generated-continuous', start_robot_ns='1000000000',
                        end_robot_ns='1900000000', segment_ids=['capture-000000'], model_uncertainty_ns='0',
                        anchors=[anchor(0,1000000000),anchor(7,1900000000)],
                        assumed_scale=None, assumed_drift_uncertainty_ppm=None)])
                status, _, body = request('POST', '/api/v1/video/alignments', calibration)
                self.assertEqual(status, 200, body)
                alignment = json.loads(body)
                selection = dict(kind='context', candidate_index=0, alignment_id=alignment['alignment_id'],
                    revision=alignment['revision'], sha256=alignment['sha256'], catalog_revision=catalog['revision'],
                    context=dict(kind='run', run_id=selected_run['run_id']))
                status, _, body = request('POST', '/api/v1/video/preservations',
                    dict(request_id='incident-log-native-request', selection=selection))
                self.assertEqual(status, 200, body)
                ready = wait('/api/v1/video/preservations/incident-log-native-request',
                    lambda data:data['state'] in ('ready','failed'))
                self.assertEqual(ready['state'], 'ready', ready)
                self.assertEqual(len(ready['items']), 1)
                item = ready['items'][0]
                route = '/api/v1/video/media/'+item['item_id']
                status, _, sidecar = request('GET', route+'/sidecar')
                self.assertEqual(status, 200)
                self.assertEqual(hashlib.sha256(sidecar).hexdigest(), item['sidecar_sha256'])

                def check_download():
                    status, _, body = request('GET', route+'/logs')
                    self.assertEqual(status, 200, body)
                    references = json.loads(body)
                    self.assertEqual(references['basis'], 'saved_run_import_job_references')
                    self.assertEqual(len(references['items']), 1)
                    ref = references['items'][0]
                    self.assertEqual((ref['import_job_id'],ref['sha256'],ref['size_bytes']),
                        (job['id'],expected_sha,len(raw)))
                    self.assertEqual(ref['state'], 'download_candidate')
                    self.assertEqual(ref['source_type'],source_type)
                    self.assertFalse(ref['original_bytes_reverified_for_export'])
                    log_route = route+'/logs/'+job['id']
                    status, headers, body = request('GET', log_route)
                    self.assertEqual(status, 200, body)
                    self.assertEqual(body, raw)
                    self.assertIn(expected_sha+'.wpilog', headers['Content-Disposition'])
                    self.assertEqual(headers['Content-Type'], 'application/octet-stream')
                    self.assertEqual(request('HEAD',log_route)[2], b'')
                    status, headers, partial = request('GET',log_route,headers={'Range':'bytes=0-31'})
                    self.assertEqual(status, 206)
                    self.assertEqual(partial, raw[:32])
                    self.assertEqual(headers['Content-Range'],f'bytes 0-31/{len(raw)}')
                    downloaded = Path(directory)/'downloaded.wpilog'
                    downloaded.write_bytes(body)
                    cycles = [row for row in extract(downloaded,PROFILE) if row['kind']=='cycle']
                    self.assertEqual(len(cycles), 7)
                    # Download is the whole immutable recording, not a fabricated run-only rewrite.
                    self.assertEqual([int(row['timestamp_ns']) for row in cycles],
                        [1000000000,1020000123,1040000000,1060000000,1080000000,1500000000,1520000000])
                    self.assertNotIn(str(root), json.dumps(references))

                check_download()
                with service.settings_lock:
                    other = Importer(root,service.settings_db).import_file(
                        fixture.with_name('alpha7-boot-b.wpilog'),source_type=source_type)
                    newer = rebuild_from_imports(root,service.settings_db)
                self.assertNotEqual(newer['revision'],catalog['revision'])
                check_download()
                self.assertEqual(request('GET',route+'/logs/'+other['id'])[0],404)
                self.assertEqual(request('GET',route+'/sidecar')[2],sidecar)
                self.assertEqual(digest(original),source_sha)
                self.assertIsNone(service.video.recorder)
                close(); start(None)
                recovered = wait('/api/v1/video/preservations/incident-log-native-request',
                    lambda data:data['state']=='ready')
                self.assertEqual(recovered,ready)
                check_download()
                self.assertEqual(request('GET',route+'/sidecar')[2],sidecar)
                self.assertEqual(request('GET',route)[0],200)
                self.assertIsNone(service.video.recorder)
                self.assertEqual(fixture.read_bytes(),raw)
            finally:
                close()


if __name__ == '__main__':
    unittest.main()
