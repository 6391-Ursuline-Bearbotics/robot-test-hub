"""Independent actual HTTP acceptance; synthetic bytes do not qualify cameras."""
import copy
import hashlib
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from robot_test_hub.config import Config
from robot_test_hub.demo import DemoSource
from robot_test_hub.notebook import Notebook
from robot_test_hub.server import create_http_server
from robot_test_hub.service import HubService
from test_notebook import annotation


class InvestigationHTTPReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve() / 'archive'
        self.base = 9007199254740993
        self.capture = 'reviewcapture'
        self.segment_id = self.capture + '-000000'
        folder = self.root / 'video' / 'reviewcamera' / 'practice' / self.capture
        folder.mkdir(parents=True)
        self.raw = folder / 'segment-000000.mkv'
        self.raw.write_bytes(b'INDEPENDENT SYNTHETIC HTTP EVIDENCE')
        self.original = self.raw.read_bytes()
        self.manifest_path = self.raw.with_name(self.raw.name + '.json')
        self.manifest = dict(schema_version=1, segment_id=self.segment_id, camera_id='reviewcamera',
            session_id='practice', capture_id=self.capture, relative_path=self.raw.name,
            source_type='SYNTHETIC', state='closed_verified',
            sha256=hashlib.sha256(self.original).hexdigest(), size_bytes=len(self.original),
            container='matroska', codec='protocol_fixture', time_base='1/1000000000',
            start_pts_ns='0', end_pts_ns='60', frames=[dict(pts_ns=str(a), duration_ns=str(b))
                for a,b in ((0,10),(10,10),(40,10),(50,10))], utc_basis='unmapped_recording_pts',
            utc_uncertainty_ns=None, provenance={'private_input':'secret-credential-path'})
        self.manifest_path.write_text(json.dumps(self.manifest), encoding='utf-8')
        self.source = DemoSource()
        self.service = HubService(Config(data_dir=str(self.root)), self.source)
        self.start_http()
        self.addCleanup(self.cleanup)

    def start_http(self):
        self.service.video.recover()
        self.assertEqual(self.service.video.recovery['verified_segments'], 1)
        self.server = create_http_server(self.service, self.source, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval':.01})
        self.thread.start()

    def stop_http(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        self.assertFalse(self.thread.is_alive())

    def cleanup(self):
        self.stop_http()
        self.assertTrue(self.service.close())
        self.temp.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        try:
            connection.request(method, path, None if body is None else json.dumps(body),
                headers=headers if headers is not None else {'Content-Type':'application/json', 'X-Hub-Request':'1'})
            response = connection.getresponse()
            encoded = response.read()
            value = json.loads(encoded)
            self.assertNotIn(str(self.root), encoded.decode())
            self.assertNotIn('secret-credential', encoded.decode())
            return response.status, value
        finally:
            connection.close()

    def calibration(self):
        anchors = [dict(robot_ns=str(self.base+t), segment_id=self.segment_id,
            source_sha256=self.manifest['sha256'], frame_index=index, robot_uncertainty_ns='0',
            video_uncertainty_ns='0', evidence_label='independently selected fixture cue')
            for index,t in ((0,0),(3,50))]
        return dict(alignment_id='reviewmapping', revision=1, expected_previous_sha256=None,
            robot_id='reviewrobot', boot_id='reviewboot', camera_id='reviewcamera', session_id='practice',
            capture_id=self.capture, segment_ids=[self.segment_id], windows=[dict(continuity_id='reviewwindow',
                start_robot_ns=str(self.base), end_robot_ns=str(self.base+50), segment_ids=[self.segment_id],
                model_uncertainty_ns='0', anchors=anchors, assumed_scale=None, assumed_drift_uncertainty_ppm=None)])

    def save(self):
        status, receipt = self.request('POST','/api/v1/video/alignments',self.calibration())
        self.assertEqual(status,200)
        return receipt

    def mapping(self, receipt, start=5, end=45, **changes):
        payload = dict(alignment_id='reviewmapping', revision=receipt['revision'], sha256=receipt['sha256'],
            robot_id='reviewrobot', boot_id='reviewboot', start_robot_ns=str(self.base+start),
            end_robot_ns=str(self.base+end), event_id=None, note_revision=None)
        payload.update(changes)
        return self.request('POST','/api/v1/video/map',payload)

    def test_http_save_retry_restart_and_actual_gap_selection(self):
        receipt = self.save()
        self.assertEqual(self.save(),receipt)
        self.stop_http()
        self.assertTrue(self.service.close())
        self.service = HubService(Config(data_dir=str(self.root)),self.source)
        self.start_http()
        status, loaded = self.request('GET','/api/v1/video/alignments/reviewmapping?revision=1&sha256='+receipt['sha256'])
        self.assertEqual(status,200)
        self.assertEqual(loaded,receipt)
        status, result = self.mapping(receipt)
        self.assertEqual(status,200)
        self.assertEqual(result['result']['state'],'partial')
        self.assertEqual(result['result']['spans'][0]['frame_indexes'],[0,1,2])
        self.assertEqual(result['result']['windows'][0]['unavailable_pts_intervals'],[['20','40']])
        self.assertEqual(result['result']['windows'][0]['start_robot_ns'],str(self.base+5))
        self.assertFalse(result['result']['measured_camera_alignment'])
        self.assertEqual(result['time_basis'],'explicit_manual_robot_interval')
        self.assertEqual(self.mapping(receipt,25,25)[1]['result']['state'],'gap')
        self.assertEqual(self.mapping(receipt,boot_id='wrongboot')[1]['result']['state'],'unavailable')
        self.assertEqual(self.raw.read_bytes(),self.original)

    def test_exact_saved_note_revision_is_context_without_time_inference(self):
        book = Notebook(self.root/'catalog.sqlite3')
        first = annotation('reviewnote')
        book.save(first)
        second = dict(first,revision=2,text='changed later context')
        book.save(second,expected_previous_revision=1)
        status, result = self.mapping(self.save(),event_id='reviewnote',note_revision=1)
        self.assertEqual(status,200)
        self.assertEqual(result['annotation']['revision'],1)
        self.assertEqual(result['annotation']['text'],first['text'])
        self.assertEqual(result['result']['windows'][0]['start_robot_ns'],str(self.base+5))
        self.assertEqual(result['time_basis'],'explicit_manual_robot_interval')

    def test_each_mapping_rechecks_raw_bytes_and_canonical_manifest(self):
        receipt = self.save()
        self.raw.write_bytes(b'X'*len(self.original))
        self.assertEqual(self.mapping(receipt)[0],409)
        self.raw.write_bytes(self.original)
        altered = copy.deepcopy(self.manifest)
        altered['frames'][1] = dict(pts_ns='12',duration_ns='8')
        self.manifest_path.write_text(json.dumps(altered),encoding='utf-8')
        self.assertEqual(self.mapping(receipt)[0],409)
        self.manifest_path.write_text(json.dumps(self.manifest),encoding='utf-8')
        self.assertEqual(self.mapping(receipt)[0],200)
        self.manifest_path.write_text('[]',encoding='utf-8')
        self.assertEqual(self.mapping(receipt)[0],409)

    def test_failed_publication_is_unconfirmed_redacted_and_retryable(self):
        with patch('robot_test_hub.video_alignment.os.link',side_effect=OSError('secret-credential disk path')):
            status, failure = self.request('POST','/api/v1/video/alignments',self.calibration())
        self.assertEqual(status,503)
        self.assertEqual(list((self.root/'video-alignments').glob('*.json')),[])
        receipt = self.save()
        changed = self.calibration()
        changed['windows'][0]['model_uncertainty_ns']='1'
        self.assertEqual(self.request('POST','/api/v1/video/alignments',changed)[0],409)
        self.assertEqual(self.save(),receipt)

    def test_http_controls_ns_validation_and_stopping(self):
        self.assertEqual(self.request('POST','/api/v1/video/alignments',self.calibration(),
            headers={'Content-Type':'application/json'})[0],403)
        self.assertEqual(self.request('POST','/api/v1/video/alignments',self.calibration(),
            headers={'Content-Type':'application/json','X-Hub-Request':'1','Origin':'http://remote.invalid'})[0],403)
        receipt = self.save()
        self.assertEqual(self.mapping(receipt,start_robot_ns=float(self.base))[0],400)
        self.assertEqual(self.request('GET','/api/v1/video/frames?segment_id=..%2Fprivate')[0],400)
        self.service.stop.set()
        self.assertEqual(self.mapping(receipt)[0],503)
        self.assertEqual(self.request('POST','/api/v1/video/alignments',self.calibration())[0],503)


if __name__=='__main__':
    unittest.main()

