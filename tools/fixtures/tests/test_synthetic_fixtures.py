"""Portable fixture regression checks; no installed WPILib or hub service required."""
import hashlib
import json
from pathlib import Path
import sys
import struct
import unittest

TOOL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL))
from validate import CASES, framed_records, validate_bytes

FIXTURES = TOOL.parents[1] / "tests/fixtures/synthetic"


class SyntheticFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))

    def test_public_synthetic_provenance_and_version_profile(self):
        self.assertEqual(self.manifest["source_type"], "SYNTHETIC")
        self.assertEqual(self.manifest["profile"], "wpilib-2027.0.0-alpha-7_akit-27.0.0-alpha-6")
        self.assertEqual(self.manifest["older_profile"]["status"], "unsupported")
        self.assertEqual({item["file"] for item in self.manifest["files"]}, {name + ".wpilog" for name in CASES})

    def test_exact_hashes_lengths_and_literal_oracle(self):
        for item in self.manifest["files"]:
            with self.subTest(file=item["file"]):
                data = (FIXTURES / item["file"]).read_bytes()
                self.assertEqual(len(data), item["size_bytes"])
                self.assertEqual(hashlib.sha256(data).hexdigest(), item["sha256"])
                case = CASES[Path(item["file"]).stem]
                self.assertEqual({key: item[key] for key in case}, case)
                validate_bytes(data, case)

    def test_complete_short_trailing_records_are_preserved(self):
        for name, case in CASES.items():
            with self.subTest(file=name):
                data = (FIXTURES / (name + ".wpilog")).read_bytes()
                tail_size = case["short_tail_record_bytes"]
                self.assertLess(tail_size, 16)
                self.assertEqual(len(framed_records(data)), case["record_count"])
                self.assertEqual(len(framed_records(data[:-tail_size])), case["record_count"] - 1)
                self.assertEqual(validate_bytes(data, case)[-1]["name"], case["short_tail_name"])

    def test_truncated_tail_cannot_pass_complete_framing(self):
        for name in CASES:
            data = (FIXTURES / (name + ".wpilog")).read_bytes()
            for removed in range(1, CASES[name]["short_tail_record_bytes"]):
                with self.subTest(file=name, removed=removed), self.assertRaisesRegex(AssertionError, "Truncated record"):
                    framed_records(data[:-removed])

    def test_invalid_header_and_appended_garbage_fail(self):
        data = (FIXTURES / "alpha7-main.wpilog").read_bytes()
        for changed in (b"BROKEN" + data[6:], data + b"\x00", data[:10],
                        data[:6] + b"\x01\x01" + data[8:],
                        data[:8] + struct.pack("<I", len(data)) + data[12:],
                        data[:23] + bytes([data[23] | 0x80]) + data[24:]):
            with self.assertRaises(AssertionError):
                framed_records(changed)

    def test_well_framed_semantic_corruption_fails_independent_oracle(self):
        data = (FIXTURES / "alpha7-main.wpilog").read_bytes()
        mutations = (
            (b"synthetic-boot-a", b"synthetic-boot-z"),
            (b"AUTONOMOUS", b"TELEOPxxxx"),
            (struct.pack("<q", 1020000123), struct.pack("<q", 1020000000)),
            (struct.pack("<d", .25), struct.pack("<d", .30)),
            (b'"centimeters"', b'"millimeters"'),
        )
        for before, after in mutations:
            with self.subTest(value=before):
                self.assertEqual(len(before), len(after))
                self.assertIn(before, data)
                changed = data.replace(before, after)
                self.assertEqual(len(framed_records(changed)), CASES["alpha7-main"]["record_count"])
                with self.assertRaises(AssertionError):
                    validate_bytes(changed, CASES["alpha7-main"])

    def test_unknown_data_identity_fails_fixture_decode(self):
        data = bytearray((FIXTURES / "alpha7-main.wpilog").read_bytes())
        position = 12 + int.from_bytes(data[8:12], "little")
        while position < len(data):
            flag = data[position]
            entry_width = (flag & 3) + 1
            size_width = ((flag >> 2) & 3) + 1
            time_width = ((flag >> 4) & 7) + 1
            entry = int.from_bytes(data[position + 1:position + 1 + entry_width], "little")
            if entry != 0:
                data[position + 1:position + 1 + entry_width] = b"\xff" * entry_width
                break
            size_start = position + 1 + entry_width
            size = int.from_bytes(data[size_start:size_start + size_width], "little")
            position = size_start + size_width + time_width + size
        self.assertEqual(len(framed_records(data)), CASES["alpha7-main"]["record_count"])
        with self.assertRaisesRegex(AssertionError, "Data references unknown entry"):
            validate_bytes(data, CASES["alpha7-main"])

    def test_timestamp_domains_and_sub_microsecond_precision(self):
        self.assertEqual(self.manifest["record_header_unit_on_disk"], "microseconds")
        self.assertEqual(self.manifest["reader_timestamp_unit"], "nanoseconds")
        self.assertEqual(self.manifest["timestamp_payload_unit"], "nanoseconds")
        self.assertEqual(self.manifest["epoch_unit"], "microseconds")
        self.assertEqual(self.manifest["odometry_timestamp_unit"], "seconds")
        rows = validate_bytes((FIXTURES / "alpha7-main.wpilog").read_bytes(), CASES["alpha7-main"])
        timestamp = [r for r in rows if r["name"] == "/Timestamp"][1]
        self.assertEqual(timestamp["value"], 1020000123)
        self.assertEqual(timestamp["timestamp_ns"], 1020000000)

    def test_boot_overlap_and_epoch_clock_jump(self):
        a, b = CASES["alpha7-main"], CASES["alpha7-boot-b"]
        self.assertEqual(a["timestamp_ns"][0], b["timestamp_ns"][0])
        self.assertNotEqual(a["boot_id"], b["boot_id"])
        self.assertFalse(a["epoch_valid"][0])
        self.assertEqual(a["epoch_us"][3] - a["epoch_us"][2], 2020000)
        self.assertEqual(a["timestamp_ns"][3] - a["timestamp_ns"][2], 20000000)
        self.assertEqual(a["timestamp_ns"][5] - a["timestamp_ns"][4], 420000000)
        self.assertFalse(b["terminal_event"])


if __name__ == "__main__":
    unittest.main()
