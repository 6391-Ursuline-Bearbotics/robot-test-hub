"""Execute actual run-page progress logic: stale data cannot replace saved evidence."""
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT=Path(__file__).resolve().parents[1]
NODE=shutil.which('node')
HARNESS=r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const html=fs.readFileSync(process.argv[1],'utf8'),source=html.match(/<script>([\s\S]*?)<\/script>/)[1];
class Element{constructor(){this.children=[];this.value='';this.style={};this.disabled=false;this.hidden=false;this._text='';}append(...v){this.children.push(...v)}replaceChildren(...v){this.children=v;this._text=''}set textContent(v){this._text=String(v);this.children=[]}get textContent(){return this._text+this.children.map(v=>v.textContent).join(' ')}setAttribute(){}scrollIntoView(){}}
const sha=n=>String(n).padStart(64,'0'),settle=async()=>{for(let i=0;i<8;i++)await new Promise(r=>setImmediate(r));};
function browser(){
 const nodes=new Map(),handlers={},calls=[],controls={defer:false,pending:[],fail:false,mode:'pending'};
 const el=id=>{if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id)};el('zone').value='UTC';
 function job(id,run,mode){return {stream_id:sha(id),run_id:run,generation:'1',state:mode==='ready'?'ready':mode==='failed'?'failed':'verifying',phase:mode==='ready'?'complete':mode==='failed'?'failed':'raw_verify',source_type:'synthetic',source_count:'1',source_ordinal:'0',verified_source_count:mode==='ready'?'1':'0',verified_bytes:'9007199254740993',replay_bytes:'12',scanned_rows:'250002',selected_rows:'50001',selected_bytes:'3000000',chunk_count:'3',spool_bytes:'3000100',created_utc_ns:'1',updated_utc_ns:'2',reason:mode==='failed'?'stream_processing_failed':null,report_id:mode==='ready'?sha(99):null,report_available:mode==='ready',source_verification_complete:mode==='ready'};}
 function progress(url){const parsed=new URL(url,'http://127.0.0.1'),run=parsed.pathname.split('/')[4],offset=Number(parsed.searchParams.get('offset')),items=Array.from({length:Math.min(5,7-offset)},(_,i)=>job(offset+i+1,run,controls.mode));return {schema_version:1,run_id:run,availability:'available',reason:null,items,total:7,returned:items.length,limit:5,offset,next_offset:offset+5<7?offset+5:null,basis:'live_worker_progress'};}
 async function fetch(url){calls.push(url);if(url.includes('/stream-progress')){const d=progress(url),failed=controls.fail;if(controls.mode==='wrongrun')d.run_id='other';if(controls.mode==='badcount')d.items[0].scanned_rows='9007199254740993.0';if(controls.mode==='badreport')d.items[0].report_available=true;const respond=()=>{if(failed)throw Error('offline');return {ok:true,json:async()=>d}};return controls.defer?new Promise((resolve,reject)=>controls.pending.push(()=>{try{resolve(respond())}catch(e){reject(e)}})):respond();}
 const parsed=new URL(url,'http://127.0.0.1'),run=parsed.pathname.split('/')[4];
 if(parsed.pathname==='/api/v1/runs')return {ok:true,json:async()=>({items:[],total:0,revision:sha(1),next_offset:null})};
 if(parsed.pathname.endsWith('/reports'))return {ok:true,json:async()=>({schema_version:1,run_id:run,snapshot_id:sha(3),items:[],total:0,returned:0,limit:5,first_cursor:null,previous_cursor:null,next_cursor:null})};
 if(parsed.pathname.endsWith('/annotations'))return {ok:true,json:async()=>({schema_version:1,run_id:run,run_catalog_revision:sha(1),annotation_watermark:'0',availability:'available',reason:null,annotations:[],total:0,returned:0,limit:20,next_cursor:null,candidate_basis:'intended_utc_overlap_any_mapped_interval',clock_uncertainty_applied:false})};
 return {ok:true,json:async()=>({run:{run_id:run,robot_id:'robot',boot_id:'boot',runtime_mode:'SIM',completeness:'complete',start_monotonic_ns:'1',end_monotonic_ns:'2',segment_ids:[]},annotations_display:{schema_version:1,run_id:run,run_catalog_revision:sha(1)}})};
 }
 const window={addEventListener(name,fn){handlers[name]=fn}},context={document:{getElementById:el,createElement:()=>new Element(),createElementNS:()=>new Element()},window,fetch,AbortController,setTimeout,clearTimeout,URLSearchParams,console};
 vm.runInNewContext(source+'\n;globalThis.ui={detail,loadStreamProgress,resetStreamProgress,active:()=>activeRun};',context);
 return {el,controls,calls,ui:context.ui,offline(){handlers.offline()}};
}
(async()=>{
"""

@unittest.skipUnless(NODE,'Optional Node browser-logic execution is unavailable')
class StreamProgressUITests(unittest.TestCase):
    def run_js(self,body):
        result=subprocess.run([NODE,'-e',HARNESS+body+'\n})().catch(e=>{console.error(e);process.exitCode=1});',str(ROOT/'robot_test_hub/static/runs.html')],capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
    def test_pending_exact_large_counts_and_live_paging_do_not_claim_completed_report(self):
        self.run_js("""const b=browser();await b.ui.detail('run_one');await settle();assert.match(b.el('stream-items').textContent,/9007199254740993 bytes verified/);assert.match(b.el('stream-items').textContent,/No completed report/);assert.match(b.el('stream-status').textContent,/live view/);await b.el('stream-next').onclick();await settle();assert.match(b.calls.at(-1),/offset=5/);assert.match(b.el('stream-status').textContent,/2 shown/);assert.equal(b.el('stream-next').disabled,true);assert.equal(b.ui.active(),'run_one');""")
    def test_ready_and_failed_are_separate_and_refresh_never_changes_saved_note_snapshot(self):
        self.run_js("""const b=browser();await b.ui.detail('run_one');await settle();const notes=b.el('notes-status').textContent,noteCalls=b.calls.filter(x=>x.includes('/annotations')).length;b.controls.mode='ready';await b.el('stream-refresh').onclick();await settle();assert.match(b.el('stream-items').textContent,/completed report is saved/);b.controls.mode='failed';await b.el('stream-refresh').onclick();await settle();assert.match(b.el('stream-items').textContent,/could not complete/);assert.equal(b.el('notes-status').textContent,notes);assert.equal(b.calls.filter(x=>x.includes('/annotations')).length,noteCalls);assert.equal(b.ui.active(),'run_one');""")
    def test_invalid_and_failed_progress_clears_only_progress_and_keeps_manual_retry(self):
        self.run_js("""for(const mode of ['wrongrun','badcount','badreport','network']){const b=browser();await b.ui.detail('run_one');await settle();const notes=b.el('notes-status').textContent;b.controls.mode=mode;b.controls.fail=mode==='network';await b.el('stream-refresh').onclick();await settle();assert.equal(b.el('stream-items').textContent,'');assert.match(b.el('stream-status').textContent,/unavailable/);assert.equal(b.ui.active(),'run_one');assert.equal(b.el('notes-status').textContent,notes);assert.equal(b.el('stream-refresh').disabled,false);assert.equal(b.el('stream-next').disabled,true);}""")
    def test_late_progress_does_not_repopulate_other_run_or_offline_view(self):
        self.run_js("""const b=browser();b.controls.defer=true;await b.ui.detail('run_one');await settle();await b.ui.detail('run_two');await settle();const first=b.controls.pending.shift();first();await settle();assert.equal(b.el('stream-items').textContent,'');b.controls.pending.shift()();await settle();assert.match(b.el('stream-status').textContent,/7 analysis jobs/);assert.equal(b.ui.active(),'run_two');b.controls.pending=[];const pending=b.ui.loadStreamProgress(0);await settle();b.offline();b.controls.pending.shift()();await pending;await settle();assert.equal(b.el('stream-items').textContent,'');assert.equal(b.ui.active(),null);assert.equal(b.el('stream-refresh').disabled,true);""")

if __name__=='__main__':unittest.main()
