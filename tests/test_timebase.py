from datetime import datetime, timezone
import unittest

from robot_test_hub.timebase import Anchor, NS, build_mapping, datetime_ns, local_candidates, utc_iso


class ClockTests(unittest.TestCase):
    def test_epoch_precision_and_interpolation(self):
        epoch = 1791003600000000123
        mapping = build_mapping('a', 'r1', [Anchor(NS, epoch, True, 2), Anchor(2*NS, epoch+NS, True, 3)])
        point = mapping.map('a', NS+1)
        self.assertEqual(point['utc_ns'], str(epoch+1))
        self.assertEqual(point['uncertainty_ns'], '4')
        self.assertEqual(mapping.map('a', NS)['uncertainty_ns'], '2')
        self.assertEqual(mapping.map('a', 0)['quality'], 'unavailable')
        with self.assertRaisesRegex(ValueError, 'across boots'):
            mapping.map('b', NS)

    def test_jump_invalid_and_gap_do_not_interpolate(self):
        e = 1791003600000000000
        anchors = [Anchor(NS, e, True), Anchor(NS+20_000_000, e+20_000_000, True),
                   Anchor(NS+40_000_000, e+2_040_000_000, True),
                   Anchor(NS+60_000_000, None, False),
                   Anchor(NS+80_000_000, e+2_080_000_000, True), Anchor(4*NS, e+5*NS, True)]
        mapping = build_mapping('a', 'r1', anchors)
        self.assertEqual([p.reason for p in mapping.pieces],
                         ['first_anchor','utc_discontinuity','after_invalid_epoch','anchor_gap'])
        for t in (NS+30_000_000, NS+50_000_000, NS+60_000_000, 3*NS):
            self.assertIsNone(mapping.map('a', t)['utc_ns'])

    def test_order_and_bounds_validation(self):
        for anchors in ([Anchor(1, 10, True), Anchor(1, 20, True)],
                        [Anchor(2, 20, True), Anchor(1, 10, True)]):
            with self.assertRaises(ValueError):
                build_mapping('a','r',anchors)
        with self.assertRaises(ValueError):
            Anchor(1, None, True)
        self.assertEqual(build_mapping('a','r',[]).map('a',10)['quality'], 'unavailable')

    def test_dst_overlap_returns_both_and_gap_rejected(self):
        candidates = local_candidates('2026-11-01T01:30:00', 'America/Chicago')
        self.assertEqual([x['utc_ns'] for x in candidates], ['1793514600000000000', '1793518200000000000'])
        self.assertIn('-05:00', candidates[0]['local'])
        self.assertIn('-06:00', candidates[1]['local'])
        with self.assertRaisesRegex(ValueError, 'does not exist'):
            local_candidates('2026-03-08T02:30:00', 'America/Chicago')
        self.assertEqual(len(local_candidates('2026-10-03T18:52:00','America/Chicago')),1)

    def test_nanosecond_rendering_and_datetime_conversion(self):
        self.assertEqual(utc_iso(1793514600000000123), '2026-11-01T06:30:00.000000123Z')
        self.assertEqual(datetime_ns(datetime(2026,11,1,6,30,tzinfo=timezone.utc)),1793514600000000000)
        with self.assertRaises(ValueError):
            datetime_ns(datetime(2026,11,1,6,30))
