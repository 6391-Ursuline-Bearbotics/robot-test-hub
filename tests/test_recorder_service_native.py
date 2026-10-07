"""Opt-in real encoded lavfi footage through service/HTTP; no hardware endpoints."""
import hashlib
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import quote

from robot_test_hub.collector import RobotStatus
from robot_test_hub.config import Config
from robot_test_hub.recorder import FFmpegConfig
from robot_test_hub.server import create_http_server
from robot_test_hub.service import HubService
from robot_test_hub.storage import OwnershipError
from test_notebook import annotation


FFMPEG=os.environ.get('ROBOT_HUB_FFMPEG')
FFPROBE=os.environ.get('ROBOT_HUB_FFPROBE')
VERSION=os.environ.get('ROBOT_HUB_FFMPEG_VERSION','9.0.2-essentials_build-www.gyan.dev')


class SyntheticUnavailableSource:
    source_type='synthetic_permission_regression'

    def __init__(self):
        self.lock=threading.Lock()
        self.mode='enabled'
        self.generation=1
        self.bulk_calls=0

    def change(self, mode):
        with self.lock:
            self.mode=mode
            self.generation+=1

    def status(self):
        with self.lock:
            mode,generation=self.mode,self.generation
        if mode=='enabled':
            return RobotStatus(True,time.monotonic(),'synthetic-boot',generation,False)
        if mode=='stale':
            return RobotStatus(False,time.monotonic()-10,'synthetic-boot',generation,True)
        return RobotStatus(None,0,'synthetic-boot',generation,False)

    def list_closed_files(self):
        self.bulk_calls+=1
        raise AssertionError('Unavailable synthetic permission must prevent bulk discovery')

    def read(self,*args,**kwargs):
        self.bulk_calls+=1
        raise AssertionError('Unavailable synthetic permission must prevent bulk reads')


