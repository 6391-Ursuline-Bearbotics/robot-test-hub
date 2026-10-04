'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const api=require('../robot_test_hub/static/notebook.js');
const clone=x=>JSON.parse(JSON.stringify(x));
const ACTION='1791071520000000000';
function base(id='event-a'){return {schema_version:1,event_id:id,revision:1,submitted_utc_ns:ACTION,client_monotonic_ns:'50000000000',clock_domain:'browser-session-original',clock_quality:'unverified_client',uncertainty_ms:1000,text:'steering lag',tags:['drive'],author:'test-observer',device_id:'test-device',source:'practice-notebook',run_id:null};}
function receipt(job){return {...clone(job.payload),hub_received_utc_ns:'1791071580000000000',storage_state:'saved_in_hub',delivery_state:'historical_hub_only'};}
function success(job){return {status:201,result:{annotation:receipt(job),idempotent:false}};}
class MemoryStore {
  constructor(){this.jobs=new Map();this.notes=new Map();this.commits=[];this.failEnqueue=false;}
  async enqueue(job){if(this.failEnqueue)throw Error('disk quota');const old=this.jobs.get(job.key);if(old&&!api.sameRequest(old,job))throw Error('local revision conflict');if(!old)this.jobs.set(job.key,clone(job));this.commits.push(job.key);return clone(old||job);}
  async listJobs(){return [...this.jobs.values()].map(clone);}
  async claim(key,owner,now){const job=this.jobs.get(key),prior=job&&this.jobs.get(job.payload.event_id+':'+job.previous);const claimed=api.claim(job,prior,owner,now);if(claimed)this.jobs.set(key,clone(claimed));return claimed;}
  async finish(job,response){const current=this.jobs.get(job.key);if(current.lease_owner!==job.lease_owner||current.attempts!==job.attempts)return null;const next=api.outcome(current,response);this.jobs.set(job.key,clone(next));if(next.receipt)this.notes.set(next.receipt.event_id,next.receipt);return next;}
  async retry(key){const job=this.jobs.get(key);job.state='queued';job.error=null;}
}
async function main(){
  const p=api.markPayload(base(),30),job=api.createJob(p),body=job.body;
  assert.equal(p.event_utc_start_ns,'1791071490000000000');
  p.text='changed after click';p.submitted_utc_ns='0';
  assert.equal(job.payload.text,'steering lag');assert.equal(job.body,body);
  const reloaded=JSON.parse(JSON.stringify(job));
  assert.equal(reloaded.payload.when.seconds,30);assert.equal(reloaded.payload.submitted_utc_ns,ACTION);
  const edit=api.revisedPayload(receipt(job),{text:'confirmed connector issue',tags:['drive','repair']});
  assert.equal(edit.event_utc_start_ns,job.payload.event_utc_start_ns);assert.equal(edit.submitted_utc_ns,ACTION);assert.equal(edit.clock_domain,'browser-session-original');assert.equal(edit.revision,2);
  const revision=api.createJob(edit,'/api/v1/annotations/event-a/revisions',1);
  assert.equal(JSON.parse(revision.body).expected_previous_revision,1);
  assert.throws(()=>api.createJob({...base(),text:'é'.repeat(2049)}));
  assert.throws(()=>api.createJob({...base(),revision:2}));
  assert.throws(()=>api.createJob(base(),'/api/control'));
  assert.throws(()=>api.createJob(edit,revision.url,1,{...receipt(job),revision:9}));

  let claimed=api.claim(job,null,'tab-a',0);
  assert.equal(claimed.state,'sending');assert.equal(claimed.body,body);assert.equal(claimed.attempts,1);
  assert.equal(api.claim(claimed,null,'tab-b',100),null);
  assert.equal(api.claim(claimed,null,'tab-b',15001).attempts,2);
  assert.equal(api.claim(revision,{state:'retry_wait'},'tab-a',0),null);
  const reviewed=api.createJob(edit,revision.url,1,receipt(job));
  assert.equal(api.claim(reviewed,{state:'conflict'},'tab-a',0).state,'sending');
  assert.equal(api.claim({...job,state:'conflict'},null,'tab-a',0),null);
  assert.equal(api.claim({...job,state:'rejected'},null,'tab-a',0),null);

  assert.equal(api.outcome(claimed,success(job)).state,'saved_in_hub');
  assert.equal(api.outcome(claimed,{status:409,message:'different revision'}).state,'conflict');
  assert.equal(api.outcome(claimed,{status:400,message:'bad request'}).state,'rejected');
  for(const status of [null,408,429,500,503])assert.equal(api.outcome(claimed,{status}).state,'retry_wait');
  const altered=success(job);altered.result.annotation.event_utc_start_ns='0';
  const invalidAck=api.outcome(claimed,altered);assert.equal(invalidAck.state,'retry_wait');assert.equal(invalidAck.receipt,null);
  assert.equal(api.outcome(claimed,{status:200,result:{}}).state,'retry_wait');

  const store=new MemoryStore(),requests=[],remote=new Map();
  const first=new api.Outbox(store,'tab-a',async request=>{assert.ok(store.commits.includes(request.key),'request sent before device commit');requests.push(request.body);remote.set(request.key,success(request));throw Error('receipt lost after hub commit');});
  await first.enqueue(job);await first.drain();
  assert.equal(store.jobs.get(job.key).state,'retry_wait');
  const retry=new api.Outbox(store,'tab-reloaded-60-seconds-later',async request=>{requests.push(request.body);return remote.get(request.key);});
  const originalNow=Date.now;Date.now=()=>Number(BigInt(ACTION)/1000000n)+60000;
  try{await retry.drain();}finally{Date.now=originalNow;}
  assert.equal(requests[0],requests[1]);assert.equal(store.jobs.get(job.key).state,'saved_in_hub');
  assert.equal(store.notes.get('event-a').event_utc_start_ns,'1791071490000000000');
  await retry.drain();assert.equal(requests.length,2,'confirmed receipt was resent');
  store.failEnqueue=true;await assert.rejects(()=>first.enqueue(api.createJob(api.markPayload(base('quota'),30))));
  assert.equal(store.jobs.has('quota:1'),false);store.failEnqueue=false;
  await assert.rejects(()=>first.enqueue(api.createJob({...job.payload,text:'different same revision'})));
  assert.equal(store.jobs.get(job.key).body,body);

  const ordered=new MemoryStore(),delivered=[];await ordered.enqueue(job);await ordered.enqueue(revision);
  const waiting=new api.Outbox(ordered,'tab-a',async request=>{delivered.push(request.key);return {message:'offline'};});
  await waiting.drain();assert.deepEqual(delivered,['event-a:1']);
  const connected=new api.Outbox(ordered,'tab-b',async request=>{delivered.push(request.key);return success(request);});
  await connected.drain();assert.deepEqual(delivered,['event-a:1','event-a:1','event-a:2']);
  assert.equal(ordered.jobs.get(revision.key).state,'saved_in_hub');

  const concurrent=new MemoryStore();await concurrent.enqueue(job);let unblock,count=0;
  const gate=new Promise(resolve=>{unblock=resolve;});
  const a=new api.Outbox(concurrent,'tab-a',async request=>{count++;await gate;return success(request);});
  const b=new api.Outbox(concurrent,'tab-b',async request=>{count++;return success(request);});
  const active=a.drain();await new Promise(resolve=>setImmediate(resolve));await b.drain();assert.equal(count,1);unblock();await active;
  assert.equal(concurrent.jobs.get(job.key).state,'saved_in_hub');

  const cached=[receipt(job),{...receipt(job),event_id:'unknown',event_utc_start_ns:null,event_utc_end_ns:null}];
  assert.equal(api.localPage(cached,{from:'1791071490000000000',to:'1791071490000000000',includeUnknown:false}).annotations.length,1);
  assert.equal(api.localPage(cached,{from:'1791071490000000001',to:'1791071490000000002',includeUnknown:false}).annotations.length,0);
  assert.equal(api.localPage(cached,{limit:1}).next_cursor,'event-a');

  const listeners={},cachedPaths=[],cachedResponse={cached:true};
  const context={URL,Response,Promise,self:{location:{origin:'http://127.0.0.1:8765'},addEventListener:(name,fn)=>listeners[name]=fn,skipWaiting:()=>Promise.resolve(),clients:{claim:()=>Promise.resolve()}},
    caches:{open:async()=>({addAll:async paths=>cachedPaths.push(...paths),put:async()=>{}}),keys:async()=>[],delete:async()=>{},match:async()=>cachedResponse},fetch:async()=>{throw Error('offline');}};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../robot_test_hub/static/notebook-sw.js'),'utf8'),context);
  let installation;listeners.install({waitUntil:value=>installation=value});await installation;
  assert.deepEqual(cachedPaths,['/notebook','/notebook.js']);
  for(const [method,url] of [['GET','/api/v1/annotations'],['POST','/api/v1/annotations'],['GET','/api/v1/status'],['GET','/'],['GET','/notebook-sw.js'],['GET','https://example.invalid/notebook']]){
    let intercepted=false;listeners.fetch({request:{method,url:new URL(url,'http://127.0.0.1:8765').href},respondWith:()=>intercepted=true});assert.equal(intercepted,false,url);
  }
  let response;listeners.fetch({request:{method:'GET',url:'http://127.0.0.1:8765/notebook'},respondWith:value=>response=value});assert.equal(await response,cachedResponse);
  console.log('Offline notebook helpers, immutable retries, receipt loss, revision ordering, tab leases, quota/conflicts, cached intervals, and shell-only service worker checks passed');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
