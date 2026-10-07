"""Independent integer PTS fixtures; bytes are protocol evidence, not encoded footage."""
from dataclasses import replace
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from robot_test_hub.video_alignment import (AlignmentError, AlignmentRevisions, CalibrationWindow,
                                          ManualAlignment, ManualAnchor, VideoSegment)


class VideoAlignmentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.base = 9_007_199_254_740_993

    def segment(self, identity='segment1', frames=((0, 10), (10, 10), (40, 10), (50, 10)), **changes):
        path = self.root / (identity + '.mkv')
        path.write_bytes(b'SYNTHETIC PROTOCOL EVIDENCE; NOT VIDEO ' + identity.encode())
        manifest = dict(schema_version=1, state='closed_verified', segment_id=identity,
            camera_id='overview', session_id='practice', capture_id='capture1', source_type='SYNTHETIC',
            relative_path=path.name, size_bytes=path.stat().st_size, sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            start_pts_ns=str(frames[0][0]), end_pts_ns=str(sum(frames[-1])),
            frames=[dict(pts_ns=str(pts), duration_ns=str(duration) if duration is not None else None)
                    for pts, duration in frames], utc_basis='process_launch_estimate', started_utc_ns='1790000000000000000')
        manifest.update(changes)
        return VideoSegment.from_manifest(self.root, manifest), manifest

    def anchor(self, segment, robot, index, **changes):
        return ManualAnchor(robot, segment.segment_id, segment.sha256, index,
            changes.get('robot_uncertainty_ns', 0), changes.get('video_uncertainty_ns', 0),
            changes.get('evidence_label', 'operator-selected visible cue'))

    def mapping(self, segments, windows, **changes):
        identity = dict(robot_id='robot6391', boot_id='boot1', camera_id='overview',
                        session_id='practice', capture_id='capture1')
        identity.update(changes)
        return ManualAlignment(**identity, segments=segments, windows=windows)

    def ordinary(self):
        segment, manifest = self.segment()
        anchors = (self.anchor(segment, self.base, 0), self.anchor(segment, self.base + 50, 3))
        window = CalibrationWindow('continuous', self.base, self.base + 50, anchors, (segment.segment_id,))
        return self.mapping((segment,), (window,)), segment, window

    def query(self, mapping, a, b=None, **kwargs):
        return mapping.map_interval('robot6391', 'boot1', self.base + a, self.base + (a if b is None else b), **kwargs)

    def test_actual_variable_frame_spans_and_gap_are_not_nominal_fps(self):
        mapping, segment, window = self.ordinary()
        result = self.query(mapping, 5, 45, event_id='late-note', run_id='run1')
        self.assertEqual(result['state'], 'partial')
        self.assertEqual(result['spans'][0]['frame_indexes'], [0, 1, 2])
        self.assertEqual(result['windows'][0]['unavailable_pts_intervals'], [['20', '40']])
        self.assertEqual(result['spans'][0]['source_sha256'], segment.sha256)
        self.assertEqual(result['event_id'], 'late-note')
        self.assertEqual(self.query(mapping, 25)['state'], 'gap')
        self.assertEqual(self.query(mapping, 45)['state'], 'mapped')
        self.assertEqual(self.query(mapping, 45)['spans'][0]['frame_indexes'], [2])

    def test_exact_nanosecond_precision_and_outward_fractional_rounding(self):
        pts_origin = self.base + 777
        segment, _ = self.segment(frames=((pts_origin, 1002), (pts_origin + 1002, 10)))
        anchors = (self.anchor(segment, self.base, 0), self.anchor(segment, self.base + 1001, 1))
        window = CalibrationWindow('precision', self.base, self.base + 1001, anchors, ('segment1',))
        mapping = self.mapping((segment,), (window,))
        result = self.query(mapping, 500)
        expected = Fraction(pts_origin) + Fraction(1002, 1001) * 500
        self.assertEqual(int(result['windows'][0]['start_pts_ns']), expected.numerator // expected.denominator)
        self.assertEqual(int(result['windows'][0]['end_pts_ns']), -((-expected.numerator) // expected.denominator))
        self.assertEqual(mapping.document()['windows'][0]['anchors'][0]['robot_ns'], str(self.base))

    def test_known_drift_and_centered_residuals(self):
        segment, _ = self.segment(frames=((0, 1010), (1010, 1010), (2020, 10)))
        anchors = tuple(self.anchor(segment, self.base + index * 1000, index) for index in range(3))
        window = CalibrationWindow('drift', self.base, self.base + 2000, anchors, ('segment1',))
        mapping = self.mapping((segment,), (window,))
        report = mapping.document()['windows'][0]
        self.assertEqual(report['drift_ppm'], '10000')
        self.assertEqual(report['residuals_ns'], ['0', '0', '0'])
        self.assertEqual(self.query(mapping, 1000)['windows'][0]['start_pts_ns'], '1010')
        other, _ = self.segment('residual', frames=((0, 12), (12, 8), (20, 10)))
        anchors = tuple(self.anchor(other, self.base + index * 10, index) for index in range(3))
        residual = self.mapping((other,), (CalibrationWindow('residual', self.base, self.base + 20,
                               anchors, ('residual',)),))
        self.assertEqual(residual.document()['windows'][0]['max_abs_residual_ns'], '2')
        self.assertEqual(self.query(residual, 10)['windows'][0]['uncertainty_ns'], '2')
        self.assertFalse(residual.document()['measured_camera_alignment'])

    def test_uncertainty_selects_adjacent_frames_and_never_certifies_camera_accuracy(self):
        mapping, segment, window = self.ordinary()
        uncertain = replace(window, anchors=tuple(replace(anchor, video_uncertainty_ns=1) for anchor in window.anchors),
                            model_uncertainty_ns=2)
        mapping = self.mapping((segment,), (uncertain,))
        result = self.query(mapping, 10)
        self.assertEqual(result['windows'][0]['uncertainty_ns'], '3')
        self.assertEqual(result['spans'][0]['frame_indexes'], [0, 1])
        self.assertEqual(result['qualification'], 'manual_unqualified')
        self.assertFalse(result['measured_camera_alignment'])
        self.assertFalse(mapping.document()['utc_launch_used'])

    def test_single_anchor_requires_explicit_scale_drift_and_bounded_validity(self):
        segment, _ = self.segment(frames=((1000, 1000), (2000, 1000)))
        anchor = self.anchor(segment, self.base, 0)
        window = CalibrationWindow('assumed', self.base, self.base + 1000, (anchor,), ('segment1',))
        with self.assertRaises(AlignmentError):
            self.mapping((segment,), (window,))
        window = replace(window, assumed_scale='1', assumed_drift_uncertainty_ppm='100')
        mapping = self.mapping((segment,), (window,))
        self.assertEqual(mapping.document()['windows'][0]['assumption'], 'explicit_single_anchor_scale')
        self.assertEqual(self.query(mapping, 1000)['windows'][0]['uncertainty_ns'], '1')
        self.assertEqual(self.query(mapping, 1001)['state'], 'outside')

    def test_calibration_windows_do_not_extrapolate(self):
        mapping, segment, window = self.ordinary()
        with self.assertRaises(AlignmentError):
            self.mapping((segment,), (replace(window, start_robot_ns=self.base - 1),))
        self.assertEqual(self.query(mapping, -1)['state'], 'outside')
        result = self.query(mapping, -1, 5)
        self.assertEqual(result['state'], 'partial')
        self.assertEqual(result['unmapped_robot_intervals'], [[str(self.base - 1), str(self.base)]])

    def test_discontinuity_windows_keep_reset_pts_and_robot_gaps_separate(self):
        first, _ = self.segment('before', frames=((0, 10), (10, 1)))
        second, _ = self.segment('after', frames=((0, 10), (10, 1)))
        a = CalibrationWindow('before-reset', self.base, self.base + 10,
            (self.anchor(first, self.base, 0), self.anchor(first, self.base + 10, 1)), ('before',))
        b = CalibrationWindow('after-reset', self.base + 20, self.base + 30,
            (self.anchor(second, self.base + 20, 0), self.anchor(second, self.base + 30, 1)), ('after',))
        mapping = self.mapping((first, second), (a, b))
        self.assertEqual(self.query(mapping, 25)['spans'][0]['segment_id'], 'after')
        result = self.query(mapping, 5, 25)
        self.assertEqual(result['state'], 'partial')
        self.assertEqual(result['unmapped_robot_intervals'], [[str(self.base + 10), str(self.base + 20)]])
        with self.assertRaises(AlignmentError):
            self.mapping((first, second), (replace(a, segment_ids=('before', 'after')),))

    def test_camera_restart_and_boot_domains_cannot_share_a_mapping(self):
        mapping, segment, window = self.ordinary()
        self.assertEqual(mapping.map_interval('robot6391', 'otherboot', self.base, self.base)['state'], 'unavailable')
        self.assertEqual(mapping.map_interval('otherrobot', 'boot1', self.base, self.base)['reason'], 'clock_domain_mismatch')
        restarted, _ = self.segment('restarted', capture_id='capture2')
        other_camera, _ = self.segment('rear', camera_id='rear')
        for other in (restarted, other_camera):
            with self.assertRaises(AlignmentError):
                self.mapping((segment, other), (window,))

    def test_revision_history_is_immutable_idempotent_and_parent_bound(self):
        mapping, segment, window = self.ordinary()
        revisions = AlignmentRevisions(self.root / 'alignments', 'alignment1')
        first = revisions.append(1, mapping)
        original = Path(first['path']).read_bytes()
        self.assertEqual(revisions.append(1, mapping), first)
        changed = replace(window, anchors=tuple(replace(anchor, evidence_label='reviewed cue') for anchor in window.anchors))
        updated = self.mapping((segment,), (changed,))
        second = revisions.append(2, updated, expected_previous_sha256=first['sha256'])
        self.assertNotEqual(first['sha256'], second['sha256'])
        self.assertEqual(Path(first['path']).read_bytes(), original)
        with self.assertRaises(AlignmentError):
            revisions.append(1, updated)
        with self.assertRaises(AlignmentError):
            revisions.append(3, updated, expected_previous_sha256='0' * 64)
        with self.assertRaises(AlignmentError):
            revisions.append(3, self.mapping((segment,), (changed,), boot_id='otherboot'),
                             expected_previous_sha256=second['sha256'])
        self.assertNotEqual(self.query(mapping, 5)['mapping_sha256'], self.query(updated, 5)['mapping_sha256'])
        loaded = revisions.load(2, (segment,), expected_sha256=second['sha256'])
        self.assertEqual(loaded.document(), updated.document())
        self.assertEqual(self.query(loaded, 5), self.query(updated, 5))
        with self.assertRaises(AlignmentError):
            revisions.load(2, (segment,), expected_sha256='0' * 64)
        with self.assertRaises(AlignmentError):
            revisions.load(2, (replace(segment, manifest_sha256='0' * 64),), expected_sha256=second['sha256'])

    def test_missing_recording_and_modified_bytes_remain_explicit(self):
        mapping, segment, window = self.ordinary()
        path = self.root / segment.relative_path
        original = path.read_bytes()
        path.unlink()
        self.assertEqual(self.query(mapping, 5)['state'], 'gap')
        path.write_bytes(b'x' * len(original))
        with self.assertRaises(AlignmentError):
            self.query(mapping, 5)

    def test_source_hash_anchor_hash_and_path_escape_are_rejected(self):
        mapping, segment, window = self.ordinary()
        with self.assertRaises(AlignmentError):
            self.mapping((segment,), (replace(window, anchors=(replace(window.anchors[0], source_sha256='0' * 64),
                                                              window.anchors[1])),))
        for relative in ('../outside.mkv', '/outside.mkv', 'C:/outside.mkv', 'sub/file.mkv', '..', 'file:stream'):
            with self.subTest(relative=relative), self.assertRaises(AlignmentError):
                self.segment('unsafe', relative_path=relative)
        with self.assertRaises(AlignmentError):
            self.segment('changed', sha256='0' * 64)
        with self.assertRaises(AlignmentError):
            AlignmentRevisions(self.root, '../alignment')

    def test_unknown_frame_duration_never_fills_coverage_and_manifest_is_snapshotted(self):
        segment, manifest = self.segment(frames=((0, None), (10, 10)))
        anchors = (self.anchor(segment, self.base, 0), self.anchor(segment, self.base + 10, 1))
        mapping = self.mapping((segment,), (CalibrationWindow('unknown-duration', self.base, self.base + 10,
                               anchors, ('segment1',)),))
        before = mapping.document()
        manifest['frames'][0]['duration_ns'] = '10'
        self.assertEqual(mapping.document(), before)
        self.assertEqual(self.query(mapping, 5)['state'], 'gap')
        document = mapping.document()
        document['windows'].clear()
        self.assertEqual(mapping.document(), before)

    def test_uncertain_robot_anchors_require_bounded_scale_and_float_time_is_rejected(self):
        mapping, segment, window = self.ordinary()
        anchors = tuple(replace(anchor, robot_uncertainty_ns=30) for anchor in window.anchors)
        with self.assertRaises(AlignmentError):
            self.mapping((segment,), (replace(window, anchors=anchors),))
        with self.assertRaises(AlignmentError):
            replace(window.anchors[0], robot_ns=float(self.base))
        with self.assertRaises(AlignmentError):
            self.query(mapping, 10, 5)

    def test_symlink_evidence_is_rejected_when_platform_supports_creation(self):
        segment, manifest = self.segment()
        link = self.root / 'linked.mkv'
        try:
            link.symlink_to(self.root / segment.relative_path)
        except OSError:
            self.skipTest('Host requires unavailable symlink privilege')
        with self.assertRaises(AlignmentError):
            VideoSegment.from_manifest(self.root, dict(manifest, relative_path=link.name))

    def test_interrupted_revision_temporary_evidence_is_not_deleted(self):
        mapping, segment, window = self.ordinary()
        revisions = AlignmentRevisions(self.root / 'alignments', 'alignment1')
        revisions.folder.mkdir()
        temporary = revisions.folder / 'alignment1-000001.writing'
        temporary.write_bytes(b'preserve interrupted revision evidence')
        with self.assertRaises(FileExistsError):
            revisions.append(1, mapping)
        self.assertEqual(temporary.read_bytes(), b'preserve interrupted revision evidence')
        self.assertFalse((revisions.folder / 'alignment1-000001.json').exists())

    def test_extreme_decimal_exponents_are_rejected_before_fraction_allocation(self):
        segment, _ = self.segment(frames=((0, 10), (10, 10)))
        anchor = self.anchor(segment, self.base, 0)
        window = CalibrationWindow('assumed', self.base, self.base + 10, (anchor,), ('segment1',),
                                   assumed_scale='1', assumed_drift_uncertainty_ppm='100')
        for value in ('1e999999999', '1e-999999999'):
            for field in ('assumed_scale', 'assumed_drift_uncertainty_ppm'):
                with self.subTest(value=value,field=field), self.assertRaises(AlignmentError):
                    self.mapping((segment,), (replace(window, **{field:value}),))

    def test_one_recording_path_cannot_supply_multiple_segment_identities(self):
        mapping, segment, window = self.ordinary()
        aliased = replace(segment, segment_id='alias')
        with self.assertRaisesRegex(AlignmentError, 'paths'):
            self.mapping((segment, aliased), (window,))

    def test_loading_pinned_revision_still_rejects_parent_clock_domain_conflict(self):
        mapping, segment, window = self.ordinary()
        revisions = AlignmentRevisions(self.root / 'alignments', 'alignment1')
        first = revisions.append(1, mapping)
        other_boot = self.mapping((segment,), (window,), boot_id='otherboot')
        payload = dict(other_boot.document(), alignment_id='alignment1', revision=2,
                       previous_sha256=first['sha256'])
        encoded = (json.dumps(payload,sort_keys=True,separators=(',',':'))+'\n').encode()
        path = revisions.folder / 'alignment1-000002.json'
        path.write_bytes(encoded)
        with self.assertRaisesRegex(AlignmentError, 'Clock-domain'):
            revisions.load(2,(segment,),expected_sha256=hashlib.sha256(encoded).hexdigest())

    def test_new_revision_cannot_reidentify_prior_original_path(self):
        mapping, segment, window = self.ordinary()
        revisions = AlignmentRevisions(self.root / 'alignments', 'alignment1')
        first = revisions.append(1, mapping)
        aliased = replace(segment, segment_id='alias')
        changed = replace(window, segment_ids=('alias',),
                          anchors=tuple(replace(anchor,segment_id='alias') for anchor in window.anchors))
        update = self.mapping((aliased,), (changed,))
        with self.assertRaisesRegex(AlignmentError, 'source evidence'):
            revisions.append(2,update,expected_previous_sha256=first['sha256'])

    def test_exact_point_expansion_cannot_overflow_signed_nanoseconds(self):
        maximum = (1 << 63) - 1
        segment, _ = self.segment(frames=((maximum-100,10),))
        window = CalibrationWindow('assumed', self.base, self.base+100,
                                   (self.anchor(segment,self.base,0),), ('segment1',),
                                   assumed_scale='1', assumed_drift_uncertainty_ppm='0')
        mapping = self.mapping((segment,), (window,))
        with self.assertRaisesRegex(AlignmentError, 'signed nanosecond'):
            self.query(mapping,100)


if __name__ == '__main__':
    unittest.main()
