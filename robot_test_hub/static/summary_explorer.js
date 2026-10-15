(function (global) {
'use strict';
const POSITIONS = ['front-left','front-right','back-left','back-right'];
const LIMIT = 20;
function integer(value, nonnegative) {
  if (typeof value !== 'string' || !/^(0|-?[1-9][0-9]*)$/.test(value)) throw Error('Invalid recorded UTC');
  const n = BigInt(value);
  if (n < -9223372036854775808n || n > 9223372036854775807n || (nonnegative && n < 0n)) throw Error('Invalid recorded UTC');
  return n;
}
function parseUTC(text) {
  if (text === '' || text === undefined || text === null) return null;
  if (typeof text !== 'string') throw Error('Use an explicit UTC date ending in Z.');
  const m = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?Z$/.exec(text);
  if (!m) throw Error('Use UTC YYYY-MM-DDTHH:mm:ss[.nanoseconds]Z.');
  const seconds = m[1] + 'T' + m[2] + 'Z';
  const ms = Date.parse(seconds);
  if (!Number.isFinite(ms) || new Date(ms).toISOString() !== seconds.slice(0,-1) + '.000Z') throw Error('UTC date or time is invalid.');
  const n = BigInt(ms) * 1000000n + BigInt((m[3] || '').padEnd(9,'0'));
  if (n < -9223372036854775808n || n > 9223372036854775807n) throw Error('UTC date is outside the recorded timestamp range.');
  return n;
}
function formatUTC(n) {
  let seconds=n/1000000000n,remainder=n%1000000000n;
  if(remainder<0n){seconds--;remainder+=1000000000n;}
  return new Date(Number(seconds*1000n)).toISOString().slice(0,-5)+'.'+String(remainder).padStart(9,'0')+'Z';
}
function bounds(from,to) {
  const result = {from:parseUTC(from),to:parseUTC(to)};
  if (result.from !== null && result.to !== null && result.from > result.to) throw Error('From UTC must be at or before To UTC.');
  return result;
}
function candidates(output) {
  const result = {valid:[],invalid:0};
  const entries = output && output.run && output.run.utc_intervals;
  if (!Array.isArray(entries)) return result;
  for (const row of entries) {
    try {
      const start=integer(row.utc_start_ns,false),end=integer(row.utc_end_ns,false),uncertainty=integer(row.uncertainty_ns,true);
      if (start>end) throw Error('Invalid interval');
      result.valid.push({start,end,uncertainty});
    } catch (_) { result.invalid++; }
  }
  return result;
}
function field(row,key) {
  if (key==='source') return row.source_type;
  if (key==='test') return (row.context || {}).test_id;
  return (row.run || {})[key==='robot'?'robot_id':'runtime_mode'];
}
function newest(row) {
  const values=candidates(row).valid;
  return values.length ? values.reduce((n,v)=>v.start>n?v.start:n,values[0].start) : null;
}
function select(runs,filters) {
  filters=filters || {};
  const interval=bounds(filters.from,filters.to),restricted=interval.from!==null || interval.to!==null;
  const unknown=filters.unknown || 'auto';
  if (!['auto','include','exclude','only'].includes(unknown)) throw Error('Invalid unavailable UTC selection.');
  const selected=runs.filter(row=>{
    for (const key of ['robot','source','runtime','test']) if (filters[key]!==undefined && field(row,key)!==filters[key]) return false;
    const utc=candidates(row).valid;
    if (!utc.length) return unknown==='include' || unknown==='only' || (unknown==='auto' && !restricted);
    if (unknown==='only') return false;
    return utc.some(v=>(interval.to===null || v.start-v.uncertainty<=interval.to) && (interval.from===null || v.end+v.uncertainty>=interval.from));
  });
  return selected.sort((a,b)=>{
    const x=newest(a),y=newest(b);
    if (x!==y) {if(x===null)return 1;if(y===null)return -1;return x>y?-1:1;}
    const xId=String((a.run || {}).run_id || ''),yId=String((b.run || {}).run_id || '');
    return xId<yId?-1:xId>yId?1:0;
  });
}
function stable(value) {
  if (Array.isArray(value)) return '['+value.map(stable).join(',')+']';
  if (value && typeof value==='object') return '{'+Object.keys(value).sort().map(k=>JSON.stringify(k)+':'+stable(value[k])).join(',')+'}';
  return JSON.stringify(value);
}
function finiteMean(values) {
  const scale=values.reduce((largest,v)=>Math.max(largest,Math.abs(v)),0);
  return scale===0?0:scale*(values.reduce((sum,v)=>sum+v/scale,0)/values.length);
}
function cohorts(outputs) {
  const groups=new Map();
  for (const row of outputs) {
    const id=(row.run || {}).run_id;
    if (typeof row.cohort_id!=='string' || !row.cohort_id || typeof id!=='string' || !id) continue;
    const key=stable([row.cohort_id,row.context || {},row.source_type, (row.run || {}).robot_id,(row.run || {}).runtime_mode]);
    if (!groups.has(key)) groups.set(key,{cohort_id:row.cohort_id,context:row.context || {},runs:new Map()});
    const runs=groups.get(key).runs;
    if (!runs.has(id)) runs.set(id,[]);
    runs.get(id).push(row);
  }
  return Array.from(groups.entries()).sort((a,b)=>a[0]<b[0]?-1:a[0]>b[0]?1:0).map(entry=>{
    const group=entry[1],modules=POSITIONS.map(position=>{
      const values=[];
      for (const rows of group.runs.values()) {
        const metrics=rows.map(row=>{
          const tracking=(row.statistics || {}).tracking;
          const matches=Array.isArray(tracking)?tracking.filter(v=>v.module_position===position):[];
          const value=matches.length===1?matches[0].drive_rmse_mps:null;
          return typeof value==='number' && Number.isFinite(value)?value:null;
        });
        if (metrics.every(v=>v!==null && v===metrics[0])) values.push(metrics[0]);
      }
      return {module_position:position,runs_with_recorded_metric:values.length,unavailable_run_count:group.runs.size-values.length,
        minimum_recorded_rmse_mps:values.length?Math.min(...values):null,maximum_recorded_rmse_mps:values.length?Math.max(...values):null,
        mean_recorded_run_rmse_mps:values.length?finiteMean(values):null};
    });
    return {cohort_id:group.cohort_id,context:group.context,independent_run_count:group.runs.size,modules};
  });
}
function page(rows,index) {
  const pages=Math.max(1,Math.ceil(rows.length/LIMIT));
  index=Math.min(Math.max(0,Number.isInteger(index)?index:0),pages-1);
  return {items:rows.slice(index*LIMIT,(index+1)*LIMIT),index,pages,total:rows.length};
}
function initialize(document) {
  const root=document.getElementById('summary-explorer'),data=document.getElementById('summary-data');
  if (!root || !data) return;
  const el=id=>document.getElementById(id);
  function node(tag,text,parent) {const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(parent)parent.appendChild(n);return n;}
  function details(parent,label,value) {const d=node('details',undefined,parent);node('summary',label,d);let loaded=false;d.addEventListener('toggle',()=>{if(d.open && !loaded){node('pre',JSON.stringify(value,null,2),d);loaded=true;}});}
  let summary;
  try {summary=JSON.parse(data.textContent);if(summary.schema_version!==1 || !Array.isArray(summary.runs) || !Array.isArray(summary.recordings))throw Error();}
  catch (_) {el('explorer-error').textContent='This saved summary cannot be read. Open the original JSON or manifest for investigation.';return;}
  let captured='Unavailable';
  try{captured=formatUTC(integer(summary.created_utc_ns,false));}catch(_){}
  el('snapshot-identity').textContent='Saved report: '+String(summary.id ?? 'Unavailable')+' · Captured UTC: '+captured+' · '+summary.runs.length+' run rows in this snapshot.';
  function metric(value){return typeof value==='number' && Number.isFinite(value)?String(value):'Unavailable';}
  function table(parent,title,columns,rows) {
    node('h4',title,parent);const t=node('table',undefined,parent);node('caption',title,t);
    const head=node('thead',undefined,t),header=node('tr',undefined,head);
    columns.forEach(label=>{const h=node('th',label,header);h.scope='col';});
    const body=node('tbody',undefined,t);
    for(const row of rows){const tr=node('tr',undefined,body);row.forEach((value,index)=>{const cell=node(index===0?'th':'td',value,tr);if(index===0)cell.scope='row';});}
  }
  function positionRows(entries,keys){return POSITIONS.map(position=>{const matches=Array.isArray(entries)?entries.filter(v=>v.module_position===position):[];return [position,...keys.map(key=>matches.length===1?metric(matches[0][key]):'Unavailable')];});}
  const selections={},state={runs:[],cohorts:[],run:0,cohort:0,recording:0};
  for (const key of ['robot','source','runtime','test']) {
    const control=el('filter-'+key),values=Array.from(new Set(summary.runs.map(row=>field(row,key))));
    values.sort((a,b)=>String(a).localeCompare(String(b)));
    node('option','All',control).value='';selections[key]=values;
    values.forEach((value,index)=>{node('option',value===null || value===undefined?'Unavailable':String(value),control).value=String(index);});
    control.value='';
  }
  function renderPage(kind,rows,render) {
    const p=page(rows,state[kind]);state[kind]=p.index;
    el(kind+'-prev').disabled=p.index===0;el(kind+'-next').disabled=p.index+1>=p.pages;
    el(kind+'-count').textContent=p.total+' '+(kind==='run'?'selected run rows':kind==='cohort'?'selected cohort groups':'whole snapshot recordings')+' · Page '+(p.index+1)+' of '+p.pages;
    const list=el(kind+'-list');list.replaceChildren();
    if(!p.total)node('p','No matching evidence.',list);
    p.items.forEach(row=>render(row,node('article',undefined,list)));
  }
  function renderRuns() {renderPage('run',state.runs,(row,parent)=>{
    const run=row.run || {},utc=candidates(row);
    node('h3',String(run.run_id || 'Run identity unavailable'),parent);
    node('p','Robot: '+String(run.robot_id ?? 'Unavailable')+' · Source: '+String(row.source_type ?? 'Unavailable')+' · Runtime: '+String(run.runtime_mode ?? 'Unavailable')+' · Test: '+String((row.context || {}).test_id ?? 'Unavailable'),parent);
    node('p',utc.valid.length?utc.valid.length+' UTC candidate(s); contextual uncertainty applies. '+utc.invalid+' invalid candidate(s) unavailable.':'UTC unavailable; this run is unlocated context.',parent);
    utc.valid.slice(0,20).forEach(v=>node('p','UTC candidate: '+formatUTC(v.start)+' to '+formatUTC(v.end)+'; uncertainty ±'+v.uncertainty+' ns. Exact ns: '+v.start+' to '+v.end+'.',parent));
    if(utc.valid.length>20)node('p','First 20 candidates shown. All candidates remain available in full saved evidence below.',parent);
    const stats=row.statistics || {};
    table(parent,'Recorded tracking', ['Module','Drive RMSE (m/s)','Steering RMSE (rad)','Eligible time (s)','Coverage fraction'],positionRows(stats.tracking,['drive_rmse_mps','steering_rmse_rad','eligible_seconds','coverage_fraction']));
    for(const side of ['drive','turn'])table(parent,'Recorded '+side+' current',['Module','Mean (A)','Peak (A)','Eligible time (s)'],positionRows((stats.currents || {})[side],['mean_amps','peak_amps','eligible_seconds']));
    node('p',row.cohort_id?'Qualified descriptive cohort: '+row.cohort_id:'Comparison unavailable: '+(row.comparison_unavailable || []).join(', '),parent);
    details(parent,'Full saved run metrics, source hashes, identities and clock evidence',row);
  });}
  function renderCohorts() {renderPage('cohort',state.cohorts,(row,parent)=>{
    node('h3','Cohort '+row.cohort_id,parent);node('p',row.independent_run_count+' distinct selected runs. Test: '+String(row.context.test_id ?? 'Unavailable'),parent);
    row.modules.forEach(v=>node('p',v.module_position+': '+v.runs_with_recorded_metric+' metric runs; '+v.unavailable_run_count+' unavailable. Min / max / mean run RMSE (m/s): '+[v.minimum_recorded_rmse_mps,v.maximum_recorded_rmse_mps,v.mean_recorded_run_rmse_mps].map(n=>n===null?'Unavailable':String(n)).join(' / '),parent));
    details(parent,'Exact cohort context',row.context);
  });}
  function renderRecordings(){renderPage('recording',summary.recordings,(row,parent)=>{node('h3','Whole recording '+String(row.id ?? 'identity unavailable'),parent);node('p','Source: '+String(row.source_type ?? 'Unavailable')+' · Records: '+String(row.record_count ?? 'Unavailable')+' · Frames: '+String(row.frame_count ?? 'Unavailable'),parent);details(parent,'Whole recording quality and source identity',row);});}
  function apply(){
    try {
      const filters={from:el('filter-from').value,to:el('filter-to').value,unknown:el('filter-unknown').value || 'auto'};
      for(const key of ['robot','source','runtime','test']){const value=el('filter-'+key).value;if(value!=='')filters[key]=selections[key][Number(value)];}
      state.runs=select(summary.runs,filters);state.cohorts=cohorts(state.runs);state.run=0;state.cohort=0;
      el('explorer-error').textContent='';renderRuns();renderCohorts();
    } catch(error){state.runs=[];state.cohorts=[];state.run=0;state.cohort=0;renderRuns();renderCohorts();el('explorer-error').textContent=error.message+' Selected evidence is cleared until valid filters are applied.';}
  }
  el('apply-filters').addEventListener('click',apply);
  el('clear-filters').addEventListener('click',()=>{for(const key of ['robot','source','runtime','test','from','to'])el('filter-'+key).value='';el('filter-unknown').value='auto';state.recording=0;apply();renderRecordings();});
  for(const kind of ['run','cohort','recording'])for(const direction of ['prev','next'])el(kind+'-'+direction).addEventListener('click',()=>{state[kind]+=direction==='next'?1:-1;if(kind==='run')renderRuns();else if(kind==='cohort')renderCohorts();else renderRecordings();});
  apply();renderRecordings();
}
const api={parseUTC,formatUTC,bounds,candidates,select,cohorts,page,initialize};
global.SummaryExplorer=api;
if(global.document)initialize(global.document);
})(typeof window==='undefined'?globalThis:window);
