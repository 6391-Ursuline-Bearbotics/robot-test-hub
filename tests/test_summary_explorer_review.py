"""Independent adversarial checks for complete portable summary snapshots."""
import base64
from contextlib import closing
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
NODE=shutil.which('node')


class Scripts(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.scripts=[];self.csp=None;self.current=None;self.external=[]
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag=='script':
            self.current={'attrs':attrs,'text':''};self.scripts.append(self.current)
            if 'src' in attrs:self.external.append(attrs['src'])
        if tag=='meta' and attrs.get('http-equiv','').lower()=='content-security-policy':self.csp=attrs['content']
        if tag in ('img','iframe','link'):
            self.external.extend(attrs[key] for key in ('src','href') if key in attrs)
    def handle_endtag(self,tag):
        if tag=='script':self.current=None
    def handle_data(self,data):
        if self.current is not None:self.current['text']+=data


NODE_REVIEW = r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const context={fetch(){throw Error('offline fetch forbidden')},localStorage:new Proxy({},{get(){throw Error('storage forbidden')}})};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
const api=context.SummaryExplorer;
const base=1792067696000000000n;
function row(id,start=100n,end=start,uncertainty=0n){return {run:{run_id:id,robot_id:'robot',runtime_mode:'SIM',utc_intervals:[{utc_start_ns:String(base+start),utc_end_ns:String(base+end),uncertainty_ns:String(uncertainty)}]},source_type:'synthetic',context:{test_id:'test'},cohort_id:'cohort',statistics:{tracking:[{module_position:'front-left',drive_rmse_mps:2}]}};}
function stamp(n){return '2026-10-15T12:34:56.'+String(n).padStart(9,'0')+'Z';}
assert.equal(api.parseUTC(stamp(1)),base+1n);
assert.equal(api.parseUTC('1970-01-01T00:00:00.000000001Z'),1n);
for(const invalid of ['2026-02-29T00:00:00Z','2026-04-31T00:00:00Z','2026-10-15T24:00:00Z',stamp(1)+'x','2026-10-15T12:34:56.1234567890Z','2026-10-15T12:34:56']) assert.throws(()=>api.parseUTC(invalid));
assert.throws(()=>api.select([row('x')],{from:stamp(2),to:stamp(1)}));
assert.throws(()=>api.select([row('x')],{from:'bad',unknown:'include'}));
const uncertain=row('uncertain',100n,200n,10n);uncertain.run.utc_intervals.push({utc_start_ns:String(base+1000n),utc_end_ns:String(base+1100n),uncertainty_ns:'10'});
for(const n of [90,210,990,1110])assert.equal(api.select([uncertain],{from:stamp(n),to:stamp(n)}).length,1);
for(const n of [89,211,989,1111])assert.equal(api.select([uncertain],{from:stamp(n),to:stamp(n)}).length,0);
const unknown=row('unknown');unknown.run.utc_intervals=[];
assert.equal(api.select([unknown],{from:stamp(1)}).length,0);
assert.equal(api.select([unknown],{from:stamp(1),unknown:'include'}).length,1);
assert.equal(api.select([unknown,row('known')],{unknown:'only'})[0].run.run_id,'unknown');
assert.equal(api.select([row('notepoch')],{from:'1970-01-01T00:00:00Z',to:'1970-01-01T00:00:00Z'}).length,0);
const malformed=row('malformed');malformed.run.utc_intervals[0].uncertainty_ns='-1';
assert.equal(api.candidates(malformed).invalid,1);assert.equal(api.candidates(malformed).valid.length,0);
assert.equal(api.select([row('older',1n),row('newer',2n)])[0].run.run_id,'newer');
assert.equal(api.select([row('literal')],{robot:'robot',source:'synthetic',runtime:'SIM',test:'test'}).length,1);
assert.equal(api.select([row('literal')],{robot:'rob'}).length,0);
const rows=Array.from({length:10000},(_,i)=>row(String(i),BigInt(i)));
const selected=api.select(rows),seen=new Set();
for(let i=0;i<500;i++){const page=api.page(selected,i);assert.equal(page.items.length,20);for(const item of page.items)seen.add(item.run.run_id);}
assert.equal(seen.size,10000);assert.equal(api.page(selected,99999).index,499);
const a=row('constructor'),same=row('constructor'),b=row('b'),bad=row('c');
a.cohort_id=same.cohort_id=b.cohort_id=bad.cohort_id='__proto__';
a.statistics.tracking=[{module_position:'back-right',drive_rmse_mps:999},{module_position:'front-left',drive_rmse_mps:2}];
same.statistics.tracking=[...a.statistics.tracking].reverse();b.statistics.tracking[0].drive_rmse_mps=4;
bad.statistics.tracking=[{module_position:'front-left',drive_rmse_mps:100},{module_position:'front-left',drive_rmse_mps:100}];
const extremes=[row('max1'),row('max2'),row('max3')];for(const item of extremes)item.statistics.tracking[0].drive_rmse_mps=Number.MAX_VALUE;assert.equal(api.cohorts(extremes)[0].modules[0].mean_recorded_run_rmse_mps,Number.MAX_VALUE);
const grouped=api.cohorts([a,same,b,bad]);assert.equal(grouped.length,1);assert.equal(grouped[0].independent_run_count,3);
const metric=grouped[0].modules.find(m=>m.module_position==='front-left');assert.equal(metric.runs_with_recorded_metric,2);assert.equal(metric.unavailable_run_count,1);assert.equal(metric.mean_recorded_run_rmse_mps,3);
class Element{constructor(tag='div'){this.tagName=tag;this.children=[];this.listeners={};this.value='';this._text='';}appendChild(n){this.children.push(n);return n;}replaceChildren(){this.children=[];this._text='';}set textContent(v){this._text=String(v);this.children=[];}get textContent(){return this._text+this.children.map(v=>v.textContent).join(' ');}addEventListener(event,handler){this.listeners[event]=handler;}set innerHTML(value){throw Error('unsafe HTML');}}
const elements=new Map(),get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
get('summary-data').textContent=JSON.stringify({schema_version:1,runs:rows,recordings:Array.from({length:45},(_,i)=>({id:String(i),record_count:i}))});
const document={getElementById:get,createElement:tag=>new Element(tag)};
api.initialize(document);
assert.equal(get('run-list').children.length,20);assert.equal(get('recording-list').children.length,20);
const originalRecordings=get('recording-list').textContent;
get('filter-from').value='bad';get('apply-filters').listeners.click();
assert.ok(get('explorer-error').textContent.includes('cleared'));assert.ok(get('run-list').textContent.includes('No matching'));
assert.equal(get('recording-list').textContent,originalRecordings);
get('clear-filters').listeners.click();
const domSeen=new Set();
for(let i=0;i<500;i++){const articles=get('run-list').children;assert.equal(articles.length,20);for(const article of articles)domSeen.add(article.children[0].textContent);if(i<499)get('run-next').listeners.click();}
assert.equal(domSeen.size,10000);assert.equal(get('run-next').disabled,true);
"""

class SummaryExplorerReviewTests(unittest.TestCase):
    @unittest.skipUnless(NODE,'Node is needed for isolated actual explorer JavaScript')
    def test_actual_javascript_precise_filters_cohorts_and_bounded_dom(self):
        result=subprocess.run([NODE,'-e',NODE_REVIEW,str(ROOT/'robot_test_hub/static/summary_explorer.js')],capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_hostile_payload_remains_exact_nonexecutable_complete_json(self):
        from robot_test_hub.portable_summary import render_report
        hostile='</ScRiPt><img src=https://invalid.example onerror=attack()> %%SUMMARY_SCRIPT%% %%SCRIPT_CSP%% %%SUMMARY_DATA%% & \u2028 \u2029'
        summary={'schema_version':1,'id':hostile,'runs':[{'run':{'run_id':str(i)},'label':hostile} for i in range(10000)],'recordings':[]}
        html=render_report(summary)
        parsed=Scripts();parsed.feed(html)
        self.assertEqual(len(parsed.scripts),2)
        data=[row for row in parsed.scripts if row['attrs'].get('type')=='application/json']
        self.assertEqual(len(data),1)
        self.assertEqual(json.loads(data[0]['text']),summary)
        self.assertNotIn('<',data[0]['text'])
        self.assertNotIn('&',data[0]['text'])
        self.assertNotIn('\u2028',data[0]['text'])
        self.assertEqual(parsed.external,[])
        executable=[row for row in parsed.scripts if row not in data]
        script=executable[0]['text'].encode('utf-8')
        packaged=(ROOT/'robot_test_hub/static/summary_explorer.js').read_text(encoding='utf-8').encode('utf-8')
        self.assertEqual(script,packaged)
        digest='sha256-'+base64.b64encode(hashlib.sha256(script).digest()).decode('ascii')
        self.assertIn("'"+digest+"'",parsed.csp)
        directives={piece.strip().split()[0]:piece.strip().split()[1:] for piece in parsed.csp.split(';') if piece.strip()}
        self.assertNotIn("'unsafe-inline'",directives['script-src'])
        self.assertNotIn("'unsafe-eval'",directives['script-src'])
        self.assertEqual(directives['default-src'],["'none'"])
        self.assertLess(len(html.encode('utf-8')),64*1024*1024)

    def test_renderer_rejects_nonfinite_embedded_values(self):
        from robot_test_hub.portable_summary import render_report
        for value in (float('nan'),float('inf'),float('-inf')):
            with self.subTest(value=value),self.assertRaises(ValueError):render_report({'runs':[],'bad':value})

    def test_presentation_upgrade_uses_existing_numeric_cache_without_queries(self):
        from robot_test_hub.analysis import identity
        from robot_test_hub import summaries
        run=dict(run_id='r',start_monotonic_ns='1',end_monotonic_ns='2',segment_ids=['s'])
        jobs=[{'id':'columnar-old'}];policy={'max_gap_ns':250000000,'max_sample_age_ns':100000000,'min_demand_mps':0.1}
        old_key=identity(['summary-1',run,['columnar-old'],policy])
        document={'cache_key':old_key,'sentinel':'exact old numeric values'}
        with closing(sqlite3.connect(':memory:')) as db:
            db.execute('CREATE TABLE summary_run_cache(id TEXT PRIMARY KEY,document_json TEXT)')
            db.execute('INSERT INTO summary_run_cache VALUES(?,?)',(old_key,json.dumps(document)))
            summarizer=summaries.Summarizer(Path('.'),db,None)
            with patch.object(summaries,'connection',side_effect=AssertionError('numeric cache must avoid DuckDB')):
                actual,reused=summarizer._run_statistics(run,jobs,policy)
            self.assertEqual(actual,document)
            self.assertTrue(reused)
            self.assertNotEqual(summaries.SUMMARY_VERSION,'summary-1')


    def test_cohort_metrics_use_unique_runs_and_unambiguous_named_positions(self):
        from robot_test_hub.summaries import aggregate_cohorts
        def output(run,modules,cohort='qualified'):
            return {'run':{'run_id':run},'cohort_id':cohort,'context':{'test_id':'test'},
                    'statistics':{'tracking':[{'module_position':name,'drive_rmse_mps':value} for name,value in modules]}}
        rows=[output('a',[('back-right',99),('front-left',2)]),
              output('a',[('front-left',2),('back-right',99)]),
              output('b',[('front-left',4)]),
              output('c',[('front-left',100),('front-left',100)]),
              output('d',[('front-left',True)]),
              output('e',[('front-left',float('inf'))]),
              output('conflict',[('front-left',6)]),output('conflict',[('front-left',8)]),
              output('excluded',[('front-left',900)],None),
              output('separate',[('front-left',800)],'other')]
        result=aggregate_cohorts(rows)
        selected=next(row for row in result if row['cohort_id']=='qualified' and row['module_position']=='front-left')
        self.assertEqual(selected['independent_run_count'],6)
        self.assertEqual(selected['runs_with_recorded_metric'],2)
        self.assertEqual((selected['minimum_recorded_rmse_mps'],selected['maximum_recorded_rmse_mps'],selected['mean_recorded_run_rmse_mps']),(2,4,3))
        missing=next(row for row in result if row['cohort_id']=='qualified' and row['module_position']=='back-left')
        self.assertEqual(missing['runs_with_recorded_metric'],0)
        self.assertIsNone(missing['mean_recorded_run_rmse_mps'])
        self.assertEqual({row['cohort_id'] for row in result},{'qualified','other'})
        self.assertTrue(all(row['health_verdict']=='not_evaluated' and row['baseline_approved'] is False for row in result))
        extremes=aggregate_cohorts([output(str(i),[('front-left',sys.float_info.max)]) for i in range(3)])
        self.assertEqual(next(row['mean_recorded_run_rmse_mps'] for row in extremes if row['module_position']=='front-left'),sys.float_info.max)

if __name__=='__main__':unittest.main()
