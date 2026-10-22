"""Independent S3a numerical and persisted-finalization adversarial regressions.

Normalized samples below are explicit invented unit contracts, never promoted
original-log provenance. Original-pipeline checks import invented bytes as SYNTHETIC.
"""
import copy
import hashlib
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from robot_test_hub import reports, stream_analysis
from robot_test_hub import stream_tracking as tracking
from robot_test_hub import stream_motion_mapper as motion
from robot_test_hub import stream_tracking_store as work
from robot_test_hub.analysis import canonical
from robot_test_hub.importer import Importer
from robot_test_hub.runs import rebuild_from_imports
from robot_test_hub.storage import open_catalog
from robot_test_hub.swerve import MODULE_POSITIONS, SAMPLE_PROFILE
from test_auto_swerve import protocol_recording


def exact_p95(values):
    """Independent integer-weight definition, including the exact crossing."""
    total = sum(duration for _, duration in values)
    cumulative = 0
    for value, duration in sorted(values):
        cumulative += duration
        if cumulative * 100 >= total * 95:
            return value
    raise AssertionError('Nonempty positive-weight oracle required')


class StreamTrackingIndependentReviewTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='tracking independent review ')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.db = open_catalog(self.root)
        self.addCleanup(self.db.close)

    def final(self, sid, state, rows=2):
        for _ in range(10000):
            with self.db:
                final = work.finalize_step(self.db, sid, 1, state, rows, 1., lambda: 0.)
            if final['complete']:
                return final
        self.fail('Quantile finalization did not complete')

    def test_integer_p95_distinguishes_one_ns_beyond_large_exact_boundary(self):
        # A floating 0.95*total can round away the extra nanosecond and select 1.
        scale = 1 << 55
        values = [(1., 19 * scale), (9., scale + 1)]
        self.assertEqual(exact_p95(values), 9.)
        state = work.initial()
        with self.db:
            for line, (error, duration) in enumerate(values):
                work.contribute(self.db, 'review-integer', 1, state, line,
                                dict(stream_source_ordinal=0, stream_line_ordinal=line),
                                0, 'drive', error, duration)
        final = self.final('review-integer', state, rows=1)
        self.assertEqual(final['quantiles']['0:drive'], 9.)
        self.assertEqual(final['quantiles']['0:steering'], None)

    def test_irregular_weighted_metrics_match_independent_oracle(self):
        start = 1_000_000_000
        durations = [7_000_000, 11_000_000, 13_000_000, 17_000_000, 23_000_000, 29_000_000]
        drive_errors = [1., -2., .25, 4., -.5, 1.5, 0.]
        angle_errors = [.125, -.5, .25, -.25, .5, -.125, 0.]
        config = dict(minimum_eligible_seconds=.001, minimum_coverage=1,
                      drive_error_limit_mps=.5, steering_error_limit_rad=.05,
                      sustained_error_seconds=.001, threshold_revision='invented-review-1')
        context = dict(run_id='invented-numerical-run', boot_id='invented-boot', robot_id='invented-robot',
                       config_hash='b' * 64, test_id='normalized-contract', surface='invented-surface',
                       battery_id='invented-battery', mapping_revision='normalized-review-1',
                       stream_source_type='simulation', runtime_mode='SIM', completeness='complete',
                       component_ids={position: 'invented-module-' + str(i) for i, position in enumerate(MODULE_POSITIONS)},
                       start_monotonic_ns=str(start), end_monotonic_ns=str(start + sum(durations)),
                       analysis_plan=dict(ideal_simulation_cycle_policy=True, swerve_configuration=config))
        state = tracking.initial_tracking(context)
        stamp = start
        for line, error in enumerate(drive_errors):
            state['cycles'] += 1
            state['first'] = str(stamp) if state['first'] is None else state['first']
            state['last'] = str(stamp)
            with self.db:
                for module, position in enumerate(MODULE_POSITIONS):
                    demand = 2. + module / 2
                    target_angle = module * 2 * math.pi + .25
                    sample = dict(kind='swerve_sample', module_index=module, module_id=context['component_ids'][position],
                                  module_position=position, timestamp_ns=stamp, enabled=True, connected=True,
                                  fresh=True, validity='valid', command_speed_mps=demand, measured_speed_mps=demand + error,
                                  command_angle_rad=target_angle, measured_angle_rad=target_angle + angle_errors[line],
                                  command_timestamp_ns=stamp, measurement_timestamp_ns=stamp,
                                  command_stage='final_io_optimized_cosine_desaturated', sample_profile=SAMPLE_PROFILE,
                                  speed_unit='meters per second', angle_unit='radians',
                                  source_reference=dict(source_hash='a' * 64, cycle_record_index=line), boundary=['a' * 64, 0])
                    tracking.consume_sample(self.db, 'review-numerical', 1, state, sample,
                                            dict(stream_source_ordinal=0, stream_line_ordinal=line), line // 2)
            if line < len(durations):
                stamp += durations[line]
        final = self.final('review-numerical', state['work'], rows=1)
        output = tracking.finish_tracking(state, dict(full_source_verified=True, source_hashes=['a' * 64]), final)
        self.assertEqual(output['outcome'], 'finding', output)
        total = sum(durations)
        drive_rmse = math.sqrt(math.fsum(error * error * duration for error, duration in zip(drive_errors, durations)) / total)
        angle_rmse = math.sqrt(math.fsum(error * error * duration for error, duration in zip(angle_errors, durations)) / total)
        drive_p95 = exact_p95([(abs(error), duration) for error, duration in zip(drive_errors, durations)])
        angle_p95 = exact_p95([(abs(error), duration) for error, duration in zip(angle_errors, durations)])
        for module, position in enumerate(MODULE_POSITIONS):
            observed = {item['name']: item['value'] for item in output['metrics'] if item['module_position'] == position}
            for name, expected in (('drive_rmse_mps', drive_rmse), ('steering_rmse_rad', angle_rmse),
                                   ('drive_p95_absolute_error_mps', drive_p95), ('steering_p95_absolute_error_rad', angle_p95),
                                   ('normalized_drive_rmse', drive_rmse / (2. + module / 2))):
                self.assertAlmostEqual(observed[name], expected, places=12, msg=(position, name, observed))
        self.assertEqual([module['sample_count'] for module in output['coverage']['modules']], [6] * 4)
        for unavailable in ('one_module', 'selected_legacy_baseline'):
            with self.subTest(unavailable=unavailable):
                partial = copy.deepcopy(state)
                if unavailable == 'one_module':
                    partial['modules'][3]['mapped_cycles'] -= 1
                else:
                    partial['context']['analysis_plan']['approved_baseline_id'] = 'historical-tuple-baseline'
                observed = tracking.finish_tracking(partial, dict(full_source_verified=True, source_hashes=['a' * 64]), final)
                self.assertTrue(observed['findings'])
                # Preserve the student report wrapper's whole-run policy while retaining findings.
                self.assertEqual(observed['outcome'], 'insufficient_data', observed)

    def test_known_disabled_terminal_is_allowed_but_unknown_authority_is_not(self):
        start = 1_000_000_000
        for label, terminal, expected in (('known_disabled', False, 'evaluated_no_finding'),
                                           ('unknown', None, 'insufficient_data')):
            with self.subTest(terminal=label):
                sid = 'review-terminal-' + label
                context = dict(run_id=sid, boot_id='invented-boot', robot_id='invented-robot',
                               config_hash='b' * 64, test_id='endpoint-contract', surface='invented-flat',
                               battery_id='invented-battery', mapping_revision='normalized-review-1',
                               stream_source_type='simulation', runtime_mode='SIM', completeness='complete',
                               component_ids={p: 'module-' + str(i) for i, p in enumerate(MODULE_POSITIONS)},
                               start_monotonic_ns=str(start), end_monotonic_ns=str(start + 200_000_000),
                               analysis_plan=dict(ideal_simulation_cycle_policy=True, swerve_configuration=dict(
                                   minimum_eligible_seconds=.01, minimum_coverage=.1,
                                   drive_error_limit_mps=1., threshold_revision='invented-terminal-1')))
                state = tracking.initial_tracking(context)
                for line in range(3):
                    stamp = start + line * 100_000_000
                    state['cycles'] += 1
                    state['first'] = str(stamp) if state['first'] is None else state['first']
                    state['last'] = str(stamp)
                    for module, position in enumerate(MODULE_POSITIONS):
                        enabled = True if line < 2 else terminal
                        sample = dict(kind='swerve_sample', module_index=module, module_id='module-' + str(module),
                                      module_position=position, timestamp_ns=stamp, enabled=enabled, connected=True,
                                      fresh=True, validity='valid' if line < 2 else 'missing_or_invalid',
                                      command_speed_mps=2. if line < 2 else None, measured_speed_mps=2. if line < 2 else None,
                                      command_angle_rad=0. if line < 2 else None, measured_angle_rad=0. if line < 2 else None,
                                      command_timestamp_ns=stamp, measurement_timestamp_ns=stamp,
                                      command_stage='final_io_optimized_cosine_desaturated', sample_profile=SAMPLE_PROFILE,
                                      speed_unit='meters per second', angle_unit='radians',
                                      source_reference=dict(source_hash='a' * 64, cycle_record_index=line), boundary=['a' * 64, 0])
                        with self.db:
                            tracking.consume_sample(self.db, sid, 1, state, sample,
                                                    dict(stream_source_ordinal=0, stream_line_ordinal=line), line)
                final = self.final(sid, state['work'])
                output = tracking.finish_tracking(state, dict(full_source_verified=True, source_hashes=['a' * 64]), final)
                self.assertEqual(output['outcome'], expected, output)
    def test_scalar_mapping_requires_radius_policy_and_never_masks_invalid_array(self):
        stamp = 1_000_000_000
        state = motion.initial()
        fields = [(name, 'structschema', value) for name, value in motion.SCHEMAS.items()]
        fields.append(('/RealOutputs/SwerveStates/SetpointsOptimized', 'struct:SwerveModuleVelocity[]',
                       [dict(velocity=2., angle=dict(value=.25)) for _ in range(4)]))
        for module in range(4):
            prefix = '/Drive/Module' + str(module) + '/'
            fields += [(prefix + 'DriveVelocityRadPerSec', 'double', 20.),
                       (prefix + 'TurnPosition', 'struct:Rotation2d', dict(value=.5))]
            fields += [(prefix + name, 'boolean', True) for name in ('DriveConnected', 'TurnConnected', 'TurnEncoderConnected')]
        with self.db:
            for index, (field, kind, value) in enumerate(fields):
                row = dict(kind='observation', field=field, type=kind, value=value, validity='valid', category='input',
                           source_sha256='a' * 64, entry_id=index + 1, entry_generation=1, record_index=index,
                           record_timestamp_ns=str(stamp), cycle_timestamp_ns=str(stamp), stream_scope='before_run')
                motion.consume(self.db, 'review-scalar', 1, state, row)
            cycle = dict(kind='cycle', source_sha256='a' * 64, timestamp_ns=str(stamp),
                         timestamp_record_index=1000, stream_scope='in_run', aliases={
                             'enabled': dict(value=True, validity='valid'),
                             'runtime_mode': dict(value='SIM', validity='valid'),
                             'configuration_sha256': dict(value='b' * 64, validity='valid'),
                             'source_sha256': dict(value='c' * 64, validity='valid')})
            snapshot = motion.consume(self.db, 'review-scalar', 1, state, cycle)
        context = dict(runtime_mode='SIM', wheel_radius_m=.05,
                       stream_motion_measurement_source='module_io_if_array_absent',
                       analysis_plan=dict(config_hash='b' * 64, build_hash='c' * 64, ideal_simulation_cycle_policy=True))
        samples, reasons = motion.samples(snapshot, context, 'simulation')
        self.assertEqual(reasons, [])
        for sample in samples:
            self.assertEqual(sample['validity'], 'valid')
            self.assertEqual(sample['measured_speed_mps'], 1.)
            self.assertEqual(sample['measured_angle_rad'], .5)
            self.assertTrue(sample['fresh'])
            self.assertEqual(len(sample['source_reference']['module_measurement_references']), 2)
        for absent in ('wheel_radius_m', 'stream_motion_measurement_source'):
            with self.subTest(absent=absent):
                incomplete = dict(context)
                incomplete.pop(absent)
                values, _ = motion.samples(snapshot, incomplete, 'simulation')
                self.assertTrue(all(sample['validity'] != 'valid' for sample in values))
        real, _ = motion.samples(snapshot, context, 'real')
        self.assertTrue(all(sample['fresh'] is False for sample in real))
        # A present, malformed measured array must block the scalar fallback.
        with self.db:
            motion.consume(self.db, 'review-scalar', 1, state,
                           dict(kind='observation', field='/RealOutputs/SwerveStates/Measured',
                                type='struct:SwerveModuleVelocity[]', value=[], validity='valid', category='output',
                                source_sha256='a' * 64, entry_id=100, entry_generation=1, record_index=1001,
                                record_timestamp_ns=str(stamp), cycle_timestamp_ns=str(stamp), stream_scope='in_run'))
            later = dict(cycle, timestamp_ns=str(stamp + 20_000_000), timestamp_record_index=1002)
            snapshot = motion.consume(self.db, 'review-scalar', 1, state, later)
        values, _ = motion.samples(snapshot, context, 'simulation')
        self.assertTrue(all(sample['validity'] != 'valid' for sample in values))
        self.assertIn('swerve_array_type_or_length_invalid', state['errors'])
    def test_rehashed_partial_cursor_and_weight_must_match_replayed_order(self):
        sid = 'review-partial-cursor'
        state = work.initial()
        with self.db:
            for line, (error, duration) in enumerate(((1., 5), (2., 7), (3., 8))):
                work.contribute(self.db, sid, 1, state, line,
                                dict(stream_source_ordinal=0, stream_line_ordinal=line),
                                0, 'drive', error, duration)
            saved = work.finalize_step(self.db, sid, 1, state, 1, 1., lambda: 0.)
        self.assertEqual(saved['cursor'], [1., 0])
        self.assertEqual(saved['cumulative'], '5')
        for field, replacement in (('cumulative', '18'), ('cursor', [1., 99])):
            with self.subTest(field=field):
                forged = dict(saved, **{field: replacement})
                raw = canonical(forged)
                with self.db:
                    self.db.execute('UPDATE analysis_tracking_finalization SET state_json=?,state_sha256=? WHERE stream_id=?',
                                    (raw, hashlib.sha256(raw.encode()).hexdigest(), sid))
                validation = work.validation_initial()
                with self.assertRaises(ValueError):
                    for _ in range(100):
                        if work.validate_step(self.db, sid, 1, state, validation, 1, 1., lambda: 0.):
                            self.fail('Forged partial cursor was admitted')
    def test_rehashed_completed_quantiles_cannot_be_trusted_after_worker_restart(self):
        source = self.root / 'invented.wpilog'
        source.write_bytes(protocol_recording())
        self.assertEqual(Importer(self.root, self.db).import_file(source, source_type='SYNTHETIC')['state'], 'succeeded')
        document = rebuild_from_imports(self.root, self.db)
        limits = dict(min_free_bytes=0, chunk_bytes=4096)
        runner = reports.StreamReportRunner(self.root, self.db, limits=limits)
        self.addCleanup(runner.close)
        sid = reports.schedule_stream_reports(self.root, self.db, document, runner=runner,
                                              pipeline_version='automatic-reports-10')[0]['stream_id']
        for _ in range(10000):
            with patch.object(stream_analysis.time, 'monotonic', return_value=0):
                progress = runner.step(sid, max_rows=2, max_bytes=4096)
            self.assertNotEqual(progress['state'], 'failed', progress)
            if progress['phase'] == 'manifest_publish':
                break
        else:
            self.fail('No final manifest phase')
        row = self.db.execute('SELECT state_json FROM analysis_tracking_finalization WHERE stream_id=?', (sid,)).fetchone()
        self.assertIsNotNone(row)
        forged = json.loads(row[0])
        self.assertTrue(forged['complete'])
        forged['quantiles'] = {str(module) + ':' + metric: 123. for module in range(4) for metric in ('drive', 'steering')}
        raw = canonical(forged)
        with self.db:
            self.db.execute('UPDATE analysis_tracking_finalization SET state_json=?,state_sha256=? WHERE stream_id=?',
                            (raw, hashlib.sha256(raw.encode()).hexdigest(), sid))
        runner.close()
        runner = reports.StreamReportRunner(self.root, self.db, limits=limits)
        self.addCleanup(runner.close)
        for _ in range(10000):
            with patch.object(stream_analysis.time, 'monotonic', return_value=0):
                progress = runner.step(sid, max_rows=2, max_bytes=4096)
            if progress['state'] in ('ready', 'failed'):
                break
        self.assertEqual(progress['state'], 'failed', progress)
        self.assertIsNone(self.db.execute('SELECT report_id FROM analysis_stream_jobs WHERE stream_id=?', (sid,)).fetchone()[0])


if __name__ == '__main__':
    unittest.main()
