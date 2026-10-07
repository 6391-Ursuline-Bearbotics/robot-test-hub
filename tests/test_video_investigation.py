"""Manual API protocol evidence is synthetic bytes, not encoded camera footage."""
import copy
from dataclasses import replace
import hashlib
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from robot_test_hub.collector import RobotStatus
from robot_test_hub.config import Config
from robot_test_hub.notebook import Notebook
from robot_test_hub.server import create_http_server
from robot_test_hub.service import HubService
from robot_test_hub.storage import OwnershipError
from robot_test_hub.video_investigation import InvestigationError, VideoInvestigation
from test_notebook import annotation


class Source:
    source_type='unconfigured'
    def status(self):return RobotStatus(None,time.monotonic(),'boot1',0,False)


class VideoInvestigationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        self.service=HubService(Config(data_dir=str(self.root/'hub')),Source())
        self.addCleanup(self.service.close)
        self.book=Notebook(self.service.root/'catalog.sqlite3')
        self.backend=VideoInvestigation(self.service,self.book)
        self.base=9007199254740993
        self.manifest,self.path=self.segment()

    def segment(self,capture='capture1',index=0,frames=((0,10),(10,10),(40,10),(50,10))):
        folder=self.service.root/'video'/'overview'/'practice'/capture
        folder.mkdir(parents=True,exist_ok=True)
        name=f'segment-{index:06d}.mkv';path=folder/name
        path.write_bytes(b'SYNTHETIC API PROTOCOL EVIDENCE NOT ENCODED VIDEO'+capture.encode())
        manifest=dict(schema_version=1,segment_id=f'{capture}-{index:06d}',camera_id='overview',
            session_id='practice',capture_id=capture,relative_path=name,source_type='SYNTHETIC',
            state='closed_verified',sha256=hashlib.sha256(path.read_bytes()).hexdigest(),size_bytes=path.stat().st_size,
            container='matroska',codec='ffv1',time_base='1/1000000000',start_pts_ns=str(frames[0][0]),
            end_pts_ns=str(sum(frames[-1])),frames=[dict(pts_ns=str(pts),duration_ns=str(duration)) for pts,duration in frames],
            utc_basis='unmapped_recording_pts',utc_uncertainty_ns=None,provenance={'private':'secret-camera-path'})
        path.with_name(name+'.json').write_text(json.dumps(manifest))
        with self.service.video.lock:
            self.service.video.records[manifest['segment_id']]=self.service.video._summary(manifest)
        return manifest,path

    def payload(self,manifest=None):
        manifest=manifest or self.manifest
        def anchor(index,offset):
            return dict(robot_ns=str(self.base+offset),segment_id=manifest['segment_id'],
                source_sha256=manifest['sha256'],frame_index=index,robot_uncertainty_ns='0',
                video_uncertainty_ns='0',evidence_label='operator visible cue')
        return dict(alignment_id='manual1',revision=1,expected_previous_sha256=None,
            robot_id='robot6391',boot_id='boot1',camera_id=manifest['camera_id'],session_id=manifest['session_id'],
            capture_id=manifest['capture_id'],segment_ids=[manifest['segment_id']],windows=[dict(
                continuity_id='continuous',start_robot_ns=str(self.base),end_robot_ns=str(self.base+50),
                segment_ids=[manifest['segment_id']],model_uncertainty_ns='0',anchors=[anchor(0,0),anchor(3,50)],
                assumed_scale=None,assumed_drift_uncertainty_ppm=None)])

    def query(self,receipt,a=5,b=45,**changes):
        value=dict(alignment_id=receipt['alignment_id'],revision=receipt['revision'],sha256=receipt['sha256'],
            robot_id='robot6391',boot_id='boot1',start_robot_ns=str(self.base+a),end_robot_ns=str(self.base+b),
            event_id=None,note_revision=None)
        value.update(changes)
        return value

    def test_actual_frame_pages_and_historical_capture_without_recorder(self):
        self.assertIsNone(self.service.video.recorder)
        page=self.backend.frames(self.manifest['segment_id'],limit=2)
        self.assertEqual(page['frames'],[dict(frame_index=0,pts_ns='0',duration_ns='10'),
                                        dict(frame_index=1,pts_ns='10',duration_ns='10')])
        self.assertEqual(page['next_offset'],2)
        self.assertEqual(page['total'],4)
        self.assertEqual(page['source_sha256'],self.manifest['sha256'])
        self.assertEqual(self.backend.frames(self.manifest['segment_id'],offset=2)['frames'][0]['pts_ns'],'40')
        self.assertNotIn('secret',json.dumps(page))
        self.assertNotIn(str(self.root),json.dumps(page))

    def test_precision_gaps_domain_and_reload_across_service_restart(self):
        receipt=self.backend.create(self.payload())
        result=self.backend.map(self.query(receipt))
        self.assertEqual(result['time_basis'],'explicit_manual_robot_interval')
        self.assertEqual(result['result']['state'],'partial')
        self.assertEqual(result['result']['spans'][0]['frame_indexes'],[0,1,2])
        self.assertEqual(result['result']['windows'][0]['unavailable_pts_intervals'],[['20','40']])
        self.assertNotIn('relative_path',json.dumps(result)+json.dumps(receipt))
        self.assertNotIn('segment-000000.mkv',json.dumps(result)+json.dumps(receipt))
        self.assertEqual(self.backend.map(self.query(receipt,25,25))['result']['state'],'gap')
        self.assertEqual(self.backend.map(self.query(receipt,boot_id='otherboot'))['result']['state'],'unavailable')
        self.assertFalse(result['result']['measured_camera_alignment'])
        self.assertEqual(receipt['document']['windows'][0]['anchors'][0]['robot_ns'],str(self.base))
        self.assertTrue(self.service.close())
        restarted=HubService(Config(data_dir=str(self.service.root)),Source());self.addCleanup(restarted.close)
        restarted.video.recover()
        backend=VideoInvestigation(restarted,Notebook(restarted.root/'catalog.sqlite3'))
        self.assertEqual(backend.get('manual1',1,receipt['sha256'])['document'],receipt['document'])
        self.assertEqual(backend.map(self.query(receipt))['result'],result['result'])

    def test_revision_idempotency_parent_conflict_and_paged_list(self):
        first=self.backend.create(self.payload())
        self.assertEqual(self.backend.create(self.payload()),first)
        second=self.payload();second['revision']=2;second['expected_previous_sha256']=first['sha256']
        second['windows'][0]['model_uncertainty_ns']='1'
        receipt=self.backend.create(second)
        self.assertNotEqual(first['sha256'],receipt['sha256'])
        self.assertEqual(self.backend.get('manual1',1,first['sha256'])['document'],first['document'])
        page=self.backend.list(limit=1)
        self.assertEqual(page['items'][0]['revision'],1)
        self.assertEqual(self.backend.list(limit=1,cursor=page['next_cursor'])['items'][0]['revision'],2)
        second['windows'][0]['model_uncertainty_ns']='2'
        with self.assertRaises(InvestigationError):self.backend.create(second)
        parent=self.backend.folder/'manual1-000001.json';parent.write_bytes(parent.read_bytes()+b' ')
        with self.assertRaises(InvestigationError) as raised:self.backend.map(self.query(receipt))
        self.assertEqual(raised.exception.code,'alignment_evidence_conflict')

    def test_altered_manifest_raw_digest_and_missing_raw_are_never_success(self):
        receipt=self.backend.create(self.payload())
        metadata=self.path.with_name(self.path.name+'.json');original=metadata.read_bytes()
        altered=copy.deepcopy(self.manifest)
        altered['frames'][1]['pts_ns']='11';altered['frames'][1]['duration_ns']='9'
        metadata.write_text(json.dumps(altered))
        with self.assertRaises(InvestigationError):self.backend.map(self.query(receipt))
        metadata.write_bytes(original)
        self.path.write_bytes(self.path.read_bytes()+b'tamper')
        with self.assertRaises(InvestigationError):self.backend.map(self.query(receipt))
        self.path.unlink()
        with self.assertRaises(InvestigationError) as raised:self.backend.map(self.query(receipt))
        self.assertEqual(raised.exception.code,'recording_unavailable')
        with self.assertRaises(InvestigationError):self.backend.get('manual1',1,'0'*64)

    def test_strict_bounds_float_ns_unknown_fields_and_capture_isolation(self):
        for update in (lambda p:p.update(unknown='secret'),lambda p:p.update(segment_ids=['../private']),
                       lambda p:p['windows'][0].update(start_robot_ns=float(self.base)),
                       lambda p:p['windows'][0]['anchors'][0].update(robot_ns=self.base),
                       lambda p:p['windows'][0]['anchors'][0].update(source_sha256='0'*64),
                       lambda p:p.update(windows=p['windows']*21),
                       lambda p:p['windows'][0].update(anchors=p['windows'][0]['anchors']*51)):
            payload=self.payload();update(payload)
            with self.subTest(payload=payload),self.assertRaises(InvestigationError):self.backend.create(payload)
        other,_=self.segment(capture='capture2')
        value=self.payload();value['segment_ids'].append(other['segment_id'])
        with self.assertRaises(InvestigationError):self.backend.create(value)
        for kwargs in ({'offset':-1},{'limit':101},{'offset':1.1}):
            with self.assertRaises(InvestigationError):self.backend.frames(self.manifest['segment_id'],**kwargs)

    def test_saved_exact_note_revision_is_context_not_utc_conversion(self):
        note=annotation();saved=self.book.save(note)['annotation']
        updated=copy.deepcopy(note);updated['revision']=2;updated['text']='later changed note'
        self.book.save(updated,expected_previous_revision=1)
        receipt=self.backend.create(self.payload())
        mapped=self.backend.map(self.query(receipt,event_id=note['event_id'],note_revision=1))
        self.assertEqual(mapped['annotation'],saved)
        self.assertEqual(mapped['result']['event_id'],note['event_id'])
        self.assertEqual(mapped['annotation']['event_utc_start_ns'],saved['event_utc_start_ns'])
        self.assertEqual(len(self.book.history(note['event_id'])['revisions']),2)
        with self.assertRaises(InvestigationError) as raised:
            self.backend.map(self.query(receipt,event_id=note['event_id'],note_revision=3))
        self.assertEqual(raised.exception.code,'annotation_revision_not_found')

    def test_concurrent_identical_revision_writers_are_serialized(self):
        barrier=threading.Barrier(3);results=[]
        def create():
            barrier.wait()
            try:results.append(self.backend.create(self.payload()))
            except Exception as exc:results.append(exc)
        threads=[threading.Thread(target=create) for _ in range(2)]
        for thread in threads:thread.start()
        barrier.wait()
        for thread in threads:thread.join(3);self.assertFalse(thread.is_alive())
        self.assertEqual(results[0],results[1])
        self.assertIsInstance(results[0],dict)
        self.assertEqual(len(list(self.backend.folder.glob('*.json'))),1)

    def test_selected_frame_bound_precedes_core_result_allocation(self):
        receipt=self.backend.create(self.payload())
        with patch('robot_test_hub.video_investigation.MAX_SELECTED_FRAMES',1), \
             patch('robot_test_hub.video_investigation.ManualAlignment.map_interval') as operation:
            with self.assertRaises(InvestigationError) as raised:self.backend.map(self.query(receipt))
            self.assertEqual(raised.exception.code,'oversized_interval')
            operation.assert_not_called()

    def test_document_response_and_aggregate_metadata_resource_bounds(self):
        with patch('robot_test_hub.video_investigation.MAX_DOCUMENT',10):
            with self.assertRaises(InvestigationError) as raised:self.backend.create(self.payload())
            self.assertEqual(raised.exception.code,'alignment_document_too_large')
            self.assertFalse(self.backend.folder.exists())
        with patch('robot_test_hub.video_investigation.MAX_RESPONSE',10):
            with self.assertRaises(InvestigationError) as raised:self.backend.create(self.payload())
            self.assertEqual(raised.exception.code,'oversized_response')
            self.assertFalse(self.backend.folder.exists())
        with patch('robot_test_hub.video_investigation.MAX_TOTAL_METADATA',10):
            with self.assertRaises(InvestigationError) as raised:self.backend.frames(self.manifest['segment_id'])
            self.assertEqual(raised.exception.code,'recording_evidence_too_large')

    def test_hashing_does_not_hold_video_health_lock(self):
        # _segments snapshot copy is released before original verification.
        from robot_test_hub.video_alignment import _hash
        entered=threading.Event();release=threading.Event();results=[]
        def slow(path):
            entered.set()
            if not release.wait(3):raise RuntimeError('Test hash coordination')
            return _hash(path)
        with patch('robot_test_hub.video_alignment._hash',side_effect=slow):
            thread=threading.Thread(target=lambda:results.append(self.backend.frames(self.manifest['segment_id'])))
            thread.start()
            try:
                self.assertTrue(entered.wait(1))
                self.assertTrue(self.service.video.lock.acquire(timeout=.2));self.service.video.lock.release()
                self.assertEqual(self.service.video.snapshot()['schema_version'],1)
            finally:release.set();thread.join(3)
        self.assertFalse(thread.is_alive());self.assertEqual(len(results),1)

    def test_pending_revision_publication_retains_archive_owner_during_shutdown(self):
        self.service.config=replace(self.service.config,shutdown_timeout=.05)
        entered=threading.Event();release=threading.Event();results=[]
        from robot_test_hub.video_alignment import AlignmentRevisions
        original=AlignmentRevisions.append
        def blocked(store,*args,**kwargs):
            entered.set()
            if not release.wait(3):raise RuntimeError('Publication coordination')
            return original(store,*args,**kwargs)
        def create():
            try:results.append(self.backend.create(self.payload()))
            except Exception as exc:results.append(exc)
        with patch.object(AlignmentRevisions,'append',blocked):
            worker=threading.Thread(target=create);worker.start()
            try:
                self.assertTrue(entered.wait(1))
                self.assertFalse(self.service.close())
                self.assertFalse(self.service.closed)
                with self.assertRaises(OwnershipError):HubService(Config(data_dir=str(self.service.root)),Source())
            finally:release.set();worker.join(3)
        self.assertFalse(worker.is_alive());self.assertIsInstance(results[0],dict)
        self.assertTrue(self.service.close())
        contender=HubService(Config(data_dir=str(self.service.root)),Source());self.assertTrue(contender.close())

    def test_shutdown_during_raw_hash_prevents_later_revision_publication(self):
        entered=threading.Event();release=threading.Event();results=[]
        from robot_test_hub.video_alignment import _hash
        def blocked(path):
            entered.set()
            if not release.wait(3):raise RuntimeError('Hash coordination')
            return _hash(path)
        def create():
            try:results.append(self.backend.create(self.payload()))
            except Exception as exc:results.append(exc)
        with patch('robot_test_hub.video_alignment._hash',side_effect=blocked), \
             patch('robot_test_hub.video_investigation.AlignmentRevisions.append') as append:
            worker=threading.Thread(target=create);worker.start()
            try:
                self.assertTrue(entered.wait(1))
                self.assertTrue(self.service.close())
            finally:release.set();worker.join(3)
            append.assert_not_called()
        self.assertFalse(worker.is_alive());self.assertIsInstance(results[0],InvestigationError)
        self.assertEqual(results[0].code,'service_stopping')
        self.assertFalse(self.backend.folder.exists())

    def test_http_contract_security_redaction_and_stopping(self):
        server=create_http_server(self.service,Source(),0)
        thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01});thread.start()
        def request(method,path,value=None,headers=None):
            client=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=2)
            try:
                client.request(method,path,json.dumps(value) if value is not None else None,
                    headers=headers or {'Content-Type':'application/json','X-Hub-Request':'1'})
                response=client.getresponse();return response.status,json.loads(response.read())
            finally:client.close()
        try:
            status,page=request('GET','/api/v1/video/frames?segment_id='+self.manifest['segment_id']+'&limit=2')
            self.assertEqual(status,200);self.assertEqual(len(page['frames']),2)
            status,receipt=request('POST','/api/v1/video/alignments',self.payload());self.assertEqual(status,200)
            status,loaded=request('GET',f"/api/v1/video/alignments/manual1?revision=1&sha256={receipt['sha256']}")
            self.assertEqual(status,200);self.assertEqual(loaded['document'],receipt['document'])
            status,mapped=request('POST','/api/v1/video/map',self.query(receipt))
            self.assertEqual(status,200);self.assertEqual(mapped['result']['state'],'partial')
            self.assertEqual(request('GET','/api/v1/video/alignments?limit=1')[0],200)
            status,error=request('GET','/api/v1/video/frames?segment_id=..%2Fsecret-path')
            self.assertEqual(status,400);self.assertNotIn('secret',json.dumps(error))
            self.assertEqual(request('POST','/api/v1/video/alignments',self.payload(),headers={'Content-Type':'application/json'})[0],403)
            self.assertEqual(request('POST','/api/v1/video/map',self.query(receipt),headers={
                'Content-Type':'application/json','X-Hub-Request':'1','Origin':'http://remote.invalid'})[0],403)
            status,error=request('POST','/api/v1/video/alignments',{'padding':'x'*32768})
            self.assertEqual(status,400);self.assertEqual(error['error_code'],'invalid_request_size')
            self.assertNotIn(str(self.root),json.dumps(receipt)+json.dumps(mapped))
            self.assertNotIn('secret-camera',json.dumps(receipt)+json.dumps(mapped))
            self.service.stop.set()
            self.assertEqual(request('POST','/api/v1/video/map',self.query(receipt))[0],503)
            self.assertEqual(request('GET','/api/v1/video/alignments')[0],503)
        finally:
            server.shutdown();server.server_close();thread.join(2)


if __name__=='__main__':unittest.main()
