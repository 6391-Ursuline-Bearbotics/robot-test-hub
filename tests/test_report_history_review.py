"""Independent HTTP acceptance with synthetic immutable history and late-fault traces."""
import copy
import http.client
import hashlib
import json
import unittest
import threading
from urllib.parse import urlencode
from unittest.mock import patch
import test_auto_swerve_review as fixture_module
from robot_test_hub import report_api


class ReportHistoryReviewTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixture_module.AutoSwerveReviewTests()
        self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        f=self.fixture;f.import_recording();f.assignments();f.store.record_analysis_plan(f.plan);f.pipeline.tick()
        self.base=f.reports()[0];self.run=self.base['run_id'];self.prefix='/api/v1/runs/'+self.run+'/reports'
        self.original_rows=[tuple(r) for r in f.db.execute('SELECT * FROM analysis_reports')]
        self.raw_before={p:p.read_bytes() for p in (f.root/'raw').rglob('*.wpilog')}
        self.document=copy.deepcopy(self.base)
        check=next(c for c in self.document['checks'] if c['analyzer_id']=='swerve-tracking')
        module=check['coverage']['modules'][0];self.module=module['module_id'];self.analyzer=check['analyzer_id']
        template=module['evidence_trace'][0];trace=[];clock=1000000000
        for i in range(1207):
            # Independently derived irregular durations and a true gap. The late
            # incident is outside both initial 500-interval display pages.
            if i==900:clock+=300000000
            dt=20000000 if i%2 else 70000000
            item=copy.deepcopy(template);item.update(start_monotonic_ns=str(clock),end_monotonic_ns=str(clock+dt),
                weight_seconds=dt/1e9,drive_error_mps=3.25 if i==1103 else .125,steering_error_rad=-.4 if i==1103 else 0.)
            item['source_reference']['command_record_index']=i+10000
            trace.append(item);clock+=dt
        module['evidence_trace']=trace
        check['findings']=[dict(kind='independent_late_error',severity='warning',intervals_ns=[[t['start_monotonic_ns'],t['end_monotonic_ns']] for t in trace],
            source_references=[t['source_reference'] for t in trace],observed_behavior='Generated late fault at interval1103')]
        self.trace=trace;self.stored={}
        # Nine tied creation timestamps exercise report ID as the second key.
        for i in range(9):self.insert('review-history-'+str(i),1800000000000000000)
        self.expected=[r[0] for r in f.db.execute('SELECT report_id FROM analysis_reports ORDER BY CAST(created_utc_ns AS INTEGER) DESC,report_id DESC')]
        self.server=None;self.addCleanup(self.stop_http);self.start_http()

    def start_http(self):
        source=fixture_module.DemoSource()
        self.service=fixture_module.HubService(fixture_module.Config(data_dir=str(self.fixture.root)),source)
        self.server=fixture_module.create_http_server(self.service,source,0)
        self.thread=threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':.01});self.thread.start()

    def stop_http(self):
        if self.server is None:return
        self.server.shutdown();self.thread.join(2);self.server.server_close()
        self.assertFalse(self.thread.is_alive());self.assertTrue(self.service.close());self.server=None

    def request(self,method,path):
        connection=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=3)
        try:
            connection.request(method,path)
            response=connection.getresponse()
            return response.status,json.loads(response.read())
        finally:connection.close()

    def insert(self,identity,created):
        document=copy.deepcopy(self.document);document['report_id']=identity
        raw=json.dumps(document,ensure_ascii=False,separators=(',',':'))
        with self.fixture.db:self.fixture.db.execute('INSERT INTO analysis_reports VALUES (?,?,?,?)',(identity,'{}',raw,str(created)))
        self.stored[identity]=raw
        return identity

    def get(self,suffix='',query=None):
        return self.request('GET',self.prefix+suffix+('?' + urlencode(query) if query else ''))

    def test_tied_history_snapshot_excludes_all_midpage_insertions_and_reaches_oldest(self):
        status,first=self.get(query={'limit':'3'});self.assertEqual(status,200,first)
        self.assertEqual(first['total'],10);self.assertEqual(first['returned'],3)
        self.assertEqual([r['report_id'] for r in first['items']],self.expected[:3])
        for identity,stamp in [('mid-new',1900000000000000000),('mid-tied',1800000000000000000),('mid-old',1000)]:self.insert(identity,stamp)
        seen=[r['report_id'] for r in first['items']];page=first
        while page['next_cursor']:
            status,page=self.get(query={'limit':'3','cursor':page['next_cursor']});self.assertEqual(status,200,page)
            self.assertEqual(page['snapshot_id'],first['snapshot_id']);self.assertEqual(page['total'],10)
            seen.extend(r['report_id'] for r in page['items'])
        self.assertEqual(seen,self.expected);self.assertEqual(len(set(seen)),10)
        status,back=self.get(query={'limit':'3','cursor':page['previous_cursor']});self.assertEqual(status,200,back)
        self.assertEqual([r['report_id'] for r in back['items']],self.expected[6:9])
        status,again=self.get(query={'limit':'3','cursor':first['first_cursor']});self.assertEqual(status,200)
        self.assertEqual(again,first)
        self.assertEqual(self.get(query={'limit':'50'})[1]['total'],13)

    def test_late_fault_exact_hash_count_receipts_and_sql_projection(self):
        identity='review-history-8';raw=self.stored[identity];digest=hashlib.sha256(raw.encode()).hexdigest()
        real_loads=json.loads
        def bounded_load(value,*args,**kwargs):
            if threading.current_thread() is not threading.main_thread() and isinstance(value,(str,bytes)):
                self.assertLess(len(value),150000,'Whole persisted trace JSON reached Python')
            return real_loads(value,*args,**kwargs)
        with patch.object(report_api.json,'loads',side_effect=bounded_load):
            status,detail=self.get('/'+identity)
        self.assertEqual(status,200,detail);self.assertEqual(detail['result_sha256'],digest)
        check=next(c for c in detail['report']['checks'] if c['analyzer_id']==self.analyzer)
        m=check['coverage']['modules'][0]
        self.assertEqual((len(m['evidence_trace']),m['evidence_trace_total'],m['evidence_trace_truncated']),(500,1207,True))
        finding=check['findings'][0]
        for key in ('intervals_ns','source_references'):
            self.assertEqual(len(finding[key]),500);self.assertEqual(finding[key+'_total'],1207);self.assertIs(finding[key+'_truncated'],True)
        collected=[];offset=0
        while offset is not None:
            status,page=self.get('/'+identity+'/traces',{'analyzer_id':self.analyzer,'module_id':self.module,'offset':str(offset),'limit':'400'})
            self.assertEqual(status,200,page);self.assertEqual(page['result_sha256'],digest)
            self.assertEqual(page['total'],1207);collected.extend(page['evidence_trace']);offset=page['next_offset']
        self.assertEqual(collected,self.trace)
        self.assertEqual(collected[1103]['drive_error_mps'],3.25)
        self.assertGreater(int(collected[900]['start_monotonic_ns']),int(collected[899]['end_monotonic_ns']))
        self.assertEqual(self.fixture.db.execute('SELECT result_json FROM analysis_reports WHERE report_id=?',(identity,)).fetchone()[0],raw)
        for path,body in self.raw_before.items():self.assertEqual(path.read_bytes(),body)

    def test_historical_receipt_remains_exact_after_review_change_and_http_restart(self):
        identity='review-history-2'
        status,before=self.get('/'+identity);self.assertEqual(status,200,before)
        self.fixture.store.record_maintenance(dict(id='later-boundary',robot_id='review-robot',
            kind='inspection',effective_utc_ns=str(fixture_module.U+1000000000),
            reviewer='Independent reviewer',rationale='New context must not rewrite pinned history'))
        self.fixture.pipeline.tick()
        self.stop_http();self.start_http()
        status,after=self.get('/'+identity);self.assertEqual(status,200,after);self.assertEqual(after,before)
        for row in self.original_rows:
            self.assertEqual(tuple(self.fixture.db.execute('SELECT * FROM analysis_reports WHERE report_id=?',(row[0],)).fetchone()),row)
        for path,body in self.raw_before.items():self.assertEqual(path.read_bytes(),body)

    def test_scope_query_and_malformed_reports_fail_closed(self):
        identity='review-history-8';trace='/'+identity+'/traces';pins={'analyzer_id':self.analyzer,'module_id':self.module}
        for extra in ({'offset':'-1'},{'offset':'01'},{'offset':'1208'},{'limit':'501'},{'limit':'0'},{'unknown':'1'},{'offset':'1.0'}):
            status,body=self.get(trace,{**pins,**extra});self.assertEqual(status,400,body)
        for key in ('analyzer_id','module_id'):
            status,body=self.get(trace,{**pins,key:'different'});self.assertEqual(status,404,body)
        status,body=self.request('GET','/api/v1/runs/different/reports/'+identity);self.assertEqual(status,404,body)
        cursor=self.get(query={'limit':'1'})[1]['next_cursor']
        status,body=self.request('GET','/api/v1/runs/different/reports?'+urlencode({'cursor':cursor}));self.assertEqual(status,400,body)
        status,body=self.request('GET',self.prefix+'?limit=1&limit=2');self.assertEqual(status,400,body)
        with self.fixture.db:self.fixture.db.execute('INSERT INTO analysis_reports VALUES (?,?,?,?)',('malformed','{}','{"run_id":','1'))
        self.assertEqual(self.get('/malformed')[0],400)


if __name__=='__main__':unittest.main()
