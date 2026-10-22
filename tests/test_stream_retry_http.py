"""Explicit local HTTP retries queue exact stream attempts, never source work."""
from contextlib import closing
import http.client
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch

from robot_test_hub.config import Config
from robot_test_hub.importer import Importer
from robot_test_hub import reports, stream_api, stream_analysis
from robot_test_hub.runs import rebuild_from_imports
from robot_test_hub.server import create_http_server
from robot_test_hub.service import HubService
from robot_test_hub.storage import open_catalog
from test_processing_http import GeneratedRecordingSource
from test_stream_ui import HARNESS, NODE, ROOT

PUBLIC=Path(__file__).parent/'fixtures/synthetic/alpha7-main.wpilog'


class StreamRetryHttpTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(prefix='stream retry http ');self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        with closing(open_catalog(self.root)) as db:
            Importer(self.root,db).import_file(PUBLIC,source_type='SYNTHETIC')
            document=rebuild_from_imports(self.root,db)
            document={**document,'runs':document['runs'][:1]}
            self.run=document['runs'][0]['run_id']
            scheduled=reports.generate(self.root,db,document,pipeline_version='automatic-reports-8')[0]
            self.stream=scheduled['stream_id']
            runner=stream_analysis.StreamRunner(self.root,db,reducer=__import__('robot_test_hub.stream_quality',fromlist=['RecordingQualityReducer']).RecordingQualityReducer())
            runner.cancel(self.stream);self.assertTrue(runner.close())
            self.job=dict(db.execute('SELECT * FROM analysis_stream_jobs WHERE stream_id=?',(self.stream,)).fetchone())
        self.source=GeneratedRecordingSource()
        self.service=HubService(Config(data_dir=str(self.root)),self.source)
        self.addCleanup(self.service.close)
        self.service._health('indexer','running')
        self.server=create_http_server(self.service,self.source,0)
        self.server.daemon_threads=False
        self.thread=threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':.01});self.thread.start()
        self.addCleanup(lambda:(self.server.shutdown(),self.thread.join(3),self.server.server_close()))
        self.origin='http://127.0.0.1:'+str(self.server.server_port)
        self.path='/api/v1/runs/'+self.run+'/streams/'+self.stream+'/retry'

    def payload(self):
        return dict(request_id=str(uuid.uuid4()),stream_id=self.stream,expected_generation=str(self.job['generation']),
                    expected_state=self.job['state'],expected_updated_utc_ns=str(self.job['updated_utc_ns']))

    def request(self,method,path,payload=None,headers=None,raw=None):
        body=raw if raw is not None else json.dumps(payload).encode() if payload is not None else None
        settings={'Content-Type':'application/json','X-Hub-Request':'1','Origin':self.origin}
        if headers:settings.update(headers)
        connection=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
        try:
            connection.request(method,path,body=body,headers=settings)
            response=connection.getresponse();return response.status,json.loads(response.read())
        finally:connection.close()

    def post(self,payload=None,**options):return self.request('POST',self.path,payload if payload is not None else self.payload(),**options)
    def progress(self):return self.request('GET','/api/v1/runs/'+self.run+'/stream-progress')

    def test_queue_only_duplicate_and_stale_pins_and_readonly_refresh(self):
        payload=self.payload()
        with patch.object(stream_analysis.StreamRunner,'_start',side_effect=AssertionError('HTTP started source work')),\
             patch.object(self.source,'status',side_effect=AssertionError('HTTP polled robot')),\
             patch.object(self.source,'read',side_effect=AssertionError('HTTP read robot')):
            status,first=self.post(payload);self.assertEqual(status,202,first)
            status,again=self.post(payload);self.assertEqual(status,202,again)
            self.assertEqual(first['request']['id'],again['request']['id'])
            status,dedup=self.post(dict(payload,request_id=str(uuid.uuid4())));self.assertEqual(status,202,dedup)
            self.assertEqual(first['request']['id'],dedup['request']['id'])
            status,page=self.progress();self.assertEqual(status,200,page)
        item=page['items'][0]
        self.assertEqual((item['generation'],item['state']),('1','canceled'))
        self.assertEqual(item['retry_requests'][0]['state'],'pending')
        self.assertEqual(item['previous_attempt_total'],0)
        self.assertNotIn('context_json',json.dumps(page));self.assertNotIn('pins_json',json.dumps(page))
        with closing(open_catalog(self.root)) as db:
            before=db.execute('SELECT COUNT(*) FROM analysis_stream_retry_requests').fetchone()[0]
        for _ in range(3):self.assertEqual(self.progress()[0],200)
        with closing(open_catalog(self.root)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM analysis_stream_retry_requests').fetchone()[0],before)
            self.assertEqual(db.execute('SELECT state FROM analysis_stream_jobs').fetchone()[0],'canceled')
        stale=dict(payload,request_id=str(uuid.uuid4()),expected_updated_utc_ns=str(int(payload['expected_updated_utc_ns'])+1))
        self.assertEqual(self.post(stale)[0],409)
        changed=dict(payload,expected_state='failed')
        self.assertEqual(self.post(changed)[0],409)

    def test_eligibility_is_narrow_and_published_attempt_is_never_retried(self):
        for code in ('stream_source_changed','stream_source_hash_mismatch','stream_schema_invalid','stream_checkpoint_invalid','stream_inventory_invalid'):
            with self.subTest(code=code),closing(open_catalog(self.root)) as db:
                with db:db.execute("UPDATE analysis_stream_jobs SET state='failed',phase='failed',error_code=?",(code,))
                status,page=self.progress();self.assertEqual(status,200)
                self.assertFalse(page['items'][0]['retry']['eligible'])
                self.assertEqual(page['items'][0]['retry']['reason'],'error_not_retryable')
                self.assertEqual(self.post(dict(self.payload(),expected_state='failed'))[0],409)
        with closing(open_catalog(self.root)) as db:
            with db:db.execute("UPDATE analysis_stream_jobs SET error_code='stream_spool_resource_limit',report_id=?",('f'*64,))
        self.assertEqual(self.post(dict(self.payload(),expected_state='failed'))[0],409)

    def test_auth_strict_body_stopping_and_failed_indexer_never_queue(self):
        for headers in ({'Origin':'http://untrusted.invalid'},{'X-Hub-Request':'0'},{'Content-Type':'text/plain'}):
            self.assertEqual(self.post(headers=headers)[0],403)
        payload=self.payload()
        for changed in (dict(payload,extra='forbidden'),dict(payload,stream_id='f'*64),dict(payload,expected_generation=1),
                        dict(payload,expected_generation='01'),dict(payload,expected_generation='9223372036854775808'),
                        dict(payload,expected_updated_utc_ns=1),dict(payload,expected_state='ready')):
            self.assertEqual(self.post(changed)[0],400)
        duplicate=json.dumps(payload)[:-1]+',"expected_state":"canceled"}'
        for raw in (duplicate.encode(),b'{}',b'x'*4097):self.assertEqual(self.post(raw=raw)[0],400)
        self.service._health('indexer','failed')
        self.assertEqual(self.post()[0],503)
        self.service._health('indexer','running');self.service.stop.set()
        self.assertEqual(self.post()[0],503)
        with closing(open_catalog(self.root)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM analysis_stream_retry_requests').fetchone()[0],0)

    def test_claim_displays_old_failure_and_new_attempt_separately_without_private_documents(self):
        from robot_test_hub import stream_retry
        status,receipt=self.post();self.assertEqual(status,202,receipt)
        with closing(open_catalog(self.root)) as db:
            runner=reports.StreamReportRunner(self.root,db,limits={'min_free_bytes':0})
            try:
                claimed=stream_retry.step(db,runner)
                self.assertEqual(claimed['state'],'running')
            finally:self.assertTrue(runner.close())
            denied=[]
            def authorize(action,table,column,*unused):
                if action in (sqlite3.SQLITE_INSERT,sqlite3.SQLITE_UPDATE,sqlite3.SQLITE_DELETE):denied.append((table,column));return sqlite3.SQLITE_DENY
                if action==sqlite3.SQLITE_READ and column in ('job_json','context_json','run_json','descriptor_json','pins_json','attempt_sha256'):
                    denied.append((table,column));return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            db.set_authorizer(authorize)
            try:
                page=stream_api.page(db,self.run,{})
            finally:db.set_authorizer(lambda *unused:sqlite3.SQLITE_OK)
            self.assertEqual(denied,[])
        self.assertEqual(page['availability'],'available',page)
        item=page['items'][0]
        self.assertEqual((item['generation'],item['state']),('2','pending'))
        self.assertFalse(item['retry']['eligible'])
        self.assertEqual(item['previous_attempt_total'],1)
        self.assertEqual((item['previous_attempts'][0]['generation'],item['previous_attempts'][0]['state']),('1','canceled'))
        self.assertEqual(item['retry_requests'][0]['result_generation'],'2')
        self.assertEqual(item['retry_requests'][0]['state'],'running')

    def test_resource_failure_is_eligible_and_retry_schema_failure_stays_unknown(self):
        with closing(open_catalog(self.root)) as db:
            with db:db.execute("UPDATE analysis_stream_jobs SET state='failed',phase='failed',error_code='stream_free_space_unavailable'")
        status,page=self.progress();self.assertEqual(status,200,page)
        self.assertTrue(page['items'][0]['retry']['eligible'])
        self.assertEqual(page['items'][0]['retry']['pins']['expected_generation'],'1')
        with closing(open_catalog(self.root)) as db:
            with db:db.execute('DROP TRIGGER analysis_stream_history_update')
        status,page=self.progress();self.assertEqual(status,200,page)
        self.assertEqual(page['availability'],'unavailable')
        self.assertIsNone(page['total']);self.assertEqual(page['items'],[])


@unittest.skipUnless(NODE,'Optional Node browser-logic execution is unavailable')
class StreamRetryUITests(unittest.TestCase):
    def test_explicit_click_and_uncertain_response_reuse_request_id_refresh_only_gets(self):
        setup=r"""
for(const item of items){
 item.state='failed';item.phase='failed';item.reason='stream_spool_resource_limit';
 item.retry_availability='available';item.retry={eligible:true,reason:'eligible',pins:{stream_id:item.stream_id,expected_generation:item.generation,expected_state:item.state,expected_updated_utc_ns:item.updated_utc_ns}};
 item.previous_attempts=[];item.previous_attempt_total=0;item.retry_requests=[];item.retry_request_total=0;item.attempt_display_limit=3;
 if(controls.claimed){item.previous_attempts=[{stream_id:item.stream_id,run_id:run,generation:'1',state:'failed',reason:'stream_spool_resource_limit',updated_utc_ns:'2'}];item.previous_attempt_total=1;item.generation='2';item.state='pending';item.phase='scheduled';item.reason=null;item.retry={eligible:false,reason:'state_not_retryable',pins:null};}
}
"""
        post=r"""
if(url.endsWith('/retry')){controls.posts.push(JSON.parse(options.body));if(controls.requestFail)throw Error('uncertain response');const p=JSON.parse(options.body);return {ok:true,json:async()=>({schema_version:1,request:{id:p.request_id,stream_id:p.stream_id,run_id:url.split('/')[4],expected_generation:p.expected_generation,expected_state:p.expected_state,expected_updated_utc_ns:p.expected_updated_utc_ns,state:'pending',error_code:null,created_utc_ns:'3',updated_utc_ns:'3',result_generation:null,result_state:null,result_updated_utc_ns:null}})};}
"""
        harness=HARNESS.replace("mode:'pending'","mode:'pending',posts:[],requestFail:false,claimed:false")
        harness=harness.replace("return {schema_version:1,run_id:run,availability:'available'",setup+"return {schema_version:1,run_id:run,availability:'available'")
        harness=harness.replace('async function fetch(url){calls.push(url);','async function fetch(url,options={}){calls.push(url);'+post)
        harness=harness.replace('context={document:','context={crypto:{randomUUID:()=>"01234567-89ab-cdef-0123-456789abcdef"},document:')
        body=r"""
function find(root,text){if(root.textContent===text)return root;for(const child of root.children||[]){const value=find(child,text);if(value)return value;}return null;}
const b=browser();await b.ui.detail('run_one');await settle();
assert.equal(b.controls.posts.length,0);await b.el('stream-refresh').onclick();await settle();assert.equal(b.controls.posts.length,0);
let button=find(b.el('stream-items'),'Request fresh analysis attempt');assert.ok(button);
b.controls.requestFail=true;await button.onclick();assert.match(b.el('stream-items').textContent,/request outcome is unavailable/);assert.equal(button.disabled,false);
b.controls.requestFail=false;await button.onclick();assert.equal(b.controls.posts.length,2);assert.equal(b.controls.posts[0].request_id,b.controls.posts[1].request_id);assert.equal(button.disabled,true);
assert.match(b.el('stream-items').textContent,/saved · pending/);assert.match(b.el('stream-items').textContent,/Current attempt 1/);
b.controls.claimed=true;await b.el('stream-refresh').onclick();await settle();assert.equal(b.controls.posts.length,2);assert.match(b.el('stream-items').textContent,/Current attempt 2/);assert.match(b.el('stream-items').textContent,/Previous attempt 1 · failed/);assert.equal(find(b.el('stream-items'),'Request fresh analysis attempt'),null);
"""
        result=subprocess.run([NODE,'-e',harness+body+'\n})().catch(e=>{console.error(e);process.exitCode=1});',str(ROOT/'robot_test_hub/static/runs.html')],capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)


if __name__=='__main__':unittest.main()
