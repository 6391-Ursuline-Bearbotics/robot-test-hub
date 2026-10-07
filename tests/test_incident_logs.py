"""Original-log resolution/localhost download tests use public generated Alpha7 bytes."""
from dataclasses import replace
import http.client
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from robot_test_hub.config import Config
from robot_test_hub.importer import Importer,install_schema
from robot_test_hub.incident_logs import IncidentLogs,LogError,byte_range
from robot_test_hub.service import HubService
from robot_test_hub.server import create_http_server
from robot_test_hub.storage import DataRootOwner,OwnershipError
from test_wpilog import FIXTURES
from test_video_investigation import Source


class IncidentLogsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.service=HubService(Config(data_dir=str(Path(self.temp.name)/'hub')),Source())
        self.addCleanup(self.service.close)
        with self.service.settings_db:install_schema(self.service.settings_db)
        self.job=Importer(self.service.root,self.service.settings_db).import_file(FIXTURES/'alpha7-main.wpilog',source_type='SYNTHETIC')
        self.assertEqual(self.job['state'],'succeeded')
        self.raw=(FIXTURES/'alpha7-main.wpilog').read_bytes();self.sha=hashlib.sha256(self.raw).hexdigest()
        self.path=self.service.root/'raw'/self.sha[:2]/(self.sha+'.wpilog');self.item='a'*64
        self.document=dict(item_id=self.item,original_logs=dict(basis='saved_run_import_job_references',items=[dict(
            import_job_id=self.job['id'],state='catalog_reference',sha256=self.sha,size_bytes=len(self.raw),
            source_type='SYNTHETIC',format_valid=True,original_bytes_reverified_for_export=False)]))
        class Media:
            lock=threading.Lock()
            @property
            def items(media):
                return {self.item:dict(sidecar_sha256=hashlib.sha256(json.dumps(self.document).encode()).hexdigest())}
            def open_item(media,item_id,sidecar=False):
                if item_id!=self.item or not sidecar:raise LogError('media_not_ready',404)
                body=json.dumps(self.document).encode();return io.BytesIO(body),len(body)
        self.service.media=Media();self.logs=IncidentLogs(self.service)

    def test_public_generated_original_exact_bytes_and_candidate_semantics(self):
        result=self.logs.metadata(self.item);candidate=result['items'][0]
        self.assertEqual(result['qualification'],'pinned_catalog_references')
        self.assertEqual(candidate['state'],'download_candidate')
        self.assertFalse(candidate['original_bytes_reverified_for_export'])
        with self.logs.open(self.item,self.job['id']) as original:
            self.assertEqual(original.read(65536),self.raw)
            self.assertEqual(original.read(65536),b'')
        self.assertFalse(self.logs.is_alive())
        self.assertNotIn(str(self.service.root),json.dumps(result))

    def test_current_identity_path_source_and_format_changes_are_not_substituted(self):
        for column,value in [('relative_path','raw/private.wpilog'),('source_type','VERIFIED_TRANSFER'),
                              ('format_state','invalid'),('size_bytes',len(self.raw)+1)]:
            with self.subTest(column=column):
                old=self.service.settings_db.execute('SELECT '+column+' FROM import_artifacts').fetchone()[0]
                with self.service.settings_db:self.service.settings_db.execute('UPDATE import_artifacts SET '+column+'=?',(value,))
                self.assertEqual(self.logs.metadata(self.item)['items'][0]['state'],'unavailable')
                with self.assertRaises(LogError):
                    with self.logs.open(self.item,self.job['id']):pass
                with self.service.settings_db:self.service.settings_db.execute('UPDATE import_artifacts SET '+column+'=?',(old,))

    def test_same_size_mutation_rejected_and_metadata_does_not_claim_hash(self):
        mutated=bytearray(self.raw);mutated[-1]^=1;self.path.write_bytes(mutated)
        self.assertEqual(self.logs.metadata(self.item)['items'][0]['state'],'download_candidate')
        with self.assertRaises(LogError) as raised:
            with self.logs.open(self.item,self.job['id']):pass
        self.assertEqual(raised.exception.code,'incident_log_evidence_conflict')

    def test_references_unknown_provenance_and_stopping(self):
        with self.assertRaises(LogError):
            with self.logs.open(self.item,'not-saved'):pass
        for source in ('other','TRANSFER_CHECKSUM_MISMATCH','MANUAL'):
            self.document['original_logs']['items'][0]['source_type']=source
            result=self.logs.metadata(self.item)['items'][0]
            self.assertEqual(result['source_type'],source);self.assertEqual(result['state'],'unavailable')
        self.service.stop.set()
        with self.assertRaises(LogError):self.logs.metadata(self.item)

    def test_slot_stays_owned_until_transient_close_failure_and_blocked_cleanup_finish(self):
        entered=threading.Event();release=threading.Event();failures=[]
        original=self.logs._close
        def close(stream):
            if stream is not None and hasattr(stream,'name'):
                entered.set();release.wait()
            original(stream)
        self.logs._close=close
        def read():
            try:
                with self.logs.open(self.item,self.job['id']):pass
            except BaseException as exc:failures.append(exc)
        thread=threading.Thread(target=read);thread.start()
        try:
            self.assertTrue(entered.wait(2));self.logs.join(.01);self.assertTrue(self.logs.is_alive())
            self.service.config=replace(self.service.config,shutdown_timeout=.05)
            self.assertFalse(self.service.close())
            with self.assertRaises(OwnershipError):DataRootOwner(self.service.root)
        finally:release.set();thread.join(2)
        self.assertEqual(failures,[]);self.assertFalse(self.logs.is_alive())
        self.assertTrue(self.service.close())
        class Pipe(io.BytesIO):
            attempts=0
            def close(self):
                self.attempts+=1
                if self.attempts==1:raise OSError('private path')
                super().close()
        pipe=Pipe();IncidentLogs._close(pipe);self.assertEqual(pipe.attempts,2)

    def test_slots_busy_promptly_and_verification_deadline(self):
        with self.logs._slot(),self.logs._slot():
            with self.assertRaises(LogError) as raised:self.logs.metadata(self.item)
            self.assertEqual(raised.exception.code,'incident_logs_busy')
        with patch.object(self.logs,'clock',side_effect=[0,61]):
            with self.assertRaises(LogError) as raised:
                with self.logs.open(self.item,self.job['id']):pass
        self.assertEqual(raised.exception.code,'incident_log_timeout')

    def test_single_byte_ranges(self):
        self.assertEqual(byte_range(None,10),(0,9,200))
        self.assertEqual(byte_range('bytes=2-4',10),(2,4,206))
        self.assertEqual(byte_range('bytes=-3',10),(7,9,206))
        for value in ('bytes=0-1,3-4','bytes=-0','bytes=10-','bytes=5-2','other=1-2'):
            with self.subTest(value=value),self.assertRaises(LogError):byte_range(value,10)

    def test_actual_localhost_metadata_get_head_range_host_query_and_stop(self):
        server=create_http_server(self.service,self.service.source,0)
        thread=threading.Thread(target=server.serve_forever);thread.start()
        self.addCleanup(lambda:(server.shutdown(),thread.join(),server.server_close()))
        def request(method,path,headers=None):
            connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=2)
            connection.request(method,path,headers=headers or {})
            response=connection.getresponse();result=response.status,dict(response.getheaders()),response.read()
            connection.close();return result
        metadata='/api/v1/video/media/'+self.item+'/logs';download=metadata+'/'+self.job['id']
        status,_,body=request('GET',metadata)
        self.assertEqual(status,200);self.assertEqual(json.loads(body)['items'][0]['state'],'download_candidate')
        status,headers,body=request('GET',download)
        self.assertEqual(status,200);self.assertEqual(body,self.raw)
        self.assertIn(self.sha+'.wpilog',headers['Content-Disposition'])
        self.assertEqual(request('HEAD',download)[2],b'')
        status,headers,body=request('GET',download,{'Range':'bytes=3-12'})
        self.assertEqual(status,206);self.assertEqual(body,self.raw[3:13]);self.assertEqual(headers['Content-Length'],'10')
        self.assertEqual(request('GET',download,{'Range':'bytes=1-2,5-6'})[0],416)
        self.assertEqual(request('GET',metadata+'?path=secret')[0],400)
        self.assertEqual(request('GET',download,{'Host':'external.example'})[0],403)
        self.assertEqual(request('GET',metadata+'/not-pinned')[0],404)
        self.service.stop.set()
        self.assertEqual(request('GET',metadata)[0],503);self.assertEqual(request('HEAD',download)[0],503)

    def test_stream_checks_stop_after_verification(self):
        with self.logs.open(self.item,self.job['id']) as original:
            self.service.stop.set()
            with self.assertRaises(LogError) as raised:original.read(1)
            self.assertEqual(raised.exception.code,'service_stopping')

    def test_sidecar_read_must_match_cached_ready_receipt_even_after_open_verification(self):
        old=self.service.media.open_item
        def changed(*args,**kwargs):
            body=copy.deepcopy(self.document)
            body['original_logs']['items'][0]['source_type']='VERIFIED_TRANSFER'
            raw=json.dumps(body).encode();return io.BytesIO(raw),len(raw)
        self.service.media.open_item=changed
        with self.assertRaises(LogError) as raised:self.logs.metadata(self.item)
        self.assertEqual(raised.exception.code,'incident_sidecar_conflict')

    def test_unavailable_basis_cannot_offer_catalog_candidates(self):
        self.document['original_logs']['basis']='unavailable'
        with self.assertRaises(LogError) as raised:self.logs.metadata(self.item)
        self.assertEqual(raised.exception.code,'incident_sidecar_conflict')
        self.document['original_logs']['items']=[]
        self.assertEqual(self.logs.metadata(self.item)['items'],[])

    def test_unknown_or_boolean_projection_version_is_not_accepted(self):
        for value in (True,1,3,'2'):
            self.document['original_logs']['projection_version']=value
            with self.subTest(value=value),self.assertRaises(LogError):self.logs.metadata(self.item)
        self.document['original_logs']['projection_version']=2
        self.assertEqual(self.logs.metadata(self.item)['items'][0]['state'],'download_candidate')

    def test_windows_313_ctime_semantics_accept_unchanged_fd_and_keep_all_mutation_checks(self):
        real_fstat=os.fstat;real_stat=Path.stat
        changes={}
        def snapshot(info,*,fd):
            values={key:getattr(info,key) for key in ('st_mode','st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns')}
            birth=getattr(info,'st_birthtime_ns',info.st_ctime_ns)
            values.update(st_birthtime_ns=birth,st_ctime_ns=birth+(1000000000 if fd else 0))
            values.update(changes.get('fd' if fd else 'path',{}))
            return SimpleNamespace(**values)
        def fstat(descriptor):return snapshot(real_fstat(descriptor),fd=True)
        def path_stat(path,*args,**kwargs):
            value=real_stat(path,*args,**kwargs)
            return snapshot(value,fd=False) if path==self.path else value
        with patch('robot_test_hub.incident_logs.os.fstat',side_effect=fstat),patch.object(Path,'stat',path_stat):
            with self.logs.open(self.item,self.job['id']) as original:
                self.assertEqual(original.read(65536),self.raw)
            with self.logs.open(self.item,self.job['id']) as original:
                info=real_stat(self.path);birth=getattr(info,'st_birthtime_ns',info.st_ctime_ns)
                cases=[('fd','st_ctime_ns',birth+1000000001),
                       ('path','st_ctime_ns',birth+1),
                       ('path','st_ino',info.st_ino+1),
                       ('path','st_birthtime_ns',birth+1),
                       ('fd','st_mtime_ns',info.st_mtime_ns+1),
                       ('path','st_size',info.st_size+1)]
                for api,key,value in cases:
                    changes[api]={key:value}
                    with self.subTest(api=api,key=key),self.assertRaises(LogError):original.read(1)
                    changes.clear()
                self.assertEqual(original.read(65536),self.raw)


if __name__=='__main__':unittest.main()
