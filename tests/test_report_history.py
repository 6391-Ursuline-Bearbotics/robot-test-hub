"""Bounded read-only history and trace receipts using public invented reports."""
from contextlib import closing
import hashlib
import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from robot_test_hub import report_api,run_api
from robot_test_hub.config import Config
from robot_test_hub.demo import DemoSource
from robot_test_hub.server import create_http_server
from robot_test_hub.service import HubService
from robot_test_hub.storage import open_catalog


def identity(value):return hashlib.sha256(str(value).encode()).hexdigest()


class ReportHistoryTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.db=open_catalog(self.root);self.addCleanup(self.db.close)
        self.run=identity('run');self.other=identity('other-run')
        self.trace=[dict(start_monotonic_ns=str(10**18+i*30000000),end_monotonic_ns=str(10**18+i*30000000+10000000),
            weight_seconds=.01,drive_error_mps=.9 if i>=801 else .02,steering_error_rad=0.,
            source_reference={'source_hash':identity('original'),'command_record_index':i+100}) for i in range(1207)]
        self.original={}
        for i in range(9):self.insert(i,created=100+i//3)
        self.db.commit()

    def insert(self,number,*,created=200,run=None):
        report=identity('report-'+str(number));run=run or self.run
        intervals=[[t['start_monotonic_ns'],t['end_monotonic_ns']] for t in self.trace]
        check=dict(analyzer_id='swerve-tracking',analyzer_version='3',job_id=identity('job-'+str(number)),
            outcome='finding',metrics=[],unavailable=[],configuration={},
            provenance={'context':{'run_id':run,'analysis_plan':{'id':'explicit-plan','revision':number+1}}},
            findings=[{'kind':'invented-late-error','intervals_ns':intervals,'source_references':[{'record_index':i} for i in range(1207)]}],
            coverage={'modules':[dict(module_id='physical-module-1',module_position='front-left',evidence_trace=self.trace)]})
        value=dict(report_id=report,run_id=run,framework_version='test-generated',source_type='simulation',
            source_hashes=[identity('original')],checks=[check],evaluated_checks=1,unavailable_checks=0,finding_count=1,overall_health='not_assessed')
        body=json.dumps(value,sort_keys=True,separators=(',',':'))
        self.db.execute('INSERT INTO analysis_reports VALUES (?,?,?,?)',(report,'{}',body,str(created)))
        self.original[report]=body
        return report

    def history(self,**query):return report_api.get(self.db,f'/api/v1/runs/{self.run}/reports',query)

    def test_all_versions_snapshot_ties_midpage_inserts_previous_and_first(self):
        expected=[r[0] for r in self.db.execute('SELECT report_id FROM analysis_reports ORDER BY CAST(created_utc_ns AS INTEGER) DESC,report_id DESC')]
        first=self.history(limit='2');ids=[r['report_id'] for r in first['items']]
        self.assertEqual(first['total'],9);self.assertIsNone(first['previous_cursor'])
        for i,created in ((20,1000),(21,101),(22,1)):self.insert(i,created=created)
        self.db.commit();cursor=first['next_cursor'];last=first
        while cursor:
            last=self.history(limit='2',cursor=cursor)
            self.assertEqual(last['snapshot_id'],first['snapshot_id']);self.assertEqual(last['total'],9)
            ids.extend(r['report_id'] for r in last['items']);cursor=last['next_cursor']
        self.assertEqual(ids,expected);self.assertEqual(len(set(ids)),9)
        back=self.history(limit='2',cursor=last['previous_cursor'])
        self.assertEqual([r['report_id'] for r in back['items']],expected[-3:-1])
        restarted=self.history(limit='2',cursor=last['first_cursor'])
        self.assertEqual(restarted['items'],first['items']);self.assertEqual(self.history()['total'],12)

    def test_metadata_has_no_metrics_traces_or_context(self):
        page=self.history(limit='50');self.assertEqual(page['returned'],9)
        for item in page['items']:
            self.assertNotIn('metrics',item);self.assertNotIn('provenance',item)
            self.assertEqual(set(item['checks'][0]),{'analyzer_id','analyzer_version','job_id','outcome'})

    def test_full_trace_pages_late_outlier_and_immutable_hash_after_restart(self):
        report=next(iter(self.original));route=f'/api/v1/runs/{self.run}/reports/{report}'
        detail=report_api.get(self.db,route,{})
        digest=hashlib.sha256(self.original[report].encode()).hexdigest()
        self.assertEqual(detail['result_sha256'],digest)
        module=detail['report']['checks'][0]['coverage']['modules'][0]
        self.assertEqual(len(module['evidence_trace']),500);self.assertEqual(module['evidence_trace_total'],1207)
        finding=detail['report']['checks'][0]['findings'][0]
        for key in ('intervals_ns','source_references'):
            self.assertEqual(len(finding[key]),500);self.assertEqual(finding[key+'_total'],1207);self.assertTrue(finding[key+'_truncated'])
        query=dict(analyzer_id='swerve-tracking',module_id='physical-module-1',offset='500',limit='500')
        page=report_api.get(self.db,route+'/traces',query)
        self.assertEqual(page['evidence_trace'],self.trace[500:1000]);self.assertEqual(page['next_offset'],1000)
        self.assertEqual(page['result_sha256'],digest);self.assertEqual(page['module_position'],'front-left')
        query['offset']='1000';tail=report_api.get(self.db,route+'/traces',query)
        self.assertEqual(tail['returned'],207);self.assertIsNone(tail['next_offset'])
        self.assertEqual(tail['evidence_trace'],self.trace[1000:]);self.assertEqual(tail['result_sha256'],digest)
        self.insert(99);self.db.commit()
        with closing(sqlite3.connect(self.root/'catalog.sqlite3')) as restarted:
            self.assertEqual(report_api.get(restarted,route,{}),detail)
        for report,raw in self.original.items():self.assertEqual(self.db.execute('SELECT result_json FROM analysis_reports WHERE report_id=?',(report,)).fetchone()[0],raw)

    def test_sql_only_extracts_selected_trace_page_before_python(self):
        report=next(iter(self.original));queries=[];self.db.set_trace_callback(queries.append)
        report_api.traces(self.db,self.run,report,dict(analyzer_id='swerve-tracking',module_id='physical-module-1',offset='1000',limit='10'))
        self.db.set_trace_callback(None)
        self.assertFalse(any(q.upper().startswith('SELECT RESULT_JSON') for q in queries))
        pages=[q for q in queries if 'SELECT t.type,CASE' in q]
        self.assertEqual(len(pages),1);self.assertIn('t.key>=1000',pages[0]);self.assertIn('LIMIT 10',pages[0])

    def test_reject_wrong_scope_invalid_queries_and_malformed_stored_shapes(self):
        first=self.history(limit='2');report=next(iter(self.original))
        for query in ({'cursor':'broken'},{'limit':'51'},{'limit':'01'},{'unknown':'private/path'}, {'limit':'-1'}):
            with self.subTest(query=query),self.assertRaises(ValueError):self.history(**query)
        with self.assertRaises(ValueError):report_api.history(self.db,self.other,{'cursor':first['next_cursor']})
        with self.assertRaises(KeyError):report_api.detail(self.db,self.other,report)
        for query in ({'analyzer_id':'private/path','module_id':'physical-module-1'},
                      {'analyzer_id':'swerve-tracking','module_id':'physical-module-1','offset':'1208'},
                      {'analyzer_id':'swerve-tracking','module_id':'physical-module-1','limit':'501'}):
            with self.subTest(query=query),self.assertRaises(ValueError):report_api.traces(self.db,self.run,report,query)
        with self.assertRaises(KeyError):report_api.traces(self.db,self.run,report,dict(analyzer_id='swerve-tracking',module_id='wrong-module'))
        malformed=json.loads(self.original[report]);malformed['checks'][0]['coverage']=None
        self.db.execute('UPDATE analysis_reports SET result_json=? WHERE report_id=?',(json.dumps(malformed),report))
        with self.assertRaises(ValueError):report_api.get(self.db,f'/api/v1/runs/{self.run}/reports/{report}',{})

    def test_actual_http_repeated_query_private_errors_and_pinned_detail(self):
        self.db.commit();source=DemoSource();service=HubService(Config(data_dir=str(self.root)),source);self.addCleanup(service.close)
        server=create_http_server(service,source,0);thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01});thread.start()
        self.addCleanup(lambda:(server.shutdown(),thread.join(2),server.server_close()))
        def request(path):
            connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3)
            try:
                connection.request('GET',path);response=connection.getresponse();return response.status,json.loads(response.read())
            finally:connection.close()
        path=f'/api/v1/runs/{self.run}/reports'
        status,page=request(path+'?limit=2');self.assertEqual(status,200);self.assertEqual(page['returned'],2)
        status,detail=request(path+'/'+page['items'][0]['report_id']);self.assertEqual(status,200);self.assertIn('result_sha256',detail)
        status,error=request(path+'?limit=2&limit=3');self.assertEqual(status,400)
        status,error=request(path+'?cursor=private-credential-path');self.assertEqual(status,400)
        self.assertNotIn('private-credential-path',json.dumps(error))

    def test_oversized_record_rejected_in_sql_before_python_trace_decode(self):
        report=next(iter(self.original));value=json.loads(self.original[report])
        value['checks'][0]['coverage']['modules'][0]['evidence_trace'][1000]['oversized']='x'*20000
        self.db.execute('UPDATE analysis_reports SET result_json=? WHERE report_id=?',(json.dumps(value),report))
        actual=report_api._json
        def bounded_decode(raw):
            self.assertNotIn('x'*20000,raw if isinstance(raw,str) else raw.decode())
            return actual(raw)
        query=dict(analyzer_id='swerve-tracking',module_id='physical-module-1',offset='1000',limit='1')
        with patch.object(report_api,'_json',bounded_decode),self.assertRaisesRegex(ValueError,'malformed_report_trace'):
            report_api.traces(self.db,self.run,report,query)
        with patch.object(report_api,'MAX_RESULT_BYTES',10),self.assertRaisesRegex(ValueError,'oversized_report'):
            report_api.detail(self.db,self.run,report)


if __name__=='__main__':unittest.main()
