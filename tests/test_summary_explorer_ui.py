"""Actual standalone explorer model and DOM behavior, without network or dependencies."""
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
JS = ROOT / "robot_test_hub/static/summary_explorer.js"
HARNESS = r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(process.argv[1],'utf8'),window={};
vm.runInNewContext(source,{window});
const api=window.SummaryExplorer;
const row=(id,ns='100',extra={})=>({run:{run_id:id,robot_id:'robot',runtime_mode:'SIM',utc_intervals:ns===null?[]:[{utc_start_ns:ns,utc_end_ns:ns,uncertainty_ns:'0'}]},source_type:'synthetic',context:{test_id:'test',config_hash:'config'},cohort_id:'cohort',statistics:{tracking:[{module_position:'front-left',drive_rmse_mps:2}]},...extra});
class Element {
 constructor(tag='div'){this.tagName=tag;this.children=[];this.listeners={};this.value='';this.disabled=false;this._text='';this.open=false;}
 appendChild(n){this.children.push(n);return n;}
 replaceChildren(...nodes){this._text='';this.children=nodes;}
 set textContent(v){this._text=String(v);this.children=[];}
 get textContent(){return this._text+this.children.map(n=>n.textContent).join(' ');}
 addEventListener(name,fn){this.listeners[name]=fn;}
}
function browser(summary){
 const elements=new Map(),element=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
 element('summary-data').textContent=JSON.stringify(summary);
 const document={getElementById:element,createElement:tag=>new Element(tag)},window={document};
 vm.runInNewContext(source,{window,fetch(){throw Error('Network forbidden');},localStorage:{getItem(){throw Error('Storage forbidden');}}});
 return {element,click(id){element(id).listeners.click();},change(id,value){element(id).value=value;}};
}
"""

@unittest.skipUnless(NODE, "Node is required only for development UI verification")
class SummaryExplorerUITests(unittest.TestCase):
    def run_js(self, body):
        result = subprocess.run([NODE, "-e", HARNESS + body, str(JS)], capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_exact_nanosecond_order_and_uncertain_inclusive_overlap(self):
        self.run_js(r"""
const a=row('a','9007199254740993000'),b=row('b','9007199254740993001');
assert.equal(api.select([a,b],{})[0].run.run_id,'b');
a.run.utc_intervals[0].uncertainty_ns='10';
const t=api.parseUTC('1970-04-15T05:59:59.254743010Z');
assert.equal(t,9007199254743010n);
const c=row('c','1000000001');c.run.utc_intervals[0].uncertainty_ns='1';
assert.equal(api.select([c],{to:'1970-01-01T00:00:01Z'}).length,1);
assert.equal(api.select([c],{to:'1970-01-01T00:00:00.999999999Z'}).length,0);
c.run.utc_intervals.push({utc_start_ns:'2000000000',utc_end_ns:'2000000001',uncertainty_ns:'2'});
assert.equal(api.select([c],{from:'1970-01-01T00:00:02.000000003Z'}).length,1);
""")

    def test_strict_calendar_range_and_invalid_uncertainty(self):
        self.run_js(r"""
for(const value of ['2026-02-30T00:00:00Z','2026-01-01T25:00:00Z','2026-01-01','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00.0000000001Z'])assert.throws(()=>api.parseUTC(value));
assert.throws(()=>api.select([row('a')],{from:'2026-02-02T00:00:00Z',to:'2026-02-01T00:00:00Z'}));
const a=row('a');a.run.utc_intervals[0].uncertainty_ns='-1';
assert.equal(api.candidates(a).valid.length,0);assert.equal(api.candidates(a).invalid,1);
assert.equal(api.select([a],{from:'1970-01-01T00:00:00Z'}).length,0);
assert.equal(api.select([a],{}).length,1);
""")

    def test_unknown_policy_and_exact_identity_filters(self):
        self.run_js(r"""
