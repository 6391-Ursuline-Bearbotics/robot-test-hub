"""Independent synthetic protocol/HTTP review; no camera or native codec proof."""
import copy
import http.client
import json
import threading
import unittest
from unittest.mock import patch

from robot_test_hub.server import create_http_server
from robot_test_hub.video_media import MediaError, NativeMediaAdapter
import test_video_media as fixture


class MediaReviewTests(unittest.TestCase):
    setUp=fixture.MediaTests.setUp
    segment=fixture.MediaTests.segment
    payload=fixture.MediaTests.payload
    query=fixture.MediaTests.query
    config=fixture.MediaTests.config
    worker=fixture.MediaTests.worker
    request=fixture.MediaTests.request

    def http(self):
        server=create_http_server(self.service,self.service.source,0)
        thread=threading.Thread(target=server.serve_forever);thread.start()
        self.addCleanup(lambda:(server.shutdown(),thread.join(2),server.server_close()))
        def request(method,path,body=None,headers=None):
            connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=2)
            try:
                connection.request(method,path,body,headers or {})
                response=connection.getresponse()
                return response.status,response.read(),dict(response.getheaders())
            finally:connection.close()
        return request

    def ready(self):
        worker=self.worker();payload=self.request();worker.submit(payload)
        worker.process(worker.jobs[payload['request_id']],self.service.stop)
        return worker,payload,worker.get(payload['request_id'])['items'][0]

    def test_concurrent_same_identity_is_one_durable_request_and_conflict(self):
        worker=self.worker();payload=self.request();barrier=threading.Barrier(5)
        results=[];errors=[]
        def submit():
            try:barrier.wait();results.append(worker.submit(copy.deepcopy(payload)))
            except Exception as error:errors.append(error)
        threads=[threading.Thread(target=submit) for _ in range(4)]
        for thread in threads:thread.start()
        barrier.wait()
        for thread in threads:thread.join(2);self.assertFalse(thread.is_alive())
        self.assertEqual(errors,[]);self.assertEqual(len(results),4)
        self.assertEqual(sum(not r['idempotent'] for r in results),1)
        self.assertEqual(len(list(worker.folder.glob('request-*.json'))),1)
        self.assertEqual(len({r['request_sha256'] for r in results}),1)
        altered=copy.deepcopy(payload);altered['selection']['end_robot_ns']=str(self.base+44)
        request=self.http()
        status,body,_=request('POST','/api/v1/video/preservations',json.dumps(altered),
            {'Content-Type':'application/json','X-Hub-Request':'1'})
        self.assertEqual(status,409)
        self.assertNotIn(str(self.root).encode(),body)
        self.assertEqual(worker.adapter.rendered,[])

    def test_media_cached_and_stream_endpoints_reject_closed_root(self):
        worker,payload,item=self.ready();request=self.http()
        self.assertTrue(self.service.close())
        for path in ('/api/v1/video/media-tools','/api/v1/video/preservations',
                     '/api/v1/video/preservations/'+payload['request_id'],
                     '/api/v1/video/media/'+item['item_id'],
                     '/api/v1/video/media/'+item['item_id']+'/sidecar'):
            with self.subTest(path=path),patch.object(worker,'open_item',side_effect=AssertionError('released root accessed')):
                status,_,_=request('GET',path);self.assertEqual(status,503)
        self.assertEqual(request('HEAD','/api/v1/video/media/'+item['item_id'])[0],503)

    def test_same_size_derivative_tamper_rejected_by_actual_http(self):
        worker,payload,item=self.ready();request=self.http()
        url='/api/v1/video/media/'+item['item_id']
        self.assertEqual(request('GET',url,headers={'Range':'bytes=-4'})[0],206)
        before=self.path.read_bytes()
        for suffix in ('.mp4','.json'):
            path=worker.folder/(item['item_id']+suffix)
            raw=path.read_bytes();path.write_bytes(bytes([raw[0]^1])+raw[1:])
            endpoint=url+('/sidecar' if suffix=='.json' else '')
            status,body,_=request('GET',endpoint)
            self.assertEqual(status,409);self.assertNotIn(raw,body)
        self.assertEqual(self.path.read_bytes(),before)

    def test_estimated_inverse_cue_uses_domain_error_not_local_error(self):
        worker=self.worker();calibration=self.payload()
        calibration['windows'][0]['anchors'][0]['video_uncertainty_ns']='20'
        receipt=self.backend.create(calibration)
        payload=dict(request_id='inverse-bound',selection=dict(kind='interval',candidate_index=0,
            **self.query(receipt,a=40,b=40)))
        worker.submit(payload);worker.process(worker.jobs[payload['request_id']],self.service.stop)
        item=worker.get(payload['request_id'])['items'][0]
        document=json.loads((worker.folder/(item['item_id']+'.json')).read_bytes())
        reference=document['manual_advantagescope']['reference']
        self.assertEqual(reference['basis'],'estimated_affine_frame_cue')
        self.assertEqual(reference['robot_ns'],str(self.base+40))
        # Declared affine envelope E(r)=20-2*r/5 permits true r=34 with PTS40:
        # |40-34|=6 <= E(34)=6.4, exceeding old local E(40)+1=5.
        # Endpoint maximum20 / scale1 + one integer rounding ns =21.
        self.assertEqual(reference['robot_uncertainty_ns'],'21')
        self.assertFalse(document['measured_camera_alignment'])

    def test_malformed_native_output_is_typed_unavailable(self):
        adapter=NativeMediaAdapter(self.config())
        with patch('robot_test_hub.video_media.sha',side_effect=['0'*64]),patch.object(adapter,'_run',return_value=b''):
            with self.assertRaises(MediaError):adapter.validate(threading.Event())
        for output in (b'[]',b'{"streams":[null]}',
            b'{"streams":[{"codec_name":"h264","pix_fmt":"yuv420p","time_base":"1/0"}],"frames":[]}'):
            with self.subTest(output=output),patch.object(adapter,'_run',return_value=output):
                with self.assertRaises(MediaError):adapter.probe(self.path,threading.Event())

    def test_recovered_oversized_sidecar_rejected_before_read(self):
        worker,payload,item=self.ready()
        sidepath=worker.folder/(item['item_id']+'.json')
        with sidepath.open('r+b') as stream:stream.truncate(4194305)
        real=type(sidepath).read_bytes
        def checked(path):
            if path==sidepath:raise AssertionError('oversized sidecar allocated before bound')
            return real(path)
        with patch.object(type(sidepath),'read_bytes',checked):
            with self.assertRaises(MediaError):worker.process(worker.jobs[payload['request_id']],self.service.stop)


if __name__=='__main__':unittest.main()
