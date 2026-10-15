"""Author tests for exact saved incident evidence, without live inputs."""
from contextlib import closing
from dataclasses import asdict
import copy
import hashlib
import json
from pathlib import Path
import socket
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from robot_test_hub import regression_evidence as subject, reports
from robot_test_hub.analysis import canonical, identity
from robot_test_hub.importer import Importer
from robot_test_hub.pipeline import Pipeline
from robot_test_hub.review import ReviewStore
from robot_test_hub.storage import open_catalog

FIXTURE = Path(__file__).parent/'fixtures/synthetic/alpha7-main.wpilog'


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='regression evidence ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()/'data'; self.root.mkdir()
        self.db = open_catalog(self.root); self.addCleanup(self.db.close)
        self.imported = Importer(self.root,self.db).import_file(FIXTURE,source_type='SYNTHETIC')
        self.assertEqual(self.imported['state'],'succeeded')
        self.assertEqual(Pipeline(self.root,self.db).tick()['runs'],2)
        self.job = next(json.loads(row[0]) for row in self.db.execute('SELECT result_json FROM analyzer_jobs')
                        if json.loads(row[0])['findings'])
        self.review = ReviewStore(self.db).review_finding({'id':'incident','job_id':self.job['job_id'],
            'finding_index':0,'disposition':'unresolved','reviewer':'Synthetic exercise','rationale':'Exact incident'})

    def capture(self):
        return subject.capture(self.root,review_id='incident',review_revision=1)

    def assert_code(self, call, code='regression_evidence_integrity'):
        with self.assertRaises(subject.EvidenceError) as found: call()
        self.assertEqual(found.exception.code,code)
        self.assertEqual(str(found.exception),code)
        self.assertNotIn(str(self.root),str(found.exception))

    def replace_job(self, change):
        job=copy.deepcopy(self.job);change(job)
        new_id=identity(['analysis-framework-2',job['declaration'],job['provenance'],job['configuration']])
        job['job_id']=new_id
        with self.db:
            self.db.execute('UPDATE analyzer_jobs SET job_id=?,input_json=?,configuration_json=?,result_json=? WHERE job_id=?',
                (new_id,canonical(job['provenance']),canonical(job['configuration']),canonical(job),self.job['job_id']))
            review={**self.review,'job_id':new_id,'finding_identity':identity([new_id,0,job['findings'][0]])}
            self.db.execute("UPDATE review_records SET payload_json=? WHERE kind='finding_review'",(canonical(review),))
        return job

    def test_actual_public_incident_exact_provenance_and_original_reimport_proof(self):
        capture=self.capture()
        self.assertEqual(capture['review'],self.review)
        self.assertEqual(capture['job'],self.job)
        self.assertEqual(capture['framework_version'],'analysis-framework-2')
        self.assertEqual(identity(capture['evidence']['rows']),self.job['provenance']['rows_digest'])
        self.assertEqual(capture['originals'][0]['path'].read_bytes(),FIXTURE.read_bytes())
        before={p.relative_to(self.root):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with patch.object(socket,'socket',side_effect=AssertionError('network forbidden')):
            receipt=subject.verify_originals(capture['originals'],capture['imports'],capture['evidence'],capture['job'])
        self.assertEqual(receipt,{'verified':True,'original_count':1,'import_count':1,'rows_digest':self.job['provenance']['rows_digest']})
        self.assertEqual(before,{p.relative_to(self.root):p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_exact_revision_is_not_current_review_or_plan(self):
        old=self.capture()
        ReviewStore(self.db).review_finding({**self.review,'revision':2,'expected_revision':1,'disposition':'false_alarm'})
        with self.db:
            self.db.execute('DELETE FROM run_catalog_state')
            self.db.execute('DELETE FROM run_catalog_revisions')
        with patch.object(reports,'review_snapshot',side_effect=AssertionError('no latest review')),             patch.object(reports,'resolve_plan',side_effect=AssertionError('no current plan')),             patch.object(Importer,'__init__',side_effect=AssertionError('no schema writer')):
            self.assertEqual(self.capture(),old)

    def test_coordinated_context_rehash_cannot_fabricate_selector_result(self):
        self.replace_job(lambda job:job['provenance']['context'].update(recorded_build_sha256=['f'*64]))
        self.assert_code(self.capture)

    def test_coordinated_rows_digest_rehash_cannot_fabricate_input(self):
        self.replace_job(lambda job:job['provenance'].update(rows_digest='0'*64))
        self.assert_code(self.capture)

    def test_unsupported_selector_and_decoder_fail_honestly(self):
        self.replace_job(lambda job:job['provenance']['context'].update(row_selection='future-selector'))
        self.assert_code(self.capture,'regression_evidence_unsupported')

    def test_import_state_and_canonical_paths_never_fall_back(self):
        with self.db:self.db.execute("UPDATE import_jobs SET state='failed'")
        self.assert_code(self.capture)
        with self.db:self.db.execute("UPDATE import_jobs SET state='succeeded',dataset_path='../private.jsonl'")
        self.assert_code(self.capture)

    def test_dataset_and_manifest_tamper_are_not_repaired(self):
        for name in ('dataset_path','manifest_path'):
            path=self.root/self.imported[name]; before=path.read_bytes(); changed=before+b' '
            path.write_bytes(changed)
            try:self.assert_code(self.capture)
            finally:path.write_bytes(before)

    def test_resource_limits_include_raw_selected_input_metadata_and_sql(self):
        for field,limit in (('MAX_ORIGINAL_BYTES',1),('MAX_TOTAL_ORIGINAL_BYTES',1),
                            ('MAX_INPUT_BYTES',1),('MAX_METADATA_BYTES',1),('MAX_VM_STEPS',0)):
            with self.subTest(field=field),patch.object(subject,field,limit):
                self.assert_code(self.capture,'regression_evidence_resource_limit')

    def test_case_rehashing_fake_rows_still_fails_original_relation(self):
        capture=self.capture();evidence=copy.deepcopy(capture['evidence']);job=copy.deepcopy(capture['job'])
        evidence['rows']=evidence['rows'][:-1]
        job['provenance']['rows_digest']=identity(evidence['rows'])
        job['job_id']=identity(['analysis-framework-2',job['declaration'],job['provenance'],job['configuration']])
        self.assert_code(lambda:subject.verify_originals(capture['originals'],capture['imports'],evidence,job))

    def test_original_proof_rejects_import_pin_tamper_and_cleans_temp(self):
        capture=self.capture();pins=copy.deepcopy(capture['imports']);pins[0]['dataset_sha256']='f'*64
        created=[];real=subject.tempfile.TemporaryDirectory
        def tracked(*args,**kwargs):
            temp=real(*args,**kwargs);created.append(Path(temp.name));return temp
        with patch.object(subject.tempfile,'TemporaryDirectory',side_effect=tracked):
            self.assert_code(lambda:subject.verify_originals(capture['originals'],pins,capture['evidence'],capture['job']))
        self.assertTrue(created);self.assertTrue(all(not p.exists() for p in created))

    def test_missing_archive_never_created_and_invalid_reference_redacted(self):
        missing=self.root/'missing'
        self.assert_code(lambda:subject.capture(missing,review_id='incident',review_revision=1),'regression_evidence_unavailable')
        self.assertFalse(missing.exists())
        for value in (0,True,'1'):
            self.assert_code(lambda:subject.capture(self.root,review_id='incident',review_revision=value),'invalid_regression_reference')
        self.assert_code(lambda:subject.capture(self.root,review_id='../private',review_revision=1),'invalid_regression_reference')

    def test_actual_saved_plan_changes_do_not_retarget_swerve_case(self):
        from test_auto_swerve import protocol_recording, UTC
        from robot_test_hub.swerve import MODULE_POSITIONS
        store=ReviewStore(self.db)
        for index,position in enumerate(MODULE_POSITIONS):
            store.assign_component({'id':'component-'+str(index),'robot_id':'author-robot',
                'component_id':'module-'+str(index),'location':position,'start_utc_ns':str(UTC-1000000),
                'end_utc_ns':None,'reviewer':'Author','rationale':'Invented assignment'})
        plan={'id':'saved-plan','robot_id':'author-robot','start_utc_ns':str(UTC-1000000),'end_utc_ns':None,
            'reviewer':'Author','rationale':'Invented saved policy','build_hash':'a'*64,'config_hash':'b'*64,
            'test_id':'case','surface':'invented-flat','battery_id':'invented-battery','wheel_radius_m':.05,
            'swerve_configuration':{'drive_error_limit_mps':.1,'minimum_eligible_seconds':.1,
                                    'sustained_error_seconds':.1,'threshold_revision':'saved-threshold'},
            'ideal_simulation_cycle_policy':True}
        first=store.record_analysis_plan(plan)
        source=Path(self.temp.name)/'invented.wpilog';source.write_bytes(protocol_recording())
        Importer(self.root,self.db).import_file(source,source_type='MANUAL_LOCAL')
        Pipeline(self.root,self.db).tick()
        job=next(json.loads(row[0]) for row in self.db.execute("SELECT result_json FROM analyzer_jobs WHERE analyzer_id='swerve-tracking'")
                 if json.loads(row[0])['findings'])
        review=store.review_finding({'id':'plan-incident','job_id':job['job_id'],'finding_index':0,
            'disposition':'unresolved','reviewer':'Author','rationale':'Saved plan incident'})
        captured=subject.capture(self.root,review_id=review['id'],review_revision=1)
        store.record_analysis_plan({**first,'revision':2,'expected_revision':1,
                                   'swerve_configuration':{'drive_error_limit_mps':.75,'threshold_revision':'later-threshold'}})
        self.assertEqual(subject.capture(self.root,review_id=review['id'],review_revision=1),captured)
        self.assertEqual(captured['evidence']['context']['analysis_plan'],first)
        self.assertTrue(subject.verify_originals(captured['originals'],captured['imports'],captured['evidence'],captured['job'])['verified'])

    def test_closed_wal_archive_preserves_all_evidence_bytes_allowing_sqlite_sidecars(self):
        # Read-only SQLite can create WAL coordination sidecars even though SQL
        # and the catalog/raw/derived evidence remain read-only. Never remove
        # those journals or use immutable=1 on a potentially live WAL archive.
        self.assertEqual(self.db.execute('PRAGMA journal_mode=WAL').fetchone()[0],'wal')
        self.db.close()
        before={str(p.relative_to(self.root)):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        captured=self.capture()
        self.assertEqual(captured['job'],self.job)
        after={str(p.relative_to(self.root)):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        for name,data in before.items():self.assertEqual(after[name],data)
        self.assertLessEqual(set(after)-set(before),{'catalog.sqlite3-shm','catalog.sqlite3-wal'})

    def test_readonly_uri_query_only_and_explicit_close(self):
        seen=[];connections=[];real=sqlite3.connect
        class Connection(sqlite3.Connection):
            closed=False
            def close(self):self.closed=True;super().close()
        def connected(path,*args,**kwargs):
            self.assertTrue(kwargs.get('uri'));self.assertTrue(path.endswith('?mode=ro'))
            db=real(path,*args,**kwargs,factory=Connection);connections.append(db)
            def authorize(action,*rest):
                self.assertNotIn(action,(sqlite3.SQLITE_INSERT,sqlite3.SQLITE_UPDATE,sqlite3.SQLITE_DELETE,sqlite3.SQLITE_CREATE_TABLE))
                return sqlite3.SQLITE_OK
            db.set_authorizer(authorize);db.set_trace_callback(seen.append);return db
        with patch.object(subject.sqlite3,'connect',side_effect=connected):self.capture()
        self.assertTrue(all(c.closed for c in connections));self.assertIn('PRAGMA query_only=ON',seen)


if __name__=='__main__':unittest.main()
