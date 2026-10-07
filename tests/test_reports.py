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
from robot_test_hub.importer import Importer
from robot_test_hub.pipeline import Pipeline
from robot_test_hub.reports import generate
from robot_test_hub.runs import RunCatalog
from robot_test_hub.server import create_http_server
from robot_test_hub.service import HubService
from robot_test_hub.storage import open_catalog
from robot_test_hub.wpilog import PROFILE


class ReportsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.db=open_catalog(self.root)
        fixture=Path(__file__).parent/'fixtures/synthetic/alpha7-main.wpilog'
        self.original=fixture.read_bytes()
        Importer(self.root,self.db).import_file(fixture,profile=PROFILE,source_type='SYNTHETIC')

    def tearDown(self):
        self.db.close();self.temp.cleanup()

    def test_automatic_reports_keep_provenance_and_restart_idempotently(self):
        value=Pipeline(self.root,self.db).tick()
        self.assertEqual(value['reports'],2)
        before=self.db.execute('SELECT report_id,result_json FROM analysis_reports ORDER BY report_id').fetchall()
        for row in before:
            report=json.loads(row['result_json'])
            self.assertEqual(report['source_type'],'synthetic')
            self.assertEqual(report['overall_health'],'not_assessed')
            self.assertEqual(report['source_hashes'],[hashlib.sha256(self.original).hexdigest()])
            self.assertEqual(report['checks'][1]['outcome'],'insufficient_data')
            self.assertIn('physical_sensor_freshness_not_qualified',report['checks'][0]['unavailable'])
        Pipeline(self.root,self.db).tick()
        after=self.db.execute('SELECT report_id,result_json FROM analysis_reports ORDER BY report_id').fetchall()
        self.assertEqual([tuple(r) for r in before],[tuple(r) for r in after])

    def test_resource_limit_preserves_unknown_and_prior_version(self):
        Pipeline(self.root,self.db).tick()
        reports=generate(self.root,self.db,RunCatalog(self.db).current(),max_rows=2)
        check=reports[0]['checks'][0]
        self.assertEqual(check['outcome'],'insufficient_data')
        self.assertIn('report_resource_limit_reached',check['unavailable'])
        self.assertGreater(self.db.execute('SELECT count(*) FROM analysis_reports').fetchone()[0],2)

    def test_chronological_merge_cannot_hide_stalled_source_cycles(self):
        from robot_test_hub.runs import rebuild_from_imports
        document=rebuild_from_imports(self.root,self.db)
        original_iterator=Importer.iter_dataset
        def duplicate_cycles(importer,job_id):
            for row in original_iterator(importer,job_id):
                yield row
                if row.get('kind')=='cycle':
                    yield dict(row)
        with patch.object(Importer,'iter_dataset',duplicate_cycles):
            reports=generate(self.root,self.db,document)
        for report in reports:
            check=report['checks'][0]
            self.assertEqual(check['outcome'],'finding')
            finding=next(f for f in check['findings'] if f['kind']=='nonadvancing_source_cycle_timestamps')
            self.assertGreater(finding['count'],0)
            self.assertEqual(finding['source_references'][0]['source_sha256'],hashlib.sha256(self.original).hexdigest())

    def test_new_import_mapping_replaces_catalog_input_without_deleting_old_evidence(self):
        Pipeline(self.root,self.db).tick()
        original=Importer(self.root,self.db).list_jobs()[0]
        fixture=Path(__file__).parent/'fixtures/synthetic/alpha7-main.wpilog'
        # Simulate an explicitly qualified mapper upgrade. An arbitrary unknown
        # revision is unsupported and must never displace the qualified dataset.
        with patch('robot_test_hub.importer.MAPPING_REVISION','test-new-mapping'):
            revised=Importer(self.root,self.db).import_file(fixture,profile=PROFILE,source_type='SYNTHETIC',mapping_revision='test-new-mapping')
        self.assertEqual(revised['state'],'succeeded')
        Pipeline(self.root,self.db).tick()
        self.assertEqual(len(Importer(self.root,self.db).list_jobs()),2)
        self.assertEqual(len(RunCatalog(self.db).current()['runs']),2)
        for run in RunCatalog(self.db).current()['runs']:
            self.assertEqual(run['segment_ids'],[revised['id']])
            self.assertNotIn(original['id'],run['segment_ids'])

    def test_unsupported_mapping_does_not_displace_qualified_import(self):
        original=Importer(self.root,self.db).list_jobs()[0]
        fixture=Path(__file__).parent/'fixtures/synthetic/alpha7-main.wpilog'
        unsupported=Importer(self.root,self.db).import_file(fixture,profile=PROFILE,source_type='SYNTHETIC',
                                                           mapping_revision='unknown-mapping')
        self.assertEqual(unsupported['state'],'unsupported')
        Pipeline(self.root,self.db).tick()
        for run in RunCatalog(self.db).current()['runs']:
            self.assertEqual(run['segment_ids'],[original['id']])

    def test_qualified_mapping_upgrade_wins_equal_catalog_timestamps(self):
        original=Importer(self.root,self.db).list_jobs()[0]
        fixture=Path(__file__).parent/'fixtures/synthetic/alpha7-main.wpilog'
        with patch('robot_test_hub.importer.MAPPING_REVISION','test-new-mapping'):
            revised=Importer(self.root,self.db).import_file(fixture,profile=PROFILE,source_type='SYNTHETIC',mapping_revision='test-new-mapping')
        self.assertEqual(revised['state'],'succeeded')
        with self.db:
            self.db.execute('UPDATE import_jobs SET created_utc_ns=1,updated_utc_ns=1')
        Pipeline(self.root,self.db).tick()
        for run in RunCatalog(self.db).current()['runs']:
            self.assertEqual(run['segment_ids'],[revised['id']])
        self.assertEqual(Importer(self.root,self.db).get_job(original['id'])['state'],'succeeded')


class ReviewHttpTests(unittest.TestCase):
    def test_review_routes_persist_and_refuse_foreign_or_stopping_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            source=DemoSource();service=HubService(Config(data_dir=directory),source)
            httpd=create_http_server(service,source,0)
            thread=threading.Thread(target=httpd.serve_forever,kwargs={'poll_interval':.01});thread.start()
            def request(method,path,body=None,origin=None):
                connection=http.client.HTTPConnection('127.0.0.1',httpd.server_address[1],timeout=3)
                headers={'Content-Type':'application/json','X-Hub-Request':'1'}
                if origin:headers['Origin']=origin
                try:
                    connection.request(method,path,json.dumps(body) if body is not None else None,headers)
                    response=connection.getresponse()
                    return response.status,response.read()
                finally:connection.close()
            try:
                assignment={'id':'fixture-assignment','component_id':'synthetic-module','robot_id':'synthetic',
                    'location':'FL','start_utc_ns':'1000000000','end_utc_ns':None,'reviewer':'Synthetic tester','rationale':'HTTP fixture'}
                self.assertEqual(request('POST','/api/v1/review/assignments',assignment,origin='http://evil.example')[0],403)
                self.assertEqual(request('POST','/api/v1/review/assignments',assignment)[0],200)
                self.assertEqual(request('POST','/api/v1/review/assignments',assignment)[0],200)
                status,body=request('GET','/api/v1/review')
                self.assertEqual(status,200)
                self.assertEqual(len(json.loads(body)['assignment']),1)
                service.stop.set()
                self.assertEqual(request('POST','/api/v1/review/assignments',assignment)[0],503)
            finally:
                httpd.shutdown();thread.join();httpd.server_close();service.close()
