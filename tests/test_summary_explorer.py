"""Portable summaries preserve complete evidence and cached numeric work."""
import base64
import copy
import hashlib
from html.parser import HTMLParser
from importlib import resources
import json
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from robot_test_hub import portable_summary,summaries
from robot_test_hub.columnar import Converter,ColumnarError,dependency,SUMMARY_VERSION
from robot_test_hub.importer import Importer
from robot_test_hub.runs import rebuild_from_imports
from robot_test_hub.storage import open_catalog
from robot_test_hub.analytics import report_file
from robot_test_hub.swerve import MODULE_POSITIONS


class Document(HTMLParser):
    def __init__(self,text):
        super().__init__();self.scripts=[];self.csp=None;self.current=None
        self.feed(text)
    def handle_starttag(self,tag,attributes):
        attributes=dict(attributes)
        if tag=='script':
            self.current={'attributes':attributes,'body':''};self.scripts.append(self.current)
        if tag=='meta' and attributes.get('http-equiv','').lower()=='content-security-policy':self.csp=attributes['content']
    def handle_data(self,data):
        if self.current is not None:self.current['body']+=data
    def handle_endtag(self,tag):
        if tag=='script':self.current=None
    def summary(self):
        return json.loads(next(row['body'] for row in self.scripts if row['attributes'].get('id')=='summary-data'))


def output(identity,metrics,cohort='cohort'):
    return {'run':{'run_id':identity},'cohort_id':cohort,'context':{'test_id':'practice'},
            'statistics':{'tracking':[{'module_position':position,'drive_rmse_mps':value} for position,value in metrics]}}


class ExplorerRendererTests(unittest.TestCase):
    def test_full_snapshot_hostile_labels_and_exact_hashed_script(self):
        hostile='</script><script>alert(1)</script><img src=x onerror=alert(2)>&'+chr(0x2028)+chr(0x2029)+'%%SUMMARY_SCRIPT%%%%SCRIPT_CSP%%'
        snapshot={'schema_version':1,'runs':[{'id':str(index),'label':hostile} for index in range(10000)],'cohorts':[],
                  'created_utc_ns':'1791003600020000123','extra':{'hostile':hostile}}
        text=portable_summary.render_report(snapshot)
        doc=Document(text)
        self.assertEqual(doc.summary(),snapshot)
        self.assertEqual(len(doc.scripts),2)
        script=next(row for row in doc.scripts if row['attributes'].get('type')!='application/json')
        pin='sha256-'+base64.b64encode(hashlib.sha256(script['body'].encode('utf-8')).digest()).decode()
        self.assertIn("script-src '"+pin+"'",doc.csp)
        self.assertNotIn("script-src 'unsafe-inline'",doc.csp)
        payload=next(row['body'] for row in doc.scripts if row['attributes'].get('id')=='summary-data')
        for character in ('<','>','&',chr(0x2028),chr(0x2029)):self.assertNotIn(character,payload)
        self.assertNotIn(hostile,text)
        self.assertIn('%%SUMMARY_SCRIPT%%%%SCRIPT_CSP%%',payload)
        self.assertNotIn('src',script['attributes'])

    def test_assets_are_resource_traversable_including_zip_package(self):
        snapshot={'runs':[],'recordings':[],'cohorts':[],'created_utc_ns':'0'}
        original=portable_summary.render_report(snapshot)
        with tempfile.TemporaryDirectory() as temporary:
            archive=Path(temporary)/'package.zip'
            with zipfile.ZipFile(archive,'w') as stream:
                for name in ('summary_explorer.html','summary_explorer.js'):
                    data=resources.files('robot_test_hub').joinpath('static').joinpath(name).read_bytes()
                    stream.writestr('robot_test_hub/static/'+name,data)
            with zipfile.ZipFile(archive) as stream,patch('robot_test_hub.portable_summary.resources.files',return_value=zipfile.Path(stream,'robot_test_hub/')):
                self.assertEqual(portable_summary.render_report(snapshot),original)

    def test_bad_templates_and_script_end_tags_fail_closed(self):
        template='<meta content="%%SCRIPT_CSP%%"><script id="summary-data" type="application/json">%%SUMMARY_DATA%%</script><script>%%SUMMARY_SCRIPT%%</script>'
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);(root/'static').mkdir()
            html=root/'static/summary_explorer.html';js=root/'static/summary_explorer.js'
            for bad_template,bad_js in ((template.replace('%%SUMMARY_DATA%%',''),''),(template+'%%SUMMARY_DATA%%',''),(template,'const bad="</ScRiPt>";')):
                html.write_text(bad_template,encoding='utf-8');js.write_text(bad_js,encoding='utf-8')
                with patch('robot_test_hub.portable_summary.resources.files',return_value=root):
                    with self.assertRaises(portable_summary.PresentationError):portable_summary.render_report({'runs':[]})
        with self.assertRaises(ValueError):portable_summary.render_report({'runs':[],'metric':float('nan')})

    def test_cohorts_match_module_labels_and_count_distinct_runs(self):
        positions=list(MODULE_POSITIONS)
        a=output('a',list(zip(positions,(1.,2.,3.,4.))))
        b=output('b',list(reversed(list(zip(positions,(3.,4.,5.,6.))))))
        rows=summaries.aggregate_cohorts([a,b,copy.deepcopy(a),output('excluded',[(positions[0],999)],None)])
        by_position={row['module_position']:row for row in rows}
        for index,position in enumerate(positions):
            row=by_position[position]
            self.assertEqual(row['independent_run_count'],2)
            self.assertEqual(row['runs_with_recorded_metric'],2)
            self.assertEqual(row['mean_recorded_run_rmse_mps'],index+2.)
            self.assertEqual((row['health_verdict'],row['baseline_approved']),('not_evaluated',False))

    def test_ambiguous_missing_and_nonfinite_cohort_metrics_stay_unavailable(self):
        position=MODULE_POSITIONS[0]
        candidates=[output('duplicate-position',[(position,1.),(position,2.)]),output('missing',[]),
                    output('nan',[(position,float('nan'))]),output('infinite',[(position,float('inf'))]),
                    output('null',[(position,None)]),output('boolean',[(position,True)]),
                    output('huge-int',[(position,10**400)]),output('same-run',[(position,1.)]),output('same-run',[(position,2.)])]
        rows=summaries.aggregate_cohorts(candidates)
        self.assertEqual(rows[0]['independent_run_count'],8)
        for row in rows:
            self.assertEqual(row['runs_with_recorded_metric'],0)
            self.assertIsNone(row['mean_recorded_run_rmse_mps'])
            self.assertIsNone(row['minimum_recorded_rmse_mps'])
            self.assertIsNone(row['maximum_recorded_rmse_mps'])
        huge=summaries.aggregate_cohorts([output('a',[(position,1e308)]),output('b',[(position,1e308)])])
        self.assertEqual(huge[0]['mean_recorded_run_rmse_mps'],1e308)
        maximum=sys.float_info.max
        extreme=summaries.aggregate_cohorts([output(str(i),[(position,maximum)]) for i in range(3)])
        self.assertEqual(extreme[0]['mean_recorded_run_rmse_mps'],maximum)