@unittest.skipUnless(FFMPEG and FFPROBE,'Explicit opt-in FFmpeg and FFprobe paths required')
class NativeRecorderServiceTests(unittest.TestCase):
    def wait_for(self,predicate,timeout=15):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            if predicate():return
            time.sleep(.02)
        self.fail('Generated-media service condition exceeded bounded deadline')

    def test_continuous_generated_recording_http_pause_shutdown_and_archive_restart(self):
        for executable in (FFMPEG,FFPROBE):
            self.assertTrue(Path(executable).is_absolute())
            self.assertTrue(Path(executable).is_file())
        with tempfile.TemporaryDirectory(prefix='robot-hub-video-service-') as directory:
            root=Path(directory)/'hub'
            source=SyntheticUnavailableSource()
            hub_config=Config(data_dir=str(root),idle_delay=.1,freshness=.5,shutdown_timeout=15)
            private_input="testsrc2=size=32x32:rate=10,select='not(eq(mod(n,10),2)+eq(mod(n,10),6))'"
            video_config=FFmpegConfig(FFMPEG,FFPROBE,VERSION,'generated-overview','lavfi',private_input,
                'SYNTHETIC',segment_seconds=1,stalled_seconds=10,minimum_free_bytes=1024*1024,operation_timeout=10)
            service=HubService(hub_config,source,video_config=video_config)
            httpd=create_http_server(service,source,0)
            thread=threading.Thread(target=httpd.serve_forever,kwargs={'poll_interval':.01})
            thread.start()
            restarted=None
            restart_http=None
            restart_thread=None

            def request(server,method,path,payload=None):
                connection=http.client.HTTPConnection('127.0.0.1',server.server_address[1],timeout=2)
                before=time.monotonic()
                try:
                    headers={'Content-Type':'application/json','X-Hub-Request':'1'} if payload is not None else {}
                    connection.request(method,path,json.dumps(payload) if payload is not None else None,headers)
                    response=connection.getresponse()
                    body=response.read()
                    self.assertLess(time.monotonic()-before,2,'Recording delayed local HTTP beyond its request deadline')
                    return response.status,json.loads(body),body
                finally:
                    connection.close()

            def get(server,path):
                return request(server,'GET',path)

            def check_redacted(health,page):
                encoded=json.dumps(health)+json.dumps(page)
                for forbidden in (str(root),private_input,FFMPEG,FFPROBE,'camera_input','relative_path','provenance'):
                    self.assertNotIn(forbidden,encoded)
                for segment in page['items']:
                    self.assertNotIn('frames',segment)
                    self.assertNotIn('relative_path',segment)
                    self.assertEqual(segment['source_type'],'SYNTHETIC')
                    self.assertEqual(segment['backup_state'],'not_backed_up')
                self.assertFalse(health['live_camera_qualified'])
                self.assertEqual(health['backup_state'],'not_backed_up')

            def all_pages(server):
                items=[];cursor=None
                for unused in range(100):
                    url='/api/v1/video/segments?limit=1'+('&cursor='+quote(cursor,safe='') if cursor else '')
                    status,page,encoded=get(server,url)
                    self.assertEqual(status,200)
                    self.assertLessEqual(len(page['items']),1)
                    self.assertLess(len(encoded),16384)
                    items.extend(page['items'])
                    cursor=page['next_cursor']
                    if cursor is None:return items,page
                self.fail('Generated fixture exceeded bounded pagination')

            try:
                service.start()
                self.wait_for(lambda:service.video.snapshot()['state']=='recording' and
                              service.video.snapshot()['closed_segments']>=1)
                with self.assertRaises(OwnershipError):
                    HubService(hub_config,SyntheticUnavailableSource(),video_config=video_config)
                original_capture=service.video.snapshot()['capture_id']
                # The real encoder continues advancing in all three prohibited
                # robot status modes. Notebook writes/readback remain independent.
                for mode in ('enabled','disconnected','stale'):
                    source.change(mode)
                    self.wait_for(lambda:service.snapshot()['source_status']['enabled'] is True
                                  if mode=='enabled' else not service.snapshot()['source_status']['fresh'])
                    before=service.video.snapshot()['frames'] or 0
                    note=annotation('generated-'+mode+'-note')
                    note['text']='Generated-media service observation while '+mode
                    status,saved,unused=request(httpd,'POST','/api/v1/annotations',note)
                    self.assertEqual(status,201)
                    self.assertEqual(saved['annotation']['storage_state'],'saved_in_hub')
                    status,readback,unused=get(httpd,'/api/v1/annotations/'+note['event_id'])
                    self.assertEqual(status,200)
                    self.assertEqual(readback['annotation']['event_utc_start_ns'],note['event_utc_start_ns'])
                    self.wait_for(lambda:(service.video.snapshot()['frames'] or 0)>before)
                    self.assertEqual(service.video.snapshot()['state'],'recording')
                    self.assertEqual(source.bulk_calls,0)
                    self.assertIn(service.snapshot()['state'],('paused','starting'))
                    status,health,unused=get(httpd,'/api/v1/video')
                    self.assertEqual(status,200)
                    status,page,unused=get(httpd,'/api/v1/video/segments?limit=1')
                    self.assertEqual(status,200)
                    check_redacted(health,page)
                service.set_paused(True)
                self.wait_for(lambda:service.snapshot()['paused_by_operator'])
                before=service.video.snapshot()['frames'] or 0
                self.wait_for(lambda:(service.video.snapshot()['frames'] or 0)>before)
                self.assertEqual(service.video.snapshot()['state'],'recording')
                for query in ('?limit=0','?limit=101','?limit=1&limit=2','?path=private','?cursor=../escape'):
                    self.assertEqual(get(httpd,'/api/v1/video/segments'+query)[0],400)
                self.assertEqual(get(httpd,'/api/v1/video?input=private')[0],400)
                old_folder=service.video.recorder.folder
                before_close=service.video.snapshot()['closed_segments']
                self.assertTrue(service.close())
                self.assertEqual(service.video.snapshot()['state'],'stopped')
                self.assertIsNone(service.video.recorder.owner)
                self.assertIsNotNone(service.video.recorder.adapter.process.poll())
                old_segments=service.video.recorder.list_segments()
                self.assertGreater(len(old_segments),before_close,'Service shutdown did not finalize the actual open video tail')
                old_ids={segment['segment_id'] for segment in old_segments}
                original_hashes={segment['relative_path']:segment['sha256'] for segment in old_segments}
                provenance=old_segments[0]['provenance']
                first=old_segments[0]
                first_frames=service.video.recorder.adapter.probe(old_folder/first['relative_path'])['frames']
                self.assertEqual([frame['pts_ns'] for frame in first_frames],
                                 [n*100_000_000 for n in (0,1,3,4,5,7,8,9)])
                self.assertTrue(json.loads((old_folder/'capture-lifecycle.json').read_bytes())['finalization_complete'])
                # A truncated prefix of genuine encoded media has no immutable
                # close manifest; recovery must preserve it without eligibility.
                orphan_folder=old_folder.parent/'generated-unfinished-capture'
                orphan_folder.mkdir()
                orphan=orphan_folder/'segment-000000.mkv'
                footage=(old_folder/first['relative_path']).read_bytes()
                orphan.write_bytes(footage[:max(1,len(footage)//2)])
                orphan_hash=hashlib.sha256(orphan.read_bytes()).hexdigest()
                httpd.shutdown();thread.join(2);httpd.server_close()
                restarted=HubService(hub_config,source,video_config=video_config)
                restart_http=create_http_server(restarted,source,0)
                restart_thread=threading.Thread(target=restart_http.serve_forever,kwargs={'poll_interval':.01})
                restart_thread.start();restarted.start()
                self.wait_for(lambda:restarted.video.snapshot()['state']=='recording' and
                              restarted.video.snapshot()['active_closed_segments']>=1)
                recovered=restarted.video.snapshot()
                self.assertNotEqual(recovered['capture_id'],original_capture)
                self.assertEqual(recovered['recovery']['verified_segments'],len(old_segments))
                self.assertEqual(recovered['recovery']['unverified_files'],1)
                self.assertEqual(recovered['recovery']['unfinished_captures'],1)
                self.assertEqual(recovered['recovery']['state'],'partial')
                self.assertTrue(restarted.snapshot()['paused_by_operator'])
                self.assertEqual(source.bulk_calls,0)
                items,page=all_pages(restart_http)
                self.assertTrue(old_ids.issubset({item['segment_id'] for item in items}))
                self.assertTrue(all(item['capture_state']=='stopped' for item in items if item['segment_id'] in old_ids))
                self.assertFalse(any(item['capture_id']=='generated-unfinished-capture' for item in items))
                status,health,unused=get(restart_http,'/api/v1/video')
                self.assertEqual(status,200);check_redacted(health,dict(page,items=items))
                for name,digest in original_hashes.items():
                    self.assertEqual(hashlib.sha256((old_folder/name).read_bytes()).hexdigest(),digest)
                self.assertEqual(hashlib.sha256(orphan.read_bytes()).hexdigest(),orphan_hash)
                self.assertTrue(restarted.close())
                self.assertIsNotNone(restarted.video.recorder.adapter.process.poll())
                self.assertEqual(provenance['version'],VERSION)
                print('GENERATED_RECORDER_SERVICE_QUALIFICATION '+json.dumps({
                    'profile':'lavfi-10hz-drop-2-6-service-http-restart-v1','provenance':provenance,
                    'closed_before_shutdown':before_close,'recovered_verified_segments':len(old_segments),
                    'unverified_files':1,'status_modes':['enabled','disconnected','stale','operator_pause'],
                    'bulk_source_calls':source.bulk_calls,'first_original_pts_ns':[frame['pts_ns'] for frame in first_frames],
                    'native_generated_media_only':True,'live_camera_qualified':False},sort_keys=True))
            finally:
                service.close()
                if restarted is not None:restarted.close()
                for server,worker in ((httpd,thread),(restart_http,restart_thread)):
                    if server is not None:
                        if worker is not None and worker.is_alive():
                            server.shutdown();worker.join(2)
                        server.server_close()


if __name__=='__main__':
    unittest.main()
