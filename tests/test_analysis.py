import math
import sqlite3
import unittest
from dataclasses import replace
from robot_test_hub.analysis import Analyzer, Declaration, Evidence, Runner, Signal, data_quality_analyzer, metric, result
from robot_test_hub.swerve import SAMPLE_PROFILE, swerve_analyzer, weighted_percentile, wrap

PROFILE = 'synthetic-explicit-analysis-1'
HASH = 'a'*64
CONTEXT = {'run_id':'run','boot_id':'boot','robot_id':'synthetic-6391','config_hash':'b'*64,
           'test_id':'D1','surface':'synthetic-flat','battery_id':'synthetic-battery','mapping_revision':'explicit-1'}


def evidence(rows=(), **context):
    return Evidence(PROFILE,(HASH,),tuple(rows),{**CONTEXT,**context},'mapping-1','synthetic-generator-1','synthetic')


def sample(t, speed=1, measured=1, angle=0, actual_angle=0, module='fl', **extra):
    return {'kind':'swerve_sample','timestamp_ns':t,'module_id':module,'module_position':module,
            'enabled':True,'connected':True,'fresh':True,'validity':'valid',
            'command_speed_mps':speed,'measured_speed_mps':measured,
            'command_angle_rad':angle,'measured_angle_rad':actual_angle,
            'command_timestamp_ns':t,'measurement_timestamp_ns':t,
            'command_stage':'final_io_optimized_cosine_desaturated','sample_profile':SAMPLE_PROFILE,
            'speed_unit':'meters per second','angle_unit':'radians','motion':'translation',
            'source_reference':{'source_hash':HASH,'record_index':t//100_000_000},**extra}


def series(**kwargs):
    return [sample(t,**kwargs) for t in range(0,2_000_000_001,100_000_000)]


class AnalyzerFrameworkTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.runner = Runner(self.db)

    def analyzer(self, outcome='evaluated_no_finding', **kwargs):
        return Analyzer(Declaration('test','1',(PROFILE,),**kwargs),lambda e,c:result(outcome))

    def test_all_five_outcomes_and_failure_isolation(self):
        good = self.analyzer()
        finding = Analyzer(Declaration('finding','1',(PROFILE,)),lambda e,c:result('finding',findings=[{'observation':'known fixture'}]))
        missing = Analyzer(Declaration('missing','1',(PROFILE,),required=(Signal('/missing','radians'),)),lambda e,c:result('evaluated_no_finding'))
        unsupported = Analyzer(Declaration('unsupported','1',('other',)),lambda e,c:result('evaluated_no_finding'))
        bad = Analyzer(Declaration('bad','1',(PROFILE,)),lambda e,c:1/0)
        report = self.runner.report([bad,missing,good,finding,unsupported],evidence())
        self.assertEqual([c['outcome'] for c in report['checks']],['failed','insufficient_data','evaluated_no_finding','finding','unsupported'])
        self.assertEqual(report['evaluated_checks'],2)
        self.assertEqual(report['unavailable_checks'],3)
        self.assertEqual(report['overall_health'],'not_assessed')
        self.assertIn('ZeroDivisionError', report['checks'][0]['unavailable'][0])

    def test_jobs_pin_versions_configs_rows_and_provenance(self):
        first = self.runner.run(self.analyzer(),evidence(),{'threshold':1})
        second = self.runner.run(self.analyzer(),evidence(),{'threshold':1})
        self.assertEqual(first,second)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM analyzer_jobs').fetchone()[0],1)
        changed = self.runner.run(self.analyzer(),evidence(),{'threshold':2})
        self.assertNotEqual(first['job_id'],changed['job_id'])
        upgraded = Analyzer(Declaration('test','2',(PROFILE,)),lambda e,c:result('evaluated_no_finding'))
        self.assertNotEqual(first['job_id'],self.runner.run(upgraded,evidence(),{'threshold':1})['job_id'])
        self.assertEqual(first['provenance']['source_type'],'synthetic')
        self.assertEqual(first['provenance']['source_hashes'],[HASH])

    def test_units_categories_and_missing_values_never_pass(self):
        analyzer = self.analyzer(required=(Signal('/Speed','meters per second'),))
        row = {'kind':'observation','field':'/Speed','unit':'radians','category':'input','value':1,'validity':'valid'}
        self.assertEqual(self.runner.run(analyzer,evidence([row]))['outcome'],'unsupported')
        row.update(unit='meters per second',category='replay_output')
        self.assertEqual(self.runner.run(analyzer,evidence([row]))['outcome'],'insufficient_data')
        row.update(category='input',value=None,validity='nonfinite')
        self.assertEqual(self.runner.run(analyzer,evidence([row]))['outcome'],'insufficient_data')

    def test_report_idempotency_and_unknown_profile(self):
        checks = [self.analyzer()]
        a = self.runner.report(checks,evidence())
        b = self.runner.report(checks,evidence())
        self.assertEqual(a,b)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM analysis_reports').fetchone()[0],1)
        unknown = replace(evidence(),profile='unknown')
        self.assertEqual(self.runner.run(checks[0],unknown)['outcome'],'unsupported')

    def test_baseline_requirement_and_exception_redaction(self):
        a = self.analyzer(baseline_required=True)
        self.assertEqual(self.runner.run(a,evidence())['outcome'],'insufficient_data')
        def fail(e,c):
            raise RuntimeError('secret=not-public')
        bad = Analyzer(Declaration('secret-failure','1',(PROFILE,)),fail)
        self.assertNotIn('not-public',str(self.runner.run(bad,evidence())))

    def test_evidence_and_metrics_reject_nonfinite_or_missing_provenance(self):
        with self.assertRaises(ValueError):
            Evidence(PROFILE,(),(),CONTEXT,'1','1','synthetic')
        with self.assertRaises(ValueError):
            evidence([{'value':float('nan')}])
        with self.assertRaises(ValueError):
            metric('bad',float('nan'),'volts')

    def test_data_quality_gaps_unknown_epoch_and_record_provenance(self):
        rows = [{'kind':'cycle','timestamp_ns':str(t),'aliases':{'epoch_valid':{'value':True}}} for t in [0,20_000_000,1_000_000_000]]
        rows += [{'kind':'observation','field':'/Sensor','validity':'nonfinite','record_index':7}]
        out = self.runner.run(data_quality_analyzer([PROFILE]),evidence(rows))
        self.assertEqual(out['outcome'],'finding')
        metrics = {m['name']:m['value'] for m in out['metrics']}
        self.assertEqual(metrics['gap_count'],1)
        self.assertAlmostEqual(metrics['valid_cycle_coverage'],.02)
        self.assertEqual(out['findings'][0]['intervals_ns'],[['20000000','1000000000']])
        self.assertEqual(out['findings'][1]['record_indices'],[7])

    def test_data_quality_empty_and_unknown_clock_coverage(self):
        quality = data_quality_analyzer([PROFILE])
        self.assertEqual(self.runner.run(quality,evidence())['outcome'],'insufficient_data')
        rows = [{'kind':'cycle','timestamp_ns':str(t)} for t in [0,20_000_000]]
        out = self.runner.run(quality,evidence(rows))
        self.assertEqual(out['coverage']['wall_clock_valid_cycles'],0)
        self.assertIn('wall_clock_invalid_or_unknown',out['unavailable'])

    def test_imported_disconnect_observation_and_samples_preserve_provenance(self):
        field = '/Drive/Module0/DriveConnected'
        rows = [{'kind':'cycle','timestamp_ns':str(t),'aliases':{'epoch_valid':{'value':True}}}
                for t in (0,100_000_000,200_000_000)]
        ref = {'field':field,'type':'boolean','value':False,'validity':'valid','record_index':17}
        rows += [{**ref,'kind':'observation','source_sha256':HASH},
                 {'kind':'sample','sensor_availability':'disconnected','source_sha256':HASH,
                  'connection_sources':{field:ref}}]
        out = self.runner.run(data_quality_analyzer([PROFILE]),evidence(rows))
        self.assertEqual(out['outcome'],'finding')
        finding = next(f for f in out['findings'] if f['kind']=='disconnected_observations')
        self.assertEqual(finding['count'],1)
        self.assertEqual(finding['record_indices'],[17])
        self.assertEqual(finding['source_references'],[{'field':field,'record_index':17,'source_sha256':HASH}])
        # Report row selection may retain only derived sample statuses.
        sample_only = self.runner.run(data_quality_analyzer([PROFILE]),evidence(rows[:3]+rows[4:]))
        self.assertEqual(sample_only['outcome'],'finding')
        self.assertEqual(sample_only['findings'][0]['record_indices'],[17])

    def test_missing_connection_and_freshness_checks_are_explicit(self):
        rows = [{'kind':'cycle','timestamp_ns':str(t),'aliases':{'epoch_valid':{'value':True}}}
                for t in (0,100_000_000)]
        out = self.runner.run(data_quality_analyzer([PROFILE]),evidence(rows))
        self.assertIn('sensor_connection_status_not_recorded',out['unavailable'])
        self.assertIn('sensor_acquisition_freshness_not_recorded',out['unavailable'])
        self.assertTrue(out['coverage']['has_unavailable_checks'])
        rows += [{'kind':'sample','sensor_availability':'unknown'}]
        out = self.runner.run(data_quality_analyzer([PROFILE]),evidence(rows))
        self.assertIn('sample_sensor_connection_status_unknown',out['unavailable'])

    def test_genuine_fixture_disconnected_status_reaches_quality_check(self):
        import hashlib
        from pathlib import Path
        from robot_test_hub.wpilog import extract, PROFILE as IMPORT_PROFILE
        path = Path(__file__).parent / 'fixtures/synthetic/alpha7-main.wpilog'
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        rows = tuple({**r,'source_sha256':digest} for r in extract(path))
        source = replace(evidence(),profile=IMPORT_PROFILE,source_hashes=(digest,),rows=rows)
        out = self.runner.run(data_quality_analyzer([IMPORT_PROFILE]),source)
        disconnections = next(f for f in out['findings'] if f['kind']=='disconnected_observations')
        self.assertEqual(disconnections['count'],1)
        self.assertEqual(disconnections['source_references'][0]['field'],'/Drive/Module0/Connected')
        self.assertEqual(disconnections['source_references'][0]['source_sha256'],digest)


class SwerveTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.runner = Runner(self.db)
        self.analyzer = swerve_analyzer([PROFILE])

    def analyze(self, rows, config=None, **context):
        return self.runner.run(self.analyzer,evidence(rows,**context),config)

    def metrics(self,out):
        return {m['name']:m['value'] for m in out['metrics']}

    def test_known_good_rotation_unequal_module_speed_is_no_finding(self):
        rows = series(module='fl',speed=2,measured=2,motion='rotation')+series(module='rr',speed=.5,measured=.5,motion='rotation')
        out = self.analyze(rows,{'drive_error_limit_mps':.5,'threshold_revision':'synthetic-limit-1'})
        self.assertEqual(out['outcome'],'evaluated_no_finding')
        self.assertEqual(out['coverage']['evaluated_modules'],2)
        self.assertTrue(all(m['value']==0 for m in out['metrics'] if 'error' in m['name'] or 'rmse' in m['name']))
        self.assertIn('approved_baseline_comparison_unavailable',out['unavailable'])

    def test_optimized_reverse_and_wrapped_angles_are_legitimate(self):
        rows = series(speed=-2,measured=-2,angle=math.pi-.02,actual_angle=-math.pi+.02)
        out = self.analyze(rows,{'steering_error_limit_rad':.1,'threshold_revision':'synthetic-limit-1'})
        self.assertEqual(out['outcome'],'evaluated_no_finding')
        self.assertAlmostEqual(self.metrics(out)['steering_rmse_rad'],.04)
        self.assertEqual(self.metrics(out)['drive_rmse_mps'],0)

    def test_time_weighted_rmse_p95_and_normalization_irregular_samples(self):
        rows = [sample(0,measured=0),sample(100_000_000,measured=1),sample(300_000_000,measured=1)]
        out = self.analyze(rows,{'minimum_eligible_seconds':.1})
        self.assertAlmostEqual(self.metrics(out)['drive_rmse_mps'],math.sqrt(1/3))
        self.assertEqual(self.metrics(out)['drive_p95_absolute_error_mps'],1)
        self.assertAlmostEqual(self.metrics(out)['normalized_drive_rmse'],math.sqrt(1/3))
        self.assertAlmostEqual(out['coverage']['modules'][0]['eligible_seconds'],.3)

    def test_sustained_lag_finding_requires_explicit_threshold_revision(self):
        config = {'drive_error_limit_mps':.5,'threshold_revision':'provisional-test-limit-1','sustained_error_seconds':.25}
        out = self.analyze(series(speed=2,measured=1),config)
        self.assertEqual(out['outcome'],'finding')
        self.assertEqual(out['findings'][0]['intervals_ns'],[['0','2000000000']])
        self.assertEqual(out['findings'][0]['threshold_revision'],'provisional-test-limit-1')
        self.assertIsNone(out['findings'][0]['baseline_version'])
        self.assertAlmostEqual(self.metrics(out)['drive_rmse_mps'],1)

    def test_stale_disconnected_gaps_and_nearzero_are_not_health_pass(self):
        for kwargs in ({'fresh':False},{'connected':False},{'speed':0,'measured':0},{'enabled':None}):
            out = self.analyze(series(**kwargs))
            self.assertEqual(out['outcome'],'insufficient_data')
            self.assertTrue(all(m['value'] is None for m in out['metrics']))
        out = self.analyze([sample(0),sample(1_000_000_000),sample(2_000_000_000)])
        self.assertEqual(out['outcome'],'insufficient_data')
        self.assertEqual(out['coverage']['modules'][0]['exclusions']['recording_gap'],2)

    def test_battery_sag_exclusion_and_no_current_ranking(self):
        out = self.analyze(series(battery_voltage_v=7,stator_current_a=100),{'minimum_voltage_v':9})
        self.assertEqual(out['outcome'],'insufficient_data')
        self.assertEqual(out['coverage']['modules'][0]['exclusions']['low_voltage_window'],20)
        out = self.analyze(series(stator_current_a=1000),{'drive_error_limit_mps':.5,'threshold_revision':'synthetic-limit-1'})
        self.assertEqual(out['outcome'],'evaluated_no_finding')
        self.assertFalse(out['findings'])

    def test_unqualified_commands_units_and_missing_identity_are_unavailable(self):
        self.assertEqual(self.analyze(series(command_stage='unoptimized'))['outcome'],'unsupported')
        self.assertEqual(self.analyze(series(speed_unit='rotations/sec'))['outcome'],'insufficient_data')
        self.assertEqual(self.analyze(series(module_id=None))['outcome'],'insufficient_data')
        self.assertEqual(self.analyze(series(),battery_id=None)['outcome'],'insufficient_data')

    def test_sample_timestamp_alignment_and_source_hash_provenance(self):
        self.assertEqual(self.analyze(series(measurement_timestamp_ns=-1))['outcome'],'insufficient_data')
        self.assertEqual(self.analyze(series(source_reference={'source_hash':'b'*64}))['outcome'],'unsupported')
        out = self.analyze(series())
        self.assertEqual(out['coverage']['modules'][0]['evidence_trace'][0]['source_reference']['source_hash'],HASH)
        self.assertEqual(out['provenance']['source_type'],'synthetic')

    def test_explicit_threshold_validation_is_failed_not_no_finding(self):
        self.assertEqual(self.analyze(series(),{'drive_error_limit_mps':.5})['outcome'],'failed')
        self.assertEqual(self.analyze(series(),{'minimum_coverage':2})['outcome'],'failed')

    def test_unconfigured_stalled_tracking_is_metrics_only_not_no_finding(self):
        out = self.analyze(series(speed=1,measured=0))
        self.assertEqual(out['outcome'],'insufficient_data')
        self.assertEqual(self.metrics(out)['drive_rmse_mps'],1.)
        self.assertEqual(out['coverage']['evaluated_modules'],1)
        self.assertFalse(out['coverage']['engineering_finding_criteria_configured'])
        self.assertFalse(out['coverage']['baseline_finding_criteria_evaluated'])
        self.assertIn('tracking_finding_criteria_not_configured_or_available',out['unavailable'])
        self.assertFalse(out['findings'])

    def test_metrics_only_jobs_cannot_become_approved_baseline(self):
        from robot_test_hub.review import ReviewStore
        jobs = [self.analyze(series(speed=1,measured=0),run_id='run-'+str(i)) for i in range(3)]
        with self.assertRaisesRegex(ValueError,'qualified no-finding'):
            ReviewStore(self.db).approve_baseline({'id':'not-qualified','label':'Metrics-only cohort',
                'reviewer':'test','rationale':'No configured finding criteria',
                'job_ids':[j['job_id'] for j in jobs]})

    def test_helpers_are_independently_known(self):
        self.assertAlmostEqual(wrap(2*math.pi+.2),.2)
        self.assertEqual(weighted_percentile([(1,.1),(0,.9)],.95),1)

from robot_test_hub.swerve import ROBOT_FIELDS, ROBOT_FORMAT_PROFILE, adapt_robot_swerve


def robot_mapping_rows():
    rows=[]
    def observation(field, value, kind, index, stamp=0):
        return {'kind':'observation','field':field,'value':value,'type':kind,'validity':'valid',
                'record_index':index,'record_timestamp_ns':str(stamp),'cycle_timestamp_ns':str(stamp)}
    rows += [observation('/.schema/struct:SwerveModuleVelocity','double velocity;Rotation2d angle','structschema',0),
             observation('/.schema/struct:Rotation2d','double value','structschema',1)]
    for i in range(4):
        for key in ('DriveConnected','TurnConnected','TurnEncoderConnected'):
            rows.append(observation(f'/Drive/Module{i}/'+key,True,'boolean',len(rows)))
    for key in ROBOT_FIELDS.values():
        rows.append(observation(key,[{'velocity':1.,'angle':{'value':0.}}]*4,'struct:SwerveModuleVelocity[]',len(rows)))
    for t in range(0,2_000_000_001,100_000_000):
        rows.append({'kind':'cycle','timestamp_ns':str(t),'timestamp_record_index':100+t//100_000_000,
                     'aliases':{'enabled':{'value':True,'validity':'valid','updated_in_cycle':False,'record_index':0}}})
    return rows


class RobotSwerveMappingTests(unittest.TestCase):
    def input(self, source_type='real'):
        return replace(evidence(robot_mapping_rows()),profile=ROBOT_FORMAT_PROFILE,source_type=source_type)

    def test_real_mapping_preserves_held_records_and_unknown_physical_freshness(self):
        mapped=adapt_robot_swerve(self.input(),{i:'physical-'+str(i) for i in range(4)})
        rows=[r for r in mapped.rows if r.get('kind')=='swerve_sample']
        self.assertFalse(rows[4]['fresh'])
        self.assertIsNone(rows[4]['measurement_timestamp_ns'])
        self.assertFalse(rows[4]['source_reference']['measured_updated_in_cycle'])
        self.assertEqual(rows[4]['source_reference']['measured_record_index'],15)
        db=sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        out=Runner(db).run(swerve_analyzer([ROBOT_FORMAT_PROFILE]),mapped)
        self.assertEqual(out['outcome'],'insufficient_data')
        self.assertEqual(out['coverage']['evaluated_modules'],0)

    def test_explicit_ideal_simulation_policy_and_real_rejection(self):
        with self.assertRaises(ValueError):
            adapt_robot_swerve(self.input(),{i:str(i) for i in range(4)},ideal_simulation_cycle_policy=True)
        mapped=adapt_robot_swerve(self.input('simulation'),{i:str(i) for i in range(4)},ideal_simulation_cycle_policy=True)
        db=sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        out=Runner(db).run(swerve_analyzer([ROBOT_FORMAT_PROFILE]),mapped,
                           {'drive_error_limit_mps':.5,'threshold_revision':'synthetic-limit-1'})
        self.assertEqual(out['outcome'],'evaluated_no_finding')
        self.assertEqual(out['coverage']['evaluated_modules'],4)
        self.assertEqual(out['provenance']['source_type'],'simulation')

    def test_multi_artifact_importer_hashes_are_retained_for_each_source(self):
        original = self.input('simulation')
        other = 'b'*64
        rows = [{**r,'source_sha256':other if r.get('field')==ROBOT_FIELDS['measured'] else HASH}
                for r in original.rows]
        original = replace(original,rows=tuple(rows),source_hashes=(HASH,other))
        mapped = adapt_robot_swerve(original,{i:str(i) for i in range(4)},ideal_simulation_cycle_policy=True)
        source = next(r['source_reference'] for r in mapped.rows if r.get('kind')=='swerve_sample')
        self.assertEqual(source['source_hash'],HASH)
        self.assertEqual(source['command_source_hash'],HASH)
        self.assertEqual(source['measured_source_hash'],other)
        self.assertEqual(source['cycle_source_hash'],HASH)
        self.assertEqual(source['connection_source_hashes'],[HASH]*3)
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        out = Runner(db).run(swerve_analyzer([ROBOT_FORMAT_PROFILE]),mapped,
                             {'drive_error_limit_mps':.5,'threshold_revision':'synthetic-limit-1'})
        self.assertEqual(out['outcome'],'evaluated_no_finding')
        # An explicit record hash must never be replaced by a singleton input
        # hash; measured evidence is checked independently from the command.
        bad = replace(original,source_hashes=(HASH,))
        bad = adapt_robot_swerve(bad,{i:str(i) for i in range(4)},ideal_simulation_cycle_policy=True)
        self.assertEqual(Runner(db).run(swerve_analyzer([ROBOT_FORMAT_PROFILE]),bad)['outcome'],'unsupported')

    def test_finished_or_reused_entries_remove_held_mapping_state(self):
        for field in (ROBOT_FIELDS['commands'],ROBOT_FIELDS['measured'],'/Drive/Module0/DriveConnected'):
            original = self.input('simulation')
            rows = list(original.rows)
            first_cycle = next(i for i,r in enumerate(rows) if r.get('kind')=='cycle')
            # Both finishing a path and reusing its entry id under a different
            # field invalidate its formerly held observation.
            old = next(i for i,r in enumerate(rows) if r.get('field')==field)
            rows[old] = {**rows[old],'entry_id':24,'entry_generation':1}
            for control in ({'control':'finish','field':field}, {'control':'start','field':'/Replacement'}):
                changed = list(rows)
                changed.insert(first_cycle,{'kind':'control','entry_id':24,**control})
                mapped = adapt_robot_swerve(replace(original,rows=tuple(changed)),{i:str(i) for i in range(4)},
                                           ideal_simulation_cycle_policy=True)
                samples = [r for r in mapped.rows if r.get('kind')=='swerve_sample']
                if field in ROBOT_FIELDS.values():
                    self.assertFalse(samples)
                else:
                    self.assertFalse(any(r['connected'] for r in samples if r['module_position']=='front-left'))

    def test_schema_and_component_identity_are_explicit(self):
        with self.assertRaises(ValueError):
            adapt_robot_swerve(self.input(),{0:'only-one'})
        original=self.input()
        rows=list(original.rows)
        rows[0]={**rows[0],'value':'double speed;Rotation2d angle'}
        with self.assertRaises(ValueError):
            adapt_robot_swerve(replace(original,rows=tuple(rows)),{i:str(i) for i in range(4)})

class FrameworkCoverageTests(unittest.TestCase):
    def test_declared_minimum_coverage_prevents_no_finding(self):
        db=sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        analyzer=Analyzer(Declaration('coverage','1',(PROFILE,),minimum_coverage=.9),
                          lambda e,c:result('evaluated_no_finding',coverage={'fraction':.5}))
        out=Runner(db).run(analyzer,evidence())
        self.assertEqual(out['outcome'],'insufficient_data')
        self.assertIn('declared_minimum_coverage_not_met',out['unavailable'])

    def test_declared_baseline_version_does_not_fabricate_comparison(self):
        db=sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        out=Runner(db).run(swerve_analyzer([PROFILE]),evidence(series(),approved_baseline_version='unloaded-v1'))
        self.assertIn('approved_baseline_comparison_unavailable',out['unavailable'])

    def test_analyzer_mutation_does_not_change_original_evidence_or_config(self):
        db=sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        original=evidence()
        config={'mutable':[1]}
        def mutate(e,c):
            e.context['robot_id']='mutated'
            c['mutable'].append(2)
            return result('evaluated_no_finding')
        out=Runner(db).run(Analyzer(Declaration('mutation','1',(PROFILE,)),mutate),original,config)
        self.assertEqual(original.context['robot_id'],'synthetic-6391')
        self.assertEqual(config,{'mutable':[1]})
        self.assertEqual(out['configuration'],{'mutable':[1]})

    def test_analyzer_schema_install_does_not_commit_caller_transaction(self):
        from robot_test_hub.analysis import install_schema
        db=sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.execute('BEGIN')
        install_schema(db)
        self.assertTrue(db.in_transaction)
        db.rollback()
        self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE name='analyzer_jobs'").fetchall(),[])
