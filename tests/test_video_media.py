"""Protocol and lifetime tests use synthetic bytes and an explicit fake codec adapter."""
import copy
from contextlib import nullcontext
from dataclasses import replace
import hashlib
import http.client
import json
import io
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from robot_test_hub.server import create_http_server
from robot_test_hub.video_media import MediaWorker,MediaToolsConfig,MediaError,NativeMediaAdapter,verify_frames
import test_video_investigation as fixture


class Adapter:
    def __init__(self,config):self.rendered=[];self.frames={};self.bad=False
    def validate(self,stop):pass
    def render(self,source,first,last,output,stop):
        self.rendered.append((first,last));output.write_bytes(b'SYNTHETIC DERIVATIVE '+str((first,last)).encode())
        self.frames[str(output)]=[(0,10),(10,10)][:last-first+1]
    def probe(self,path,stop):
        if self.bad:return [(0,9)]
        if str(path) in self.frames:return self.frames[str(path)]
        raw=path.read_bytes()
        return [(0,10),(10,10)] if b'(0, 1)' in raw else [(0,10)]


class MediaTests(unittest.TestCase):
    setUp=fixture.VideoInvestigationTests.setUp
    segment=fixture.VideoInvestigationTests.segment
    payload=fixture.VideoInvestigationTests.payload
    query=fixture.VideoInvestigationTests.query

    def config(self):
        return MediaToolsConfig(str((self.root/'ffmpeg.exe').resolve()),str((self.root/'ffprobe.exe').resolve()),
            'explicit-fixture','0'*64,'1'*64,minimum_free_bytes=0)

    def worker(self):
        worker=MediaWorker(self.service,self.config(),adapter_factory=Adapter)
        self.service.media=worker
        return worker

    def request(self):
        receipt=self.backend.create(self.payload())
        return dict(request_id='test-preservation',selection=dict(kind='interval',candidate_index=0,**self.query(receipt)))

    def test_exact_pin_idempotency_gap_split_and_download(self):
        worker=self.worker();request=self.request()
        receipt=worker.submit(request)
        self.assertEqual(receipt['state'],'queued')
        self.assertTrue(worker.submit(request)['idempotent'])
        altered=copy.deepcopy(request);altered['selection']['end_robot_ns']=str(self.base+46)
        with self.assertRaises(MediaError):worker.submit(altered)
        worker.process(worker.jobs[request['request_id']],self.service.stop)
        result=worker.get(request['request_id'])
        self.assertEqual(result['state'],'ready');self.assertEqual(worker.adapter.rendered,[(0,1),(2,2)])
        self.assertEqual([x['frame_count'] for x in result['items']],[2,1])
        side=worker.folder/(result['items'][0]['item_id']+'.json')
        document=json.loads(side.read_bytes())
        self.assertEqual(document['frames'],[
            dict(source_frame_index=0,source_pts_ns='0',duration_ns='10',derived_pts_ns='0'),
            dict(source_frame_index=1,source_pts_ns='10',duration_ns='10',derived_pts_ns='10')])
        self.assertFalse(document['manual_advantagescope']['automatic_offset_supported'])
        self.assertIn('unavailable_pts_intervals',json.dumps(document))
        self.assertNotIn(str(self.root),json.dumps(document))
        self.assertNotIn('secret-camera-path',json.dumps(document))
        before=hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.assertEqual(before,self.manifest['sha256'])
        stream,size=worker.open_item(result['items'][0]['item_id']);self.assertEqual(len(stream.read()),size);stream.close()
        worker.process(worker.jobs[request['request_id']],self.service.stop)
        self.assertEqual(len(worker.adapter.rendered),2)

    def test_frame_count_pts_and_final_duration_all_required(self):
        expected=[dict(derived_pts_ns='0',duration_ns='10'),dict(derived_pts_ns='10',duration_ns='15')]
        verify_frames([(0,10),(10,15)],expected)
        for actual in ([(0,10)],[(0,10),(11,15)],[(0,10),(10,10)]):
            with self.subTest(actual=actual),self.assertRaises(MediaError):verify_frames(actual,expected)

    def test_native_timeout_and_cancellation_reap_child_and_reader(self):
        class Process:
            def __init__(self):self.stdout=io.BytesIO(b'');self.returncode=None;self.killed=False
            def poll(self):return self.returncode
            def kill(self):self.killed=True;self.returncode=-9
            def wait(self,timeout=None):return self.returncode
        adapter=NativeMediaAdapter(self.config())
        process=Process();stop=threading.Event()
        with patch('robot_test_hub.video_media.subprocess.Popen',return_value=process),\
                patch('robot_test_hub.video_media.time.monotonic',side_effect=[0,601]):
            with self.assertRaises(MediaError) as raised:adapter._run(['fixture'],stop)
        self.assertEqual(raised.exception.code,'media_timeout');self.assertTrue(process.killed);self.assertTrue(process.stdout.closed)
        process=Process()
        def poll():stop.set();return process.returncode
        process.poll=poll
        with patch('robot_test_hub.video_media.subprocess.Popen',return_value=process):
            with self.assertRaises(MediaError) as raised:adapter._run(['fixture'],stop)
        self.assertEqual(raised.exception.code,'media_interrupted');self.assertTrue(process.killed);self.assertTrue(process.stdout.closed)

    def test_failed_attempt_restart_does_not_encode_again(self):
        worker=self.worker();request=self.request();worker.submit(request)
        worker._terminal(worker.jobs[request['request_id']],'failed','media_probe_mismatch')
        recovered=MediaWorker(self.service,self.config(),adapter_factory=Adapter)
        with self.service.video.lock:self.service.video.recovery['state']='complete'
        stop=threading.Event();thread=threading.Thread(target=recovered.run,args=(stop,));thread.start()
        deadline=time.monotonic()+2
        while not recovered.jobs and time.monotonic()<deadline:time.sleep(.01)
        stop.set();thread.join(2)
        self.assertEqual(recovered.get(request['request_id'])['state'],'failed')
        self.assertEqual(recovered.adapter.rendered,[])

    def test_probe_failure_preserves_working_and_never_publishes_ready(self):
        worker=self.worker();request=self.request();worker.submit(request);worker.adapter.bad=True
        with self.assertRaises(MediaError):worker.process(worker.jobs[request['request_id']],self.service.stop)
        self.assertEqual(worker.items,{})
        self.assertEqual(len(list(worker.folder.glob('.working-*.mp4'))),1)
        self.assertEqual(list(worker.folder.glob('[0-9a-f]'*64+'.mp4')),[])

    def test_changed_source_and_manifest_fail_before_encoding(self):
        worker=self.worker();request=self.request();worker.submit(request)
        self.path.write_bytes(b'ALTERED')
        with self.assertRaises(Exception):worker.process(worker.jobs[request['request_id']],self.service.stop)
        self.assertEqual(worker.adapter.rendered,[])

    def test_shutdown_retains_worker_ownership_while_codec_cleanup_blocks(self):
        worker=self.worker();request=self.request();worker.submit(request)
        entered=threading.Event();release=threading.Event()
        def blocked(source,first,last,output,stop):
            entered.set();release.wait();raise MediaError('media_interrupted',503)
        worker.adapter.render=blocked
        with self.service.video.lock:self.service.video.recovery['state']='complete'
        thread=threading.Thread(target=worker.run,args=(self.service.stop,));self.service.threads=[thread];thread.start()
        self.assertTrue(entered.wait(2))
        from dataclasses import replace
        from robot_test_hub.storage import DataRootOwner,OwnershipError
        self.service.config=replace(self.service.config,shutdown_timeout=.05)
        self.assertFalse(self.service.close())
        with self.assertRaises(OwnershipError):DataRootOwner(self.service.root)
        release.set();thread.join(2);self.assertFalse(thread.is_alive());self.assertTrue(self.service.close())
        persisted=json.loads((worker.folder/'state-test-preservation.json').read_bytes())
        self.assertEqual(persisted['state'],'interrupted')

    def test_cached_http_range_head_and_strict_post(self):
        worker=self.worker();request=self.request();worker.submit(request);worker.process(worker.jobs[request['request_id']],self.service.stop)
        server=create_http_server(self.service,self.service.source,0)
        thread=threading.Thread(target=server.serve_forever);thread.start()
        self.addCleanup(lambda:(server.shutdown(),thread.join(),server.server_close()))
        def request_http(method,path,body=None,headers=None):
            connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=2)
            connection.request(method,path,body,headers or {});response=connection.getresponse()
            result=response.status,dict(response.getheaders()),response.read();connection.close();return result
        item=worker.get(request['request_id'])['items'][0]
        url='/api/v1/video/media/'+item['item_id']
        status,headers,body=request_http('GET',url,headers={'Range':'bytes=1-4'})
        self.assertEqual(status,206);self.assertEqual(len(body),4)
        self.assertEqual(request_http('HEAD',url)[2],b'')
        self.assertEqual(request_http('GET',url,headers={'Range':'bytes=1-2,4-5'})[0],416)
        self.assertEqual(request_http('GET','/api/v1/video/media-tools')[0],200)
        self.assertEqual(request_http('POST','/api/v1/video/preservations',json.dumps(request),
            {'Content-Type':'application/json','X-Hub-Request':'1'})[0],200)
        self.assertEqual(request_http('POST','/api/v1/video/preservations',b'{"request_id":"a","request_id":"b"}',
            {'Content-Type':'application/json','X-Hub-Request':'1'})[0],400)
        output=worker.folder/(item['item_id']+'.mp4');output.write_bytes(b'changed')
        self.assertEqual(request_http('GET',url)[0],409)
        self.service.stop.set()
        for endpoint in (url,url+'/sidecar','/api/v1/video/media-tools','/api/v1/video/preservations',
                         '/api/v1/video/preservations/test-preservation'):
            self.assertEqual(request_http('GET',endpoint)[0],503)
        self.assertEqual(request_http('HEAD',url)[0],503)

    def test_disabled_tools_recover_pinned_verified_outputs_without_encoding(self):
        worker=self.worker();request=self.request();worker.submit(request);worker.process(worker.jobs[request['request_id']],self.service.stop)
        recovered=MediaWorker(self.service,None)
        with self.service.video.lock:self.service.video.recovery['state']='complete'
        stop=threading.Event();thread=threading.Thread(target=recovered.run,args=(stop,));thread.start()
        deadline=time.monotonic()+2
        while time.monotonic()<deadline:
            if recovered.jobs and recovered.get(request['request_id'])['state']=='ready':break
            time.sleep(.01)
        stop.set();thread.join(2)
        self.assertEqual(recovered.get(request['request_id'])['state'],'ready')
        self.assertEqual(recovered.tools_snapshot()['state'],'disabled')
        with self.assertRaises(MediaError):recovered.submit(request)

    def test_recovery_rejects_unprojected_tool_metadata(self):
        worker=self.worker();request=self.request();worker.submit(request);worker.process(worker.jobs[request['request_id']],self.service.stop)
        item=worker.get(request['request_id'])['items'][0];path=worker.folder/(item['item_id']+'.json')
        document=json.loads(path.read_bytes());document['tools']['private_input']='secret path'
        path.write_text(json.dumps(document))
        recovered=MediaWorker(self.service,None)
        recovered.jobs=copy.deepcopy(worker.jobs)
        job=recovered.jobs[request['request_id']]
        with self.assertRaises(Exception):recovered.process(job,self.service.stop)
        self.assertEqual(recovered.items,{})

    def test_recovery_bounds_sidecar_before_read(self):
        worker=self.worker();request=self.request();worker.submit(request);worker.process(worker.jobs[request['request_id']],self.service.stop)
        item=worker.get(request['request_id'])['items'][0];path=worker.folder/(item['item_id']+'.json')
        path.write_bytes(b' '*4194305)
        with self.assertRaises(MediaError) as raised:worker.process(worker.jobs[request['request_id']],self.service.stop)
        self.assertEqual(raised.exception.code,'media_sidecar_too_large')

    def test_transient_pipe_close_failure_retains_cleanup_until_retry(self):
        class Pipe(io.BytesIO):
            attempts=0
            def close(self):
                self.attempts+=1
                if self.attempts==1:raise OSError('secret native path')
                super().close()
        class Process:
            stdout=Pipe(b'');returncode=0
            def poll(self):return 0
        process=Process();adapter=NativeMediaAdapter(self.config())
        with patch('robot_test_hub.video_media.subprocess.Popen',return_value=process):
            self.assertEqual(adapter._run(['fixture'],threading.Event()),b'')
        self.assertEqual(process.stdout.attempts,2);self.assertTrue(process.stdout.closed)

    def test_reservation_blocks_concurrent_publication_and_directory_scan_is_bounded(self):
        worker=self.worker();request=self.request();worker.submit(request)
        worker.config=replace(worker.config,maximum_output_bytes=1048576,maximum_total_bytes=3145728)
        entered=threading.Event();release=threading.Event();original=worker.adapter.render;failure=[]
        def render(*args):entered.set();release.wait();return original(*args)
        worker.adapter.render=render
        def process():
            try:worker.process(worker.jobs[request['request_id']],self.service.stop)
            except BaseException as exc:failure.append(exc)
        thread=threading.Thread(target=process);thread.start()
        try:
            self.assertTrue(entered.wait(2));self.assertGreater(worker.reservation,1048576)
            with self.assertRaises(MediaError) as raised:worker._publish('extra.json',b'X'*2097152)
            self.assertEqual(raised.exception.code,'media_disk_limit');self.assertFalse((worker.folder/'extra.json').exists())
        finally:release.set();thread.join(2)
        self.assertEqual(failure,[]);self.assertEqual(worker.reservation,0)
        worker.config=replace(worker.config,maximum_jobs=1)
        path=worker.folder/'request-test-preservation.json'
        with patch('robot_test_hub.video_media.os.scandir',return_value=nullcontext(iter([SimpleNamespace(path=str(path))]*1203))):
            with self.assertRaises(MediaError) as raised:worker._quota()
        self.assertEqual(raised.exception.code,'media_disk_limit')


if __name__=='__main__':unittest.main()
