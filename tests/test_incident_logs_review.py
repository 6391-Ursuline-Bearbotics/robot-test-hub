"""Independent pure-resolver tests using public synthetic WPILOG bytes only.
HTTP downloads and service lifecycle integration remain unqualified pending authorization.
"""
from contextlib import contextmanager
import copy
import hashlib
import io
import http.client
from dataclasses import replace
from robot_test_hub.server import create_http_server
from robot_test_hub.storage import DataRootOwner, OwnershipError
import json
import os
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import patch

from robot_test_hub.incident_logs import IncidentLogs, LogError
from robot_test_hub.importer import Importer
from robot_test_hub.runs import RunCatalog
import test_video_media as fixture
from test_wpilog import FIXTURES


class IncidentLogReviewTests(unittest.TestCase):
    segment=fixture.MediaTests.segment
    payload=fixture.MediaTests.payload
    config=fixture.MediaTests.config
    worker=fixture.MediaTests.worker

    def setUp(self):
        self._setup()

    def _setup(self,source_type='SYNTHETIC'):
        fixture.MediaTests.setUp(self)
        with self.book._connection() as db:
            importer=Importer(self.service.root,db)
            self.import_job=importer.import_file(FIXTURES/'alpha7-main.wpilog',source_type=source_type)
            cycles=list(importer.iter_dataset(self.import_job['id'],'cycle'))
            self.catalog=RunCatalog(db).rebuild([(self.import_job['id'],cycles)])
            row=db.execute('SELECT * FROM import_artifacts WHERE sha256=?',
                (self.import_job['artifact_sha256'],)).fetchone()
            self.artifact=dict(row)
        self.run=next(run for run in self.catalog['runs'] if run['known_boot'])
        self.base=int(self.run['start_monotonic_ns'])
        calibration=self.payload();calibration.update(robot_id=self.run['robot_id'],boot_id=self.run['boot_id'])
        pin=self.backend.create(calibration)
        self.worker_instance=self.worker()
        self.preservation=dict(request_id='incident-log-review',selection=dict(kind='context',
            alignment_id=pin['alignment_id'],revision=pin['revision'],sha256=pin['sha256'],
            catalog_revision=self.catalog['revision'],candidate_index=0,
            context=dict(kind='run',run_id=self.run['run_id'])))
        self.worker_instance.submit(self.preservation)
        self.worker_instance.process(self.worker_instance.jobs[self.preservation['request_id']],self.service.stop)
        self.item=self.worker_instance.get(self.preservation['request_id'])['items'][0]
        self.raw=self.service.root/self.artifact['relative_path'];self.original=self.raw.read_bytes()
        self.sidecar=self.worker_instance.folder/(self.item['item_id']+'.json')
        self.sidecar_before=self.sidecar.read_bytes()
        # Isolated public-synthetic resolver harness. Does not patch/recreate rejected
        # HubService lifecycle integration or expose any HTTP route.
        self.harness=SimpleNamespace(root=self.service.root,media=self.worker_instance,
            settings_lock=threading.Lock(),stop=threading.Event(),closed=False,io_lifetimes=[])
        self.manager=IncidentLogs(self.harness,slots=1)
        self.addCleanup(lambda:self.assertEqual(self.sidecar.read_bytes(),self.sidecar_before))

    def http(self):
        server=create_http_server(self.service,self.service.source,0)
        thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01});thread.start()
        self.addCleanup(lambda:(server.shutdown(),thread.join(2),server.server_close()))
        def request(method,path,headers=None):
            connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=2)
            try:
                connection.request(method,path,headers=headers or {})
                response=connection.getresponse()
                return response.status,response.read(),dict(response.getheaders())
            finally:connection.close()
        return request

    def test_actual_loopback_http_exact_original_range_head_tamper_and_shutdown(self):
        request=self.http();url='/api/v1/video/media/'+self.item['item_id']+'/logs'
        status,body,_=request('GET',url)
        self.assertEqual(status,200)
        self.assertFalse(json.loads(body)['items'][0]['original_bytes_reverified_for_export'])
        download=url+'/'+self.import_job['id']
        status,body,headers=request('GET',download)
        self.assertEqual(status,200);self.assertEqual(body,self.original)
        self.assertEqual(headers['Content-Disposition'],
            'attachment; filename="'+self.artifact['sha256']+'.wpilog"')
        self.assertEqual(request('GET',download,{'Range':'bytes=3-19'})[:2],(206,self.original[3:20]))
        status,body,headers=request('HEAD',download,{'Range':'bytes=-7'})
        self.assertEqual(status,206);self.assertEqual(body,b'');self.assertEqual(headers['Content-Length'],'7')
        self.assertEqual(request('GET',download,{'Range':'bytes=0-1,3-5'})[0],416)
        self.assertEqual(request('GET',url+'/'+'0'*64)[0],404)
        self.raw.write_bytes(bytes([self.original[0]^1])+self.original[1:])
        status,body,_=request('GET',download);self.assertEqual(status,409)
        self.assertNotIn(str(self.service.root).encode(),body)
        self.raw.write_bytes(self.original)
        self.assertTrue(self.service.close())
        self.assertEqual(request('GET',url)[0],503)
        self.assertEqual(request('HEAD',download)[0],503)

    def test_actual_http_failed_fd_cleanup_keeps_hub_owner(self):
        request=self.http();url='/api/v1/video/media/'+self.item['item_id']+'/logs/'+self.import_job['id']
        self.service.config=replace(self.service.config,shutdown_timeout=.05)
        close_entered=threading.Event();release=threading.Event();errors=[];results=[]
        real_fdopen=os.fdopen
        class Stream:
            def __init__(self,raw):self.raw=raw;self.calls=0
            def __getattr__(self,key):return getattr(self.raw,key)
            def close(self):
                self.calls+=1
                if self.calls==1:raise OSError('secret failed cleanup')
                close_entered.set();release.wait(2);self.raw.close()
        def get():
            try:results.append(request('GET',url))
            except Exception as error:errors.append(error)
        with patch('robot_test_hub.incident_logs.os.fdopen',side_effect=lambda *a,**k:Stream(real_fdopen(*a,**k))):
            client=threading.Thread(target=get);client.start()
            try:
                self.assertTrue(close_entered.wait(2));self.assertFalse(self.service.close())
                with self.assertRaises(OwnershipError):DataRootOwner(self.service.root)
            finally:release.set();client.join(2)
        self.assertFalse(client.is_alive());self.assertEqual(errors,[])
        self.assertEqual(results[0][:2],(200,self.original))
        for lifetime in self.service.io_lifetimes:lifetime.join(2);self.assertFalse(lifetime.is_alive())
        self.assertTrue(self.service.close())
        replacement=DataRootOwner(self.service.root);replacement.close()

    def test_path_ctime_api_mismatch_keeps_fd_mutation_and_identity_checks(self):
        # CPython Windows3.13 fstat exposes ChangeTime as ctime, while
        # path.stat preserves CreationTime; unchanged files differ legitimately.
        with self.raw.open('rb') as stream:
            baseline=os.fstat(stream.fileno())
            def info(**changes):
                values={name:getattr(baseline,name) for name in
                    ('st_mode','st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns')}
                values.update(changes);return SimpleNamespace(**values)
            opened=info(st_ctime_ns=900);current=info(st_ctime_ns=700)
            stamp=(self.manager._stamp(opened),self.manager._stamp(current))
            original_stat=type(self.raw).stat
            def path_stat(path,*args,**kwargs):
                if path==self.raw:return current
                return original_stat(path,*args,**kwargs)
            with patch('robot_test_hub.incident_logs.os.fstat',return_value=opened),\
                    patch.object(type(self.raw),'stat',path_stat):
                self.manager._identity(stream,self.raw,stamp)
                current=info(st_ctime_ns=701)
                with self.assertRaises(LogError):self.manager._identity(stream,self.raw,stamp)
                current=info(st_ctime_ns=700,st_ino=baseline.st_ino+1)
                with self.assertRaises(LogError):self.manager._identity(stream,self.raw,stamp)
                current=info(st_ctime_ns=700,st_mtime_ns=baseline.st_mtime_ns+1)
                with self.assertRaises(LogError):self.manager._identity(stream,self.raw,stamp)
                current=info(st_ctime_ns=700)
                with patch('robot_test_hub.incident_logs.os.fstat',return_value=info(st_ctime_ns=1000)):
                    with self.assertRaises(LogError):self.manager._identity(stream,self.raw,stamp)

    def test_saved_incident_exact_public_synthetic_original_and_metadata_honesty(self):
        metadata=self.manager.metadata(self.item['item_id'])
        self.assertEqual(metadata['qualification'],'pinned_catalog_references')
        candidate=metadata['items'][0]
        self.assertEqual(candidate['import_job_id'],self.import_job['id'])
        self.assertEqual(candidate['sha256'],self.artifact['sha256'])
        self.assertEqual(candidate['state'],'download_candidate')
        self.assertFalse(candidate['original_bytes_reverified_for_export'])
        self.assertNotIn(str(self.service.root),json.dumps(metadata))
        with self.manager.open(self.item['item_id'],self.import_job['id']) as verified:
            self.assertEqual(verified.receipt['size_bytes'],len(self.original))
            self.assertEqual(verified.read(len(self.original)),self.original)
            self.assertTrue(self.manager.is_alive())
        self.assertFalse(self.manager.is_alive())
        self.assertEqual(self.raw.read_bytes(),self.original)

    def test_same_size_mutation_and_current_catalog_path_substitution_rejected(self):
        self.raw.write_bytes(bytes([self.original[0]^1])+self.original[1:])
        with self.assertRaises(LogError) as error:
            with self.manager.open(self.item['item_id'],self.import_job['id']):pass
        self.assertEqual(error.exception.code,'incident_log_evidence_conflict')
        self.assertFalse(self.manager.is_alive())
        self.raw.write_bytes(self.original)
        with self.book._connection() as db:
            db.execute('UPDATE import_artifacts SET relative_path=? WHERE sha256=?',
                ('../secret-credentials.wpilog',self.artifact['sha256']))
            db.commit()
        metadata=self.manager.metadata(self.item['item_id'])
        self.assertEqual(metadata['items'][0]['error_code'],'incident_log_catalog_conflict')
        with patch('robot_test_hub.incident_logs.os.open',side_effect=AssertionError('substituted path opened')):
            with self.assertRaises(LogError):
                with self.manager.open(self.item['item_id'],self.import_job['id']):pass

    def test_sidecar_invalid_duplicate_pins_and_old_unknown_provenance_never_upgraded(self):
        original=json.loads(self.sidecar_before)
        for source in ('TRANSFER_CHECKSUM_MISMATCH','other'):
            document=copy.deepcopy(original);document['original_logs']['items'][0]['source_type']=source
            body=json.dumps(document).encode()
            with patch.dict(self.worker_instance.items[self.item['item_id']],{'sidecar_sha256':hashlib.sha256(body).hexdigest()}),\
                    patch.object(self.worker_instance,'open_item',return_value=(io.BytesIO(body),len(body))):
                result=self.manager.metadata(self.item['item_id'])
            self.assertEqual(result['items'][0]['state'],'unavailable')
            self.assertFalse(result['items'][0]['original_bytes_reverified_for_export'])
        document=copy.deepcopy(original)
        document['original_logs']['items']*=2;body=json.dumps(document).encode()
        with patch.dict(self.worker_instance.items[self.item['item_id']],{'sidecar_sha256':hashlib.sha256(body).hexdigest()}),\
                    patch.object(self.worker_instance,'open_item',return_value=(io.BytesIO(body),len(body))):
            with self.assertRaises(LogError):self.manager.metadata(self.item['item_id'])
        self.assertFalse(self.manager.is_alive())

    def test_stream_truncation_and_deadline_do_not_report_success(self):
        with self.manager.open(self.item['item_id'],self.import_job['id']) as verified:
            self.raw.write_bytes(self.original[:-1])
            with self.assertRaises(LogError) as error:verified.read(64)
            self.assertEqual(error.exception.code,'incident_log_evidence_conflict')
        self.raw.write_bytes(self.original)
        self.manager.clock=lambda:0
        with self.manager.open(self.item['item_id'],self.import_job['id']) as verified:
            self.manager.clock=lambda:301
            with self.assertRaises(LogError) as error:verified.read(64)
            self.assertEqual(error.exception.code,'incident_log_timeout')
        self.assertFalse(self.manager.is_alive())

    def test_fd_failed_close_retains_slot_until_cleanup_finishes(self):
        close_entered=threading.Event();release=threading.Event();yielded=threading.Event();errors=[]
        real_fdopen=os.fdopen
        class Stream:
            def __init__(self,raw):self.raw=raw;self.calls=0
            def __getattr__(self,key):return getattr(self.raw,key)
            def close(self):
                self.calls+=1
                if self.calls==1:raise OSError('private native cleanup detail')
                close_entered.set();release.wait(2);self.raw.close()
        def open_fd(*args,**kwargs):return Stream(real_fdopen(*args,**kwargs))
        def operation():
            try:
                with self.manager.open(self.item['item_id'],self.import_job['id']):yielded.set()
            except Exception as error:errors.append(error)
        with patch('robot_test_hub.incident_logs.os.fdopen',side_effect=open_fd):
            worker=threading.Thread(target=operation);worker.start()
            try:
                self.assertTrue(yielded.wait(2));self.assertTrue(close_entered.wait(2))
                self.assertTrue(self.manager.is_alive())
                with self.assertRaises(LogError) as error:self.manager.metadata(self.item['item_id'])
                self.assertEqual(error.exception.code,'incident_logs_busy')
                self.manager.join(.05);self.assertTrue(self.manager.is_alive())
            finally:release.set();worker.join(2)
        self.assertFalse(worker.is_alive());self.assertEqual(errors,[])
        self.assertFalse(self.manager.is_alive())



