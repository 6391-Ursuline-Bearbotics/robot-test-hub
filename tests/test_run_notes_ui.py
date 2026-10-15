"""Execute the run page's actual JavaScript with bounded saved-note API fixtures."""
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
HARNESS = r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const html=fs.readFileSync(process.argv[1],'utf8'),source=html.match(/<script>([\s\S]*?)<\/script>/)[1];
const sha=n=>String(n).padStart(64,'0');
class Element {
 constructor(tag='div'){this.tagName=tag;this.children=[];this.value='';this.disabled=false;this.hidden=false;this.style={};this._text='';this.listeners={};}
 append(...items){this.children.push(...items);}
 replaceChildren(...items){this._text='';this.children=items;}
 set textContent(value){this._text=String(value);this.children=[];}
 get textContent(){return this._text+this.children.map(n=>n.textContent).join(' ');}
 addEventListener(name,fn){this.listeners[name]=fn;}
 setAttribute(name,value){this[name]=value;}
 scrollIntoView(){}
}
const settle=async()=>{for(let i=0;i<6;i++)await new Promise(resolve=>setImmediate(resolve));};
function browser(){
 const elements=new Map(),listeners={},calls=[];
 const el=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};el('zone').value='UTC';el('minutes').value='2';
 const control={total:2221,watermark:'2221',revision:sha(1),defer:false,pending:[],status:null,invalid:null,missing:false,unavailable:false,duplicates:false,networkFail:false};
 function note(i){return {schema_version:1,event_id:'event_'+i,revision:3,text:i===0?'<img src=x onerror=attack()>':'note '+i,author:'driver',tags:['drive'],clock_quality:'unverified_client',uncertainty_ms:10,event_utc_start_ns:'1000000000',event_utc_end_ns:'2000000000',submitted_utc_ns:'3000000000',hub_received_utc_ns:'4000000000',storage_state:'saved_in_hub',delivery_state:'historical_hub_only',url:'javascript:attack()'};}
 function reply(url){
  const parsed=new URL(url,'http://127.0.0.1'),path=parsed.pathname,parts=path.split('/'),id=decodeURIComponent(parts[4]||'');
  if(path==='/api/v1/time/resolve')return {candidates:[{utc_ns:'2000000000',local:'1970-01-01T00:00:02',timezone:'UTC'}]};
  if(path==='/api/v1/runs')return {items:[],total:0,revision:control.revision,next_offset:null};
  if(path.endsWith('/reports'))return {schema_version:1,run_id:id,snapshot_id:sha(2),items:[],total:0,returned:0,limit:5,first_cursor:null,previous_cursor:null,next_cursor:null};
  if(path.endsWith('/annotations')){
   const cursor=parsed.searchParams.get('cursor'),m=cursor&&/^w(\d+)-p(\d+)$/.exec(cursor),watermark=m?m[1]:control.watermark,total=Number(watermark),offset=m?Number(m[2]):0;
   const count=Math.min(20,Math.max(0,total-offset));
   const d={schema_version:1,run_id:id,run_catalog_revision:control.revision,annotation_watermark:watermark,availability:'available',reason:null,annotations:Array.from({length:count},(_,i)=>note(offset+i)),total,returned:count,limit:20,next_cursor:offset+20<total?'w'+watermark+'-p'+(offset+20):null,candidate_basis:'intended_utc_overlap_any_mapped_interval',clock_uncertainty_applied:false};
   if(control.unavailable)Object.assign(d,{availability:'unavailable',reason:'run_clock_unavailable',annotations:[],total:null,returned:0,next_cursor:null});
   if(control.duplicates&&d.annotations.length>1)d.annotations[1]={...d.annotations[0]};
   if(control.invalid==='run')d.run_id='other_run';
   if(control.invalid==='revision')d.run_catalog_revision=sha(99);
   if(control.invalid==='watermark')d.annotation_watermark='9999';
   if(control.invalid==='receipt')d.annotations[0].delivery_state='robot_acknowledged';
   if(control.invalid==='count')d.returned=21;
   if(control.invalid==='truncated'){d.annotations=d.annotations.slice(0,1);d.returned=1;d.next_cursor=null;}
   return d;
  }
  return {run:{run_id:id,robot_id:'robot',boot_id:'boot',runtime_mode:'SIM',completeness:'complete',start_monotonic_ns:'1',end_monotonic_ns:'2',segment_ids:['segment']},annotations_display:control.missing?undefined:{schema_version:1,run_id:id,run_catalog_revision:control.revision}};
 }
 async function fetch(url,options){
  calls.push({url,options});
  if(url.includes('/annotations')){
   const result=reply(url),status=control.status;
   if(control.networkFail)throw Error('Hub unreachable');
   const respond=()=>({ok:status===null,status:status||200,json:async()=>result});
   if(control.defer)return new Promise(resolve=>control.pending.push(()=>resolve(respond())));
   return respond();
  }
  return {ok:true,json:async()=>reply(url)};
 }
 const window={addEventListener(name,fn){listeners[name]=fn;}},context={document:{getElementById:el,createElement:tag=>new Element(tag),createElementNS:(_ns,tag)=>new Element(tag)},window,fetch,AbortController,setTimeout,clearTimeout,URLSearchParams,console};
 vm.runInNewContext(source+'\n;globalThis.ui={detail,load,lost,request};',context);
 return {el,control,calls,ui:context.ui,click(id){if(!el(id).disabled)return el(id).onclick();},offline(){listeners.offline();}};
}
(async()=>{
"""

@unittest.skipUnless(NODE, "Node is required only for development UI verification")
class RunNotesUITests(unittest.TestCase):
    def run_js(self, body):
        script = HARNESS + body + "\n})().catch(e=>{console.error(e);process.exitCode=1;});"
        result = subprocess.run([NODE, "-e", script, str(ROOT / "robot_test_hub/static/runs.html")], capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_more_than_one_hundred_pages_are_reachable_and_replaced(self):
        self.run_js(r"""
const b=browser();await settle();await b.ui.detail('run_one');await settle();
assert.equal(b.el('notes').children.length,20);assert.match(b.el('notes-status').textContent,/2221 candidate/);
for(let page=1;page<=111;page++){b.click('notes-next');await settle();assert.ok(b.el('notes').children.length<=20);}
assert.match(b.el('notes-status').textContent,/Page 112/);assert.match(b.el('notes').textContent,/note 2220/);assert.equal(b.el('notes-next').disabled,true);
b.click('notes-previous');await settle();assert.match(b.el('notes-status').textContent,/Page 111/);assert.doesNotMatch(b.el('notes').textContent,/note 2220/);
b.click('notes-newest');await settle();assert.match(b.el('notes-status').textContent,/Page 1 /);assert.equal(b.el('notes').children.length,20);assert.match(b.el('notes').textContent,/saved revision 3/);
""")

    def test_first_page_snapshot_stays_frozen_until_explicit_refresh(self):
        self.run_js(r"""
const b=browser();await settle();await b.ui.detail('run_one');await settle();
b.control.watermark='2222';b.click('notes-next');await settle();assert.match(b.el('notes-status').textContent,/2221 candidate/);
b.click('notes-newest');await settle();assert.match(b.el('notes-status').textContent,/2221 candidate/);
b.click('notes-refresh');await settle();assert.match(b.el('notes-status').textContent,/2222 candidate/);
assert.match(b.calls.filter(c=>c.url.includes('/annotations')).at(-1).url,/annotations\?limit=20$/);
""")

    def test_late_response_cannot_repopulate_different_run_or_reset(self):
        self.run_js(r"""
const b=browser();await settle();b.control.defer=true;await b.ui.detail('run_old');await settle();
assert.equal(b.control.pending.length,1);b.control.defer=false;await b.ui.detail('run_new');await settle();
const before=b.el('notes-status').textContent;b.control.pending.shift()();await settle();assert.equal(b.el('notes-status').textContent,before);assert.match(b.calls.at(-1).url,/run_new/);
b.control.defer=true;b.click('notes-next');await settle();await b.ui.load();await settle();b.control.pending.shift()();await settle();assert.equal(b.el('notes').children.length,0);assert.equal(b.el('detail').hidden,true);assert.equal(b.el('notes-next').disabled,true);
""")

    def test_page_error_clears_notes_preserves_report_view_and_manual_retry(self):
        self.run_js(r"""
for(const status of [400,404,409,503]){
 const b=browser();await settle();await b.ui.detail('run_one');await settle();b.el('reports').textContent='Selected exact report';
 b.control.status=status;b.click('notes-next');await settle();assert.equal(b.el('notes').children.length,0);assert.equal(b.el('reports').textContent,'Selected exact report');assert.equal(b.el('notes-next').disabled,true);assert.equal(b.el('notes-previous').disabled,true);assert.equal(b.el('notes-refresh').disabled,false);assert.match(b.el('notes-status').textContent,/unavailable/);
 b.control.status=null;b.click('notes-refresh');await settle();assert.equal(b.el('notes').children.length,20);
}
""")

    def test_mismatched_pins_and_receipts_are_never_published(self):
        self.run_js(r"""
for(const invalid of ['run','revision','watermark','receipt','count','truncated']){
 const b=browser();await settle();await b.ui.detail('run_one');await settle();b.control.invalid=invalid;b.click('notes-next');await settle();assert.equal(b.el('notes').children.length,0);assert.match(b.el('notes-status').textContent,/unavailable/);assert.equal(b.el('notes-next').disabled,true);
}
""")

    def test_unknown_clock_and_legacy_details_do_not_claim_zero_coverage(self):
        self.run_js(r"""
for(const mode of ['missing','unavailable']){
 const b=browser();await settle();b.control[mode]=true;await b.ui.detail('run_one');await settle();assert.equal(b.el('notes').children.length,0);assert.match(b.el('notes-status').textContent,/unavailable/);assert.doesNotMatch(b.el('notes-status').textContent,/0 candidate/);
 if(mode==='missing')assert.equal(b.calls.filter(c=>c.url.includes('/annotations')).length,0);
}
const b=browser();await settle();b.control.watermark='0';await b.ui.detail('run_one');await settle();assert.match(b.el('notes-status').textContent,/0 candidate/);assert.match(b.el('notes').textContent,/No saved notes/);
""")

    def test_offline_invalidates_pending_response_and_retry_controls(self):
        self.run_js(r"""
const b=browser();await settle();await b.ui.detail('run_one');await settle();b.control.defer=true;b.click('notes-next');await settle();b.offline();b.control.pending.shift()();await settle();assert.equal(b.el('notes').children.length,0);assert.equal(b.el('notes-refresh').disabled,true);assert.equal(b.el('detail').hidden,true);assert.match(b.el('error').textContent,/Hub connection lost/);
""")

    def test_hostile_note_text_exact_revision_and_no_client_side_dedup_or_urls(self):
        self.run_js(r"""
const b=browser();await settle();b.control.duplicates=true;await b.ui.detail('run_one');await settle();
assert.equal(b.el('notes').children.length,20);assert.match(b.el('notes').textContent,/<img src=x/);assert.match(b.el('notes').textContent,/historical\/hub-only/);assert.match(b.el('notes').textContent,/uncertainty ±10 ms/);assert.match(b.el('notes').textContent,/"revision": 3/);
const walk=n=>[n,...n.children.flatMap(walk)];assert.equal(walk(b.el('notes')).filter(n=>['a','img','script'].includes(n.tagName)).length,0);
assert.ok(b.calls.filter(c=>c.url.includes('/annotations')).every(c=>c.url.startsWith('/api/v1/runs/run_one/annotations?limit=20')));
""")

    def test_report_history_refresh_does_not_change_note_snapshot_or_request(self):
        self.run_js(r"""
const b=browser();await settle();await b.ui.detail('run_one');await settle();b.control.defer=true;b.click('notes-next');await settle();
b.click('history-refresh');await settle();assert.equal(b.control.pending.length,1);b.control.pending.shift()();await settle();assert.equal(b.el('notes').children.length,20);assert.match(b.el('notes-status').textContent,/Page 2/);
""")

    def test_search_and_duplicate_clicks_cannot_issue_extra_note_requests(self):
        self.run_js(r"""
const b=browser();await settle();await b.ui.detail('run_one');await settle();b.control.defer=true;b.click('notes-next');b.click('notes-next');await settle();assert.equal(b.control.pending.length,1);assert.equal(b.el('notes-next').disabled,true);
b.click('search');await settle();b.control.pending.shift()();await settle();assert.equal(b.el('notes').children.length,0);assert.equal(b.el('detail').hidden,true);
""")


if __name__ == '__main__':
    unittest.main()
