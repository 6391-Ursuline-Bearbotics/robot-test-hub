/* Durable device outbox. Pure helpers also export to Node for offline regressions. */
(function(root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.NotebookOffline = api;
})(typeof globalThis === 'object' ? globalThis : this, function() {
  'use strict';
  const DB_NAME = 'robot-test-hub-notebook-v1';
  const ACTIVE = new Set(['queued', 'retry_wait', 'sending']);
  const copy = value => JSON.parse(JSON.stringify(value));
  function stable(value) {
    if (Array.isArray(value)) return '[' + value.map(stable).join(',') + ']';
    if (value && typeof value === 'object') return '{' + Object.keys(value).sort().map(k => JSON.stringify(k) + ':' + stable(value[k])).join(',') + '}';
    return JSON.stringify(value);
  }
  function createJob(payload, url='/api/v1/annotations', previous=null, previousReceipt=null) {
    if (!payload || !/^[A-Za-z0-9_-]{1,100}$/.test(payload.event_id) || !Number.isInteger(payload.revision) || payload.revision < 1 || payload.revision > 1000000) throw Error('Invalid event identity or revision');
    const expectedUrl = previous === null ? '/api/v1/annotations' : '/api/v1/annotations/' + payload.event_id + '/revisions';
    if (url !== expectedUrl || (previous !== null && previous !== payload.revision - 1) || (previous === null && payload.revision !== 1)) throw Error('Invalid revision request');
    if(previousReceipt && (previousReceipt.event_id!==payload.event_id || previousReceipt.revision!==previous || previousReceipt.storage_state!=='saved_in_hub' || previousReceipt.delivery_state!=='historical_hub_only'))throw Error('Previous hub receipt does not confirm this revision');
    if(typeof payload.text!=='string'||new TextEncoder().encode(payload.text).length>4096)throw Error('Observation exceeds 4 KiB');
    if(payload.author!==undefined&&(typeof payload.author!=='string'||new TextEncoder().encode(payload.author).length>128))throw Error('Observer exceeds 128 UTF-8 bytes');
    const frozen = copy(payload), body = JSON.stringify(previous === null ? frozen : {annotation:frozen, expected_previous_revision:previous});
    if (new TextEncoder().encode(body).length > 16384) throw Error('Note request exceeds 16 KiB');
    return {key:frozen.event_id + ':' + frozen.revision, url, body, payload:frozen, previous, previous_receipt:previousReceipt?copy(previousReceipt):null,
            state:'queued', attempts:0, error:null, lease_owner:null, lease_until_ms:0, receipt:null};
  }
  function markPayload(base, seconds) {
    if (!Number.isInteger(seconds) || seconds < 0 || seconds > 86400) throw Error('Invalid retrospective delay');
    const result = copy(base);
    result.when = seconds ? {kind:'seconds_ago',seconds} : {kind:'now'};
    result.event_utc_start_ns = result.event_utc_end_ns = String(BigInt(result.submitted_utc_ns) - BigInt(seconds) * 1000000000n);
    return result;
  }
  function revisedPayload(note, changes) {
    const result = copy(note);
    for (const key of ['hub_received_utc_ns','storage_state','delivery_state']) delete result[key];
    result.revision += 1;
    for (const key of ['text','tags','author']) if (key in changes) result[key] = copy(changes[key]);
    return result;
  }
  function sameRequest(existing, incoming) {
    return existing.key === incoming.key && existing.url === incoming.url && existing.body === incoming.body;
  }
  function claim(job, previous, owner, now) {
    if (!job || !ACTIVE.has(job.state) || (job.state === 'sending' && job.lease_until_ms > now && job.lease_owner !== owner)) return null;
    if (previous && previous.state !== 'saved_in_hub' && !job.previous_receipt) return null;
    return {...job, state:'sending', attempts:job.attempts + 1, error:null, lease_owner:owner, lease_until_ms:now + 15000};
  }
  function outcome(job, {status=null, result=null, message=null}) {
    let state, error = message;
    if (status !== null && status >= 200 && status < 300) {
      const annotation = result && result.annotation;
      const matching = annotation && Object.keys(job.payload).every(k => stable(annotation[k]) === stable(job.payload[k]));
      if (matching && annotation.storage_state === 'saved_in_hub' && annotation.delivery_state === 'historical_hub_only') state = 'saved_in_hub';
      else { state = 'retry_wait'; error = 'Hub response did not confirm this exact event/revision'; }
    } else if (status === 409) state = 'conflict';
    else if (status !== null && status >= 400 && status < 500 && status !== 408 && status !== 429) state = 'rejected';
    else state = 'retry_wait';
    return {...job, state, error, lease_owner:null, lease_until_ms:0,
            receipt:state === 'saved_in_hub' ? copy(result.annotation) : null};
  }
  function localPage(notes, {from=null,to=null,includeUnknown=true,cursor=null,limit=50}={}) {
    let selected = notes.filter(n => (!cursor || n.event_id > cursor) &&
      (n.event_utc_start_ns === null ? includeUnknown : (!from || (BigInt(n.event_utc_start_ns) <= BigInt(to) && BigInt(n.event_utc_end_ns) >= BigInt(from)))));
    selected.sort((a,b) => a.event_id < b.event_id ? -1 : a.event_id > b.event_id ? 1 : 0);
    return {annotations:selected.slice(0,limit), next_cursor:selected.length > limit ? selected[limit-1].event_id : null};
  }

  class Store {
    constructor(db) { this.db = db; }
    static open(indexedDB) {
      if (!indexedDB) return Promise.reject(Error('IndexedDB unavailable; no note has been saved'));
      return new Promise((resolve,reject) => {
        const request = indexedDB.open(DB_NAME,1);
        request.onupgradeneeded = () => {
          const db=request.result;
          db.createObjectStore('jobs',{keyPath:'key'});
          db.createObjectStore('notes',{keyPath:'event_id'});
          db.createObjectStore('settings',{keyPath:'key'});
        };
        request.onsuccess = () => {
          const store=new Store(request.result);
          request.result.onversionchange=()=>request.result.close();
          resolve(store);
        };
        request.onerror=()=>reject(Error('Device storage could not open; no note has been saved'));
        request.onblocked=()=>reject(Error('Device storage upgrade is blocked by another tab'));
      });
    }
    transaction(names, write, operation) {
      return new Promise((resolve,reject) => {
        let transaction, value;
        try { transaction=this.db.transaction(names,write?'readwrite':'readonly',write?{durability:'strict'}:undefined); }
        catch(error) { try { transaction=this.db.transaction(names,write?'readwrite':'readonly'); } catch(error) { reject(error); return; } }
        transaction.oncomplete=()=>resolve(value);
        transaction.onerror=()=>reject(transaction.error||Error('Device storage transaction failed'));
        transaction.onabort=()=>reject(transaction.notebookFailure||transaction.error||Error('Device storage did not commit'));
        try { operation(transaction,result=>{value=result;}); }
        catch(error) { transaction.abort(); reject(error); }
      });
    }
    deviceId(uuid) {
      return this.transaction(['settings'],true,(tx,done)=>{
        const store=tx.objectStore('settings'),request=store.get('device_id');
        request.onsuccess=()=>{const value=request.result||{key:'device_id',value:'browser-'+uuid()};if(!request.result)store.add(value);done(value.value);};
      });
    }
    listJobs() { return this.transaction(['jobs'],false,(tx,done)=>{const r=tx.objectStore('jobs').getAll();r.onsuccess=()=>done(r.result);}); }
    listNotes() { return this.transaction(['notes'],false,(tx,done)=>{const r=tx.objectStore('notes').getAll();r.onsuccess=()=>done(r.result);}); }
    enqueue(job) {
      return this.transaction(['jobs'],true,(tx,done)=>{
        const store=tx.objectStore('jobs'),request=store.get(job.key);
        request.onsuccess=()=>{
          if(request.result && !sameRequest(request.result,job)) {tx.notebookFailure=Error('This event/revision already has a different saved request; the original was retained');tx.abort();return;}
          if(!request.result)store.add(job);
          else if(!request.result.previous_receipt&&job.previous_receipt){request.result.previous_receipt=job.previous_receipt;store.put(request.result);}
          done(request.result||job);
        };
      });
    }
    claim(key,owner,now) {
      return this.transaction(['jobs'],true,(tx,done)=>{
        const store=tx.objectStore('jobs'),request=store.get(key);
        request.onsuccess=()=>{
          const job=request.result;
          const complete=previous=>{const claimed=claim(job,previous,owner,now);if(claimed)store.put(claimed);done(claimed);};
          if(job && job.previous!==null){const prior=store.get(job.payload.event_id+':'+job.previous);prior.onsuccess=()=>complete(prior.result);}
          else complete(null);
        };
      });
    }
    finish(job, response) {
      return this.transaction(['jobs','notes'],true,(tx,done)=>{
        const store=tx.objectStore('jobs'),request=store.get(job.key);
        request.onsuccess=()=>{
          const current=request.result;
          if(!current || current.lease_owner!==job.lease_owner || current.attempts!==job.attempts){done(null);return;}
          const completed=outcome(current,response);store.put(completed);
          if(completed.receipt){const notes=tx.objectStore('notes'),prior=notes.get(completed.payload.event_id);prior.onsuccess=()=>{if(!prior.result||prior.result.revision<=completed.receipt.revision)notes.put(completed.receipt);};}
          done(completed);
        };
      });
    }
    retry(key) {
      return this.transaction(['jobs'],true,(tx,done)=>{
        const store=tx.objectStore('jobs'),request=store.get(key);
        request.onsuccess=()=>{const job=request.result;if(job&&job.state!=='saved_in_hub'&&job.state!=='sending'){job.state='queued';job.error=null;store.put(job);}done(job);};
      });
    }
    saveNotes(notes) {
      return this.transaction(['notes'],true,(tx,done)=>{
        const store=tx.objectStore('notes');
        for(const note of notes){const request=store.get(note.event_id);request.onsuccess=()=>{if(!request.result||request.result.revision<=note.revision)store.put(note);};}
        done(null);
      });
    }
  }

  class Outbox {
    constructor(store, owner, send, changed=()=>{}) { this.store=store;this.owner=owner;this.send=send;this.changed=changed;this.running=false; }
    async enqueue(job) { const saved=await this.store.enqueue(job);await this.changed(saved);return saved; }
    async drain() {
      if(this.running)return;
      this.running=true;
      try {
        const jobs=await this.store.listJobs();
        jobs.sort((a,b)=>a.payload.event_id.localeCompare(b.payload.event_id)||a.payload.revision-b.payload.revision);
        for(const candidate of jobs){
          const job=await this.store.claim(candidate.key,this.owner,Date.now());
          if(!job)continue;
          await this.changed(job);
          let response;
          try { response=await this.send(job); } catch(error) { response={message:error.message||'Hub unavailable'}; }
          const completed=await this.store.finish(job,response);
          await this.changed(completed);
        }
      } finally { this.running=false; }
    }
  }
  return {createJob,markPayload,revisedPayload,sameRequest,claim,outcome,localPage,Store,Outbox};
});