class ManualIncidentLogReviewTests(unittest.TestCase):
    segment=fixture.MediaTests.segment
    payload=fixture.MediaTests.payload
    config=fixture.MediaTests.config
    worker=fixture.MediaTests.worker
    _setup=IncidentLogReviewTests._setup
    http=IncidentLogReviewTests.http

    def setUp(self):self._setup('MANUAL_LOCAL')

    def test_explicit_manual_projection_has_real_download_and_unknown_version_rejected(self):
        document=json.loads(self.sidecar_before)
        self.assertEqual(document['original_logs']['projection_version'],2)
        self.assertEqual(document['original_logs']['items'][0]['source_type'],'MANUAL_LOCAL')
        request=self.http();url='/api/v1/video/media/'+self.item['item_id']+'/logs'
        self.assertEqual(json.loads(request('GET',url)[1])['items'][0]['state'],'download_candidate')
        self.assertEqual(request('GET',url+'/'+self.import_job['id'])[:2],(200,self.original))
        document['original_logs']['projection_version']=True;body=json.dumps(document).encode()
        with patch.dict(self.worker_instance.items[self.item['item_id']],{'sidecar_sha256':hashlib.sha256(body).hexdigest()}),\
                patch.object(self.worker_instance,'open_item',return_value=(io.BytesIO(body),len(body))):
            with self.assertRaises(LogError):self.manager.metadata(self.item['item_id'])
        self.assertEqual(self.raw.read_bytes(),self.original)

    def test_legacy_manual_recovery_keeps_exact_sidecar_and_unavailable_source(self):
        from robot_test_hub.video_media import MediaWorker
        legacy=copy.deepcopy(self.preservation);legacy['request_id']='legacy-manual-reference'
        worker=self.worker_instance;worker.submit(legacy);original_projection=worker._log_references
        # Construct the historical fixture with the historical serializer;
        # never edit persisted sidecar/original bytes into a stronger reference.
        with patch.object(worker,'_log_references',side_effect=lambda *a,**kw:original_projection(*a,projection_version=1)):
            worker.process(worker.jobs[legacy['request_id']],self.service.stop)
        item=worker.get(legacy['request_id'])['items'][0]
        sidepath=worker.folder/(item['item_id']+'.json');before=sidepath.read_bytes()
        document=json.loads(before)
        self.assertNotIn('projection_version',document['original_logs'])
        self.assertEqual(document['original_logs']['items'][0]['source_type'],'other')
        recovered=MediaWorker(self.service,None);recovered.jobs=copy.deepcopy(worker.jobs)
        recovered.process(recovered.jobs[legacy['request_id']],self.service.stop)
        self.assertEqual(sidepath.read_bytes(),before)
        self.assertEqual(recovered.get(legacy['request_id'])['state'],'ready')
        self.assertEqual(recovered.get(legacy['request_id'])['items'][0]['sidecar_sha256'],hashlib.sha256(before).hexdigest())
        self.service.media=recovered
        request=self.http();url='/api/v1/video/media/'+item['item_id']+'/logs'
        candidate=json.loads(request('GET',url)[1])['items'][0]
        self.assertEqual(candidate['source_type'],'other');self.assertEqual(candidate['state'],'unavailable')
        self.assertEqual(request('GET',url+'/'+self.import_job['id'])[0],409)
        self.assertEqual(sidepath.read_bytes(),before);self.assertEqual(self.raw.read_bytes(),self.original)

if __name__=='__main__':unittest.main()