const a=row('a'),b=row('b',null),c=row('c',null,{source_type:'real',context:{test_id:'other'}});
assert.equal(api.select([a,b,c],{}).length,3);
assert.equal(api.select([a,b,c],{from:'1970-01-01T00:00:00Z'}).length,1);
assert.equal(api.select([a,b,c],{from:'1970-01-01T00:00:00Z',unknown:'include'}).length,3);
assert.equal(api.select([a,b,c],{unknown:'only',source:'synthetic',robot:'robot',runtime:'SIM',test:'test'})[0].run.run_id,'b');
assert.equal(api.select([a,b,c],{test:'tes'}).length,0);
""")

    def test_cohorts_positions_distinct_runs_and_missing_metrics(self):
        self.run_js(r"""
const a=row('a'),b=row('b');b.statistics.tracking=[{module_position:'back-right',drive_rmse_mps:10},{module_position:'front-left',drive_rmse_mps:4}];
let groups=api.cohorts([a,a,b,row('no',null,{cohort_id:null})]);
assert.equal(groups.length,1);assert.equal(groups[0].independent_run_count,2);
let front=groups[0].modules[0];assert.equal(front.mean_recorded_run_rmse_mps,3);assert.equal(front.runs_with_recorded_metric,2);
assert.equal(groups[0].modules[3].unavailable_run_count,1);
b.statistics.tracking.push({module_position:'front-left',drive_rmse_mps:4});
front=api.cohorts([a,b])[0].modules[0];assert.equal(front.runs_with_recorded_metric,1);assert.equal(front.unavailable_run_count,1);
const conflict=row('a');conflict.statistics.tracking[0].drive_rmse_mps=8;
assert.equal(api.cohorts([a,conflict])[0].modules[0].runs_with_recorded_metric,0);
for(const metric of [null,true,NaN,Infinity,'2']){const r=row('bad');r.statistics.tracking[0].drive_rmse_mps=metric;assert.equal(api.cohorts([r])[0].modules[0].mean_recorded_run_rmse_mps,null);}
""")

    def test_cohorts_do_not_merge_different_source_configuration_or_unqualified(self):
        self.run_js(r"""
const a=row('a'),b=row('b',null,{source_type:'real'}),c=row('c',null,{context:{test_id:'test',config_hash:'other'}}),d=row('d',null,{cohort_id:null});
assert.equal(api.cohorts([a,b,c,d]).length,3);
const x=row('x'),y=row('y');x.statistics.tracking[0].drive_rmse_mps=1e308;y.statistics.tracking[0].drive_rmse_mps=1e308;
assert.equal(api.cohorts([x,y])[0].modules[0].mean_recorded_run_rmse_mps,1e308);
const large=[row('max1'),row('max2'),row('max3')];for(const r of large)r.statistics.tracking[0].drive_rmse_mps=Number.MAX_VALUE;
assert.equal(api.cohorts(large)[0].modules[0].mean_recorded_run_rmse_mps,Number.MAX_VALUE);
""")

    def test_ten_thousand_rows_reachable_and_all_dom_sections_bounded(self):
        self.run_js(r"""
const runs=Array.from({length:10000},(_,i)=>row('run-'+String(i).padStart(5,'0'),String(i),{cohort_id:'c'+i}));
const ordered=api.select(runs,{});assert.equal(api.page(ordered,499).items.at(-1).run.run_id,'run-00000');
const b=browser({schema_version:1,runs,recordings:Array.from({length:100},(_,i)=>({id:'recording'+i}))});
assert.equal(b.element('run-list').children.length,20);assert.equal(b.element('cohort-list').children.length,20);assert.equal(b.element('recording-list').children.length,20);
for(let i=0;i<11;i++)b.click('run-next');
assert.match(b.element('run-count').textContent,/Page 12 of 500/);assert.equal(b.element('run-list').children.length,20);
assert.doesNotMatch(b.element('run-list').textContent,/run-09999/);
assert.match(b.element('run-list').textContent,/run-09779/);
b.click('cohort-next');assert.match(b.element('cohort-count').textContent,/Page 2 of 500/);
b.click('recording-next');assert.match(b.element('recording-list').textContent,/recording20/);
b.click('clear-filters');assert.match(b.element('run-count').textContent,/Page 1 of 500/);assert.match(b.element('recording-count').textContent,/Page 1 of 5/);
""")

    def test_dom_invalid_filter_clears_claims_and_preserves_whole_quality(self):
        self.run_js(r"""
