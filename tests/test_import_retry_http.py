"""Actual loopback requests over failed genuine synthetic WPILOG imports."""
from contextlib import closing
import hashlib
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch

from robot_test_hub.config import Config
from robot_test_hub.importer import Importer
from robot_test_hub.pipeline import Pipeline
from robot_test_hub.server import create_http_server
from robot_test_hub.service import HubService
from robot_test_hub.storage import open_catalog
from robot_test_hub.wpilog import PROFILE
from test_processing_http import GeneratedRecordingSource

FIXTURE=Path(__file__).parent/'fixtures'/'synthetic'/'alpha7-main.wpilog'


class ImportRetryHttpTests(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        self.root=Path(folder.name)
        with closing(open_catalog(self.root)) as db:
            importer=Importer(self.root,db)
            with patch('robot_test_hub.importer.extract',side_effect=OSError('private fixture storage failure')):
                self.job=importer.import_file(FIXTURE,profile=PROFILE,source_type='SYNTHETIC')
        self.assertEqual(self.job['state'],'failed')
        self.assertEqual(self.job['error_code'],'import_io_or_integrity')
        self.source=GeneratedRecordingSource()
        self.service=HubService(Config(data_dir=str(self.root)),self.source)
        self.addCleanup(self.service.close)
        self.service._health('indexer','running')
        self.server=create_http_server(self.service,self.source,0)
        self.server.daemon_threads=False
        self.thread=threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':.01})
        self.thread.start()
        self.addCleanup(lambda:(self.server.shutdown(),self.thread.join(3),self.server.server_close()))
        self.origin='http://127.0.0.1:'+str(self.server.server_port)

    def payload(self):
        j=self.job
        return dict(request_id=str(uuid.uuid4()),job_id=j['id'],source_sha256=j['artifact_sha256'],
            extractor_version=j['extractor_version'],profile=j['profile'],mapping_revision=j['mapping_revision'],
            expected_attempts=j['attempts'],expected_updated_utc_ns=str(j['updated_utc_ns']))

    def request(self,method,path,payload=None,headers=None,raw=None):
        body=raw if raw is not None else json.dumps(payload).encode() if payload is not None else None
        settings={'Content-Type':'application/json','X-Hub-Request':'1','Origin':self.origin}
        if headers is not None:settings.update(headers)
        connection=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
        try:
            connection.request(method,path,body=body,headers=settings)
            response=connection.getresponse();data=response.read()
            return response.status,json.loads(data)
        finally:connection.close()

    def post(self,payload=None,**kwargs):
        return self.request('POST','/api/v1/imports/'+self.job['id']+'/retry',payload or self.payload(),**kwargs)

    def processing(self,section='imports'):
        return self.request('GET','/api/v1/processing/'+self.job['artifact_sha256']+'?section='+section)

    def test_http_enqueue_is_small_local_and_worker_publishes_runs_and_reports(self):
        raw=self.root/'raw'/self.job['artifact_sha256'][:2]/(self.job['artifact_sha256']+'.wpilog')
        before=raw.read_bytes()
        payload=self.payload()
        with patch('robot_test_hub.importer._digest',side_effect=AssertionError('HTTP must not hash')),\
             patch('robot_test_hub.importer.extract',side_effect=AssertionError('HTTP must not extract')),\
             patch.object(self.source,'status',side_effect=AssertionError('HTTP must not poll source')),\
             patch.object(self.source,'read',side_effect=AssertionError('HTTP must not download')):
            status,receipt=self.post(payload)
        self.assertEqual(status,202,receipt)
        self.assertNotIn('private',json.dumps(receipt))
        with closing(open_catalog(self.root)) as db:
            queued=Importer(self.root,db).get_job(self.job['id'])
            self.assertEqual(queued['state'],'failed')
            self.assertEqual(queued['attempts'],self.job['attempts'])
            Pipeline(self.root,db).tick()
            result=Importer(self.root,db).get_job(self.job['id'])
            self.assertEqual(result['state'],'succeeded')
            self.assertEqual(result['attempts'],self.job['attempts']+1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM import_jobs').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM import_requests').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT source_type FROM import_artifacts').fetchone()[0],'SYNTHETIC')
        status,runs=self.processing('runs');self.assertEqual(status,200,runs)
        self.assertEqual(runs['total'],2)
        status,reports=self.processing('reports');self.assertEqual(status,200,reports)
        self.assertEqual(reports['total'],2)
        for report in reports['items']:
            status,exact=self.request('GET',report['url'])
            self.assertEqual(status,200,exact)
            self.assertEqual(report['sha256'],exact['result_sha256'])
        self.assertEqual(raw.read_bytes(),before)
        self.assertEqual(hashlib.sha256(before).hexdigest(),self.job['artifact_sha256'])

    def test_duplicate_submission_recovery_and_stale_attempt_are_distinct(self):
        payload=self.payload()
        first_status,first=self.post(payload)
        self.assertEqual(first_status,202,first)
        repeated_status,repeated=self.post(payload)
        self.assertEqual(repeated_status,202,repeated)
        with closing(open_catalog(self.root)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM import_retry_requests').fetchone()[0],1)
            Pipeline(self.root,db).tick()
        status,receipt=self.post(payload)
        self.assertIn(status,(200,202),receipt)
        duplicate=dict(payload,request_id=str(uuid.uuid4()))
        status,receipt=self.post(duplicate)
        self.assertEqual(status,202,receipt)
        self.assertTrue(receipt['deduplicated'])
        changed=dict(payload,request_id=str(uuid.uuid4()),expected_updated_utc_ns=str(int(payload['expected_updated_utc_ns'])+1))
        status,receipt=self.post(changed)
        self.assertEqual(status,409,receipt)
        with closing(open_catalog(self.root)) as db:
            self.assertEqual(Importer(self.root,db).get_job(self.job['id'])['attempts'],self.job['attempts']+1)

    def test_origin_strict_json_and_stopping_reject_without_queue_changes(self):
        for headers in ({'Origin':'http://untrusted.invalid'},{'X-Hub-Request':'0'},{'Content-Type':'text/plain'}):
            status,body=self.post(headers=headers)
            self.assertEqual(status,403,body)
        payload=self.payload();encoded=json.dumps(payload)
        duplicate=encoded[:-1]+',"expected_attempts":'+str(payload['expected_attempts'])+'}'
        for raw in (duplicate.encode(),b'{}',b'{"private":"secret"}',b'x'*8193):
            status,body=self.post(raw=raw)
            self.assertEqual(status,400,body)
            self.assertNotIn('secret',json.dumps(body))
        self.service._health('indexer','failed','indexer_failed')
        self.assertEqual(self.post()[0],503)
        self.service._health('indexer','running')
        self.service.stop.set()
        self.assertEqual(self.post()[0],503)
        with closing(open_catalog(self.root)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM import_retry_requests').fetchone()[0],0)


if __name__=='__main__':unittest.main()
