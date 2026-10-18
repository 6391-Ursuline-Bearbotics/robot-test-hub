"""Known runs without reports keep selected run and notes visible."""
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from robot_test_hub import report_api,run_lookup
from robot_test_hub.storage import open_catalog

class EmptyReportHistoryTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve();self.db=open_catalog(self.root);self.addCleanup(self.db.close)
        self.document=dict(revision='synthetic-revision',runs=[dict(run_id='synthetic-run',robot_id='synthetic-robot',utc_intervals=[])],mappings=[])
        run_lookup.publish_document(self.db,self.document)
    def test_known_empty_history_is_readonly_with_standard_snapshot_and_cursors(self):
        def authorize(action,table,column,*unused):
            if action in (sqlite3.SQLITE_INSERT,sqlite3.SQLITE_DELETE) or action==sqlite3.SQLITE_UPDATE and table!='sqlite_master':return sqlite3.SQLITE_DENY
            if action==sqlite3.SQLITE_READ and table=='run_catalog_revisions' and column=='document':return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        self.db.set_authorizer(authorize)
        try:
            result=report_api.history(self.db,'synthetic-run',{})
            repeated=report_api.history(self.db,'synthetic-run',{'cursor':result['first_cursor']})
        finally:self.db.set_authorizer(lambda *unused:sqlite3.SQLITE_OK)
        self.assertEqual(result,repeated);self.assertEqual((result['total'],result['returned'],result['items']),(0,0,[]))
        self.assertIsNone(result['next_cursor']);self.assertIsNone(result['previous_cursor']);self.assertTrue(result['first_cursor']);self.assertEqual(len(result['snapshot_id']),64)
    def test_unknown_run_is_missing_and_pending_lookup_is_unavailable(self):
        with self.assertRaises(run_lookup.LookupMissing):report_api.history(self.db,'unknown-run',{})
        with self.db:self.db.execute('UPDATE run_catalog_revisions SET document=document')
        with self.assertRaises(run_lookup.LookupUnavailable) as caught:report_api.history(self.db,'synthetic-run',{})
        self.assertEqual(caught.exception.status,503)
    def test_saved_report_history_remains_reachable_without_catalog_run(self):
        value=dict(report_id='saved-report',run_id='report-only-run',source_type='synthetic',checks=[dict(analyzer_id='recording-quality',analyzer_version='1',job_id='synthetic-job',outcome='insufficient_data',coverage={},provenance={},findings=[],metrics=[],unavailable=[])],evaluated_checks=0,unavailable_checks=1,finding_count=0,overall_health='not_assessed')
        with self.db:self.db.execute('INSERT INTO analysis_reports VALUES(?,?,?,?)',('saved-report','{}',json.dumps(value),'1'))
        result=report_api.history(self.db,'report-only-run',{})
        self.assertEqual(result['total'],1);self.assertEqual(result['items'][0]['report_id'],'saved-report')
    @unittest.skipUnless(shutil.which('node'),'Optional Node browser-logic execution is unavailable')
    def test_empty_history_ui_preserves_active_run_and_existing_notes(self):
        script=r"""
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(process.argv[1],'utf8'),nodes=new Map();
global.el=id=>{if(!nodes.has(id))nodes.set(id,{textContent:'',children:[],replaceChildren(){this.children=[]}});return nodes.get(id)};
global.activeRun='synthetic-run';global.history={run_id:activeRun};global.historySequence=0;global.HISTORY_LIMIT=20;global.SHA=/^[a-f0-9]{64}$/;
global.historyControls=()=>{};global.cursor=value=>value;global.count=(value,max=Number.MAX_SAFE_INTEGER)=>{assert.ok(Number.isInteger(value)&&value>=0&&value<=max);return value};
global.request=async()=>({schema_version:1,run_id:activeRun,items:[],total:0,returned:0,limit:20,next_cursor:null,previous_cursor:null,first_cursor:'saved-snapshot-cursor',snapshot_id:'a'.repeat(64)});
global.add=(node,tag,text)=>{const item={textContent:text};node.children.push(item);return item};let lostCalls=0;global.lost=()=>{lostCalls++;activeRun=null;el('notes').replaceChildren()};
el('notes').children.push({textContent:'Saved note remains visible'});
const start=source.indexOf('async function loadHistory('),end=source.indexOf('async function selectReport(',start);vm.runInThisContext(source.slice(start,end));
(async()=>{await loadHistory(null,true);assert.equal(lostCalls,0);assert.equal(activeRun,'synthetic-run');assert.equal(el('notes').children[0].textContent,'Saved note remains visible');assert.match(el('history-status').textContent,/0 retained versions/);assert.equal(el('history-items').children[0].textContent,'No retained report yet.');console.log('Empty report history preserves run and notes')})().catch(error=>{console.error(error);process.exitCode=1});
"""
        result=subprocess.run([shutil.which('node'),'-e',script,str(Path(__file__).resolve().parents[1]/'robot_test_hub/static/runs.html')],capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

if __name__=='__main__':unittest.main()