const b=browser({schema_version:1,runs:[row('a'),row('b')],recordings:[{id:'WHOLE',record_count:80}]});
b.change('filter-from','2026-02-30T00:00:00Z');b.click('apply-filters');
assert.match(b.element('explorer-error').textContent,/invalid/);assert.match(b.element('run-count').textContent,/^0 /);assert.match(b.element('cohort-count').textContent,/^0 /);
assert.match(b.element('recording-list').textContent,/WHOLE/);assert.equal(b.element('run-next').disabled,true);
b.click('clear-filters');assert.equal(b.element('explorer-error').textContent,'');assert.match(b.element('run-count').textContent,/^2 /);
""")

    def test_hostile_text_details_and_no_dynamic_links(self):
        self.run_js(r"""
const hostile='<img src=x onerror=attack()>',a=row(hostile,null,{context:{test_id:hostile},source_sha256:[hostile]});
const b=browser({schema_version:1,runs:[a],recordings:[]});
assert.match(b.element('run-list').textContent,/<img/);
const article=b.element('run-list').children[0],detail=article.children.find(n=>n.tagName==='details');
assert.equal(detail.children.length,1);detail.open=true;detail.listeners.toggle();assert.match(detail.textContent,/source_sha256/);assert.match(detail.textContent,/<img/);
const walk=n=>[n,...n.children.flatMap(walk)];assert.equal(walk(b.element('run-list')).filter(n=>['img','a','script'].includes(n.tagName)).length,0);
""")

    def test_visible_metrics_snapshot_identity_and_exact_pre_epoch_formatting(self):
        self.run_js(r"""
assert.equal(api.formatUTC(-1n),'1969-12-31T23:59:59.999999999Z');
const a=row('a','1000000001');a.statistics.tracking[0].steering_rmse_rad=0.125;a.statistics.tracking[0].coverage_fraction=0.98;
a.statistics.currents={drive:[{module_position:'front-left',mean_amps:12,peak_amps:20,eligible_seconds:8}]};
const b=browser({schema_version:1,id:'report-exact',created_utc_ns:'1000000001',runs:[a],recordings:[]});
assert.match(b.element('snapshot-identity').textContent,/report-exact/);assert.match(b.element('snapshot-identity').textContent,/1970-01-01T00:00:01.000000001Z/);
assert.match(b.element('run-list').textContent,/Drive RMSE \(m\/s\)/);assert.match(b.element('run-list').textContent,/0.125/);assert.match(b.element('run-list').textContent,/Mean \(A\)/);assert.match(b.element('run-list').textContent,/Unavailable/);
""")

    def test_template_fixed_placeholders_exports_and_accessibility(self):
        html = (JS.parent / "summary_explorer.html").read_text(encoding="utf-8")
        for placeholder in ("%%SUMMARY_DATA%%", "%%SUMMARY_SCRIPT%%", "%%SCRIPT_CSP%%"):
            self.assertEqual(html.count(placeholder), 1)
        self.assertIn('<script>%%SUMMARY_SCRIPT%%</script>', html)
        for name in ('summary.csv', 'summary.json', 'manifest.json'):
            self.assertIn('href="' + name + '"', html)
        self.assertIn('<noscript>', html)
        self.assertIn('role="alert"', html)
        self.assertNotIn('innerHTML', JS.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