class ExplorerGenerationTests(unittest.TestCase):
    def setUp(self):
        try:dependency()
        except ColumnarError:self.skipTest('Pinned analytics extra unavailable')
        temporary=tempfile.TemporaryDirectory(prefix='portable summaries ')
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve()/'data'
        self.root.mkdir()
        self.db=open_catalog(self.root);self.addCleanup(self.db.close)
        source=Path(__file__).parent/'fixtures/synthetic/alpha7-main.wpilog'
        self.job=Importer(self.root,self.db).import_file(source,source_type='SYNTHETIC')
        self.converter=Converter(self.root,self.db,threads=1,memory_mb=256)
        self.converter.convert(self.job['id'])
        rebuild_from_imports(self.root,self.db)

    def test_presentation_upgrade_preserves_old_reports_and_numeric_cache(self):
        self.assertEqual(summaries.STATISTICS_VERSION,'summary-1')
        self.assertNotEqual(SUMMARY_VERSION,summaries.STATISTICS_VERSION)
        with patch('robot_test_hub.summaries.SUMMARY_VERSION','summary-1'):
            old,queried,cached=summaries.Summarizer(self.root,self.db,self.converter).generate()
        self.assertEqual((queried,cached),(2,0))
        old_rows=self.db.execute('SELECT path,sha256 FROM summary_artifacts WHERE report_id=?',(old['id'],)).fetchall()
        old_bytes={row['path']:(self.root/row['path']).read_bytes() for row in old_rows}
        cache_before=[tuple(row) for row in self.db.execute('SELECT * FROM summary_run_cache ORDER BY id')]
        conversion_before=[tuple(row) for row in self.db.execute('SELECT * FROM columnar_jobs ORDER BY id')]
        with patch('robot_test_hub.summaries.connection',side_effect=AssertionError('No numeric requery')):
            new,queried,cached=summaries.Summarizer(self.root,self.db,self.converter).generate()
        self.assertNotEqual(new['id'],old['id'])
        self.assertEqual((queried,cached),(0,2))
        self.assertEqual([tuple(row) for row in self.db.execute('SELECT * FROM summary_run_cache ORDER BY id')],cache_before)
        self.assertEqual([tuple(row) for row in self.db.execute('SELECT * FROM columnar_jobs ORDER BY id')],conversion_before)
        for relative,expected in old_bytes.items():self.assertEqual((self.root/relative).read_bytes(),expected)
        snapshot=json.loads(report_file(self.root,new['id'],'summary.json'))
        html=report_file(self.root,new['id'],'report.html').decode('utf-8')
        self.assertEqual(Document(html).summary(),snapshot)
        self.assertEqual((snapshot['html_run_limit'],snapshot['html_pagination'],snapshot['full_summary_run_count']),(20,True,2))
        self.assertFalse(snapshot['hardware_thresholds_qualified'])
        manifest=json.loads(report_file(self.root,new['id'],'manifest.json'))
        self.assertEqual(manifest['version'],SUMMARY_VERSION)
        self.assertEqual(manifest['run_count'],2)
        for item in manifest['artifacts']:
            data=(self.root/item['path']).read_bytes()
            self.assertEqual((len(data),hashlib.sha256(data).hexdigest()),(item['size_bytes'],item['sha256']))


if __name__=='__main__':unittest.main()
