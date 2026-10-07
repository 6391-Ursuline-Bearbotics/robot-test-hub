"""Portable extraction regressions qualified by T04's genuine official-reader fixtures."""
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest

from robot_test_hub.wpilog import (PROFILE, FormatError, ResourceLimit, Structs,
                                   UnsupportedProfile, decode, extract, records)

FIXTURES = Path(__file__).parent / "fixtures/synthetic"


def wire_record(entry, payload, timestamp_us=1000000):
    widths = [max(1, (v.bit_length() + 7) // 8) for v in (entry, len(payload), timestamp_us)]
    flag = widths[0] - 1 | ((widths[1] - 1) << 2) | ((widths[2] - 1) << 4)
    return bytes([flag]) + b"".join(v.to_bytes(w, "little") for v, w in zip((entry, len(payload), timestamp_us), widths)) + payload


def text(value):
    data = value.encode()
    return struct.pack("<I", len(data)) + data


def start(entry, field, kind, metadata='{"source":"AdvantageKit"}'):
    return wire_record(0, b"\0" + struct.pack("<I", entry) + text(field) + text(kind) + text(metadata))


def log_bytes(*events):
    return (b"WPILOG\0\1" + struct.pack("<I", len(b"AdvantageKit")) + b"AdvantageKit"
            + start(1, "/Timestamp", "int64", '{"unit":"nanoseconds"}')
            + wire_record(1, struct.pack("<q", 1000000000)) + b"".join(events))


class WpilogTests(unittest.TestCase):
    def test_actual_logger_metadata_and_replay_metadata_stay_distinct(self):
        rows=self.rows(log_bytes(start(2,'/RealMetadata/RobotId','string'),wire_record(2,b'actual-robot'),
            start(3,'/RealMetadata/BootId','string'),wire_record(3,b'actual-boot'),
            start(4,'/ReplayMetadata/RobotId','string'),wire_record(4,b'replay-robot')))
        cycle=next(r for r in rows if r['kind']=='cycle')
        self.assertEqual(cycle['aliases']['real_metadata_robot_id']['value'],'actual-robot')
        self.assertEqual(cycle['aliases']['real_metadata_boot_id']['value'],'actual-boot')
        replay=next(r for r in rows if r['kind']=='observation' and r.get('field')=='/ReplayMetadata/RobotId')
        self.assertEqual(replay['category'],'replay_metadata')

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "input.wpilog"

    def rows(self, data):
        self.path.write_bytes(data)
        return list(extract(self.path))

    def test_genuine_fixtures_literal_cycles_structs_units_and_short_tail(self):
        manifest = json.loads((FIXTURES / "expected.json").read_text())
        for case in manifest["files"]:
            with self.subTest(file=case["file"]):
                rows = list(extract(FIXTURES / case["file"]))
                physical = [r for r in rows if r["kind"] in ("control", "observation")]
                self.assertEqual(len(physical), case["record_count"])
                self.assertEqual([r["record_index"] for r in physical], list(range(case["record_count"])))
                self.assertEqual(physical[-1]["field"], case["short_tail_name"])
                cycles = [r for r in rows if r["kind"] == "cycle"]
                self.assertEqual([r["timestamp_ns"] for r in cycles], list(map(str, case["timestamp_ns"])))
                self.assertEqual([r["aliases"]["fixture_mode"]["value"] for r in cycles], case["modes"])
                self.assertEqual([r["aliases"]["epoch_us"]["value"] for r in cycles], case["epoch_us"])
                self.assertEqual([r["aliases"]["epoch_valid"]["value"] for r in cycles], case["epoch_valid"])
                self.assertEqual(cycles[0]["aliases"]["boot_id"]["value"], case["boot_id"])
                self.assertIsNone(cycles[0]["bootstrap_snapshot"])
                observations = [r for r in rows if r["kind"] == "observation"]
                poses = [r for r in observations if r["field"] == "/Drive/Pose"]
                for i, pose in enumerate(poses):
                    self.assertEqual(pose["value"], {"translation": {"x": 1.25 + i, "y": -2.5}, "rotation": {"value": .25}})
                    self.assertEqual(pose["coordinate_frame"], "2026-rebuilt-blue-origin")
                arrays = [r for r in rows if r["kind"] == "nested_array"]
                self.assertEqual([r["value"] for r in arrays], [[[i + .5, -i - .5], [], [i + 2.]] for i in range(len(cycles))])
                unit_rows = [r for r in observations if r["field"] == "/Fixture/UnitsChange"]
                self.assertEqual([r["unit"] for r in unit_rows], ["meters"] + ["centimeters"] * (len(cycles) - 1))
                self.assertAlmostEqual(unit_rows[-1]["canonical_value"], (100 + len(cycles) - 1) / 100)

    def test_wire_and_payload_precision_are_distinct(self):
        rows = list(extract(FIXTURES / "alpha7-main.wpilog"))
        timestamp = [r for r in rows if r["kind"] == "observation" and r["field"] == "/Timestamp"][1]
        self.assertEqual(timestamp["value"], "1020000123")
        self.assertEqual(timestamp["record_timestamp_ns"], "1020000000")
        self.assertEqual(timestamp["cycle_timestamp_ns"], "1020000123")
        self.assertEqual(timestamp["unit"], "nanoseconds")

    def test_missing_odometry_is_not_a_fresh_held_observation(self):
        rows = list(extract(FIXTURES / "alpha7-main.wpilog"))
        missing = "1060000000"
        self.assertFalse(any(r["kind"] == "sample" and r["cycle_timestamp_ns"] == missing for r in rows))
        self.assertFalse(any(r["kind"] == "observation" and r["field"] == "/Drive/Module0/OdometryTimestamps" and r["cycle_timestamp_ns"] == missing for r in rows))
        disconnected = next(r for r in rows if r["kind"] == "observation" and r["field"] == "/Drive/Module0/Connected" and r["cycle_timestamp_ns"] == missing)
        self.assertFalse(disconnected["value"])
        sample = next(r for r in rows if r["kind"] == "sample" and r["cycle_timestamp_ns"] == "1020000123")
        self.assertEqual(sample["sample_timestamp_ns"], "1015000123")
        self.assertEqual(sample["sample_timestamp_seconds"], 1.015000123)
        arrays = [r for r in rows if r["kind"] == "nested_array"]
        self.assertFalse(arrays[1]["element_sources"][1]["updated_in_cycle"])

    def test_real_and_replay_outputs_remain_distinct(self):
        rows = list(extract(FIXTURES / "alpha7-main.wpilog"))
        real = [r["value"] for r in rows if r["kind"] == "observation" and r["category"] == "real_output"]
        replay = [r["value"] for r in rows if r["kind"] == "observation" and r["category"] == "replay_output"]
        self.assertEqual(real, list(range(1, 8)))
        self.assertEqual(replay, list(range(10, 17)))

    def test_finish_and_entry_id_reuse_follow_record_definitions(self):
        rows = self.rows(log_bytes(start(2, "/A", "double"), wire_record(2, struct.pack("<d", 4)),
                                   wire_record(0, b"\1" + struct.pack("<I", 2)),
                                   start(2, "/B", "string"), wire_record(2, b"hello")))
        values = [r for r in rows if r["kind"] == "observation" and r["entry_id"] == 2]
        self.assertEqual([(r["field"], r["value"], r["entry_generation"]) for r in values], [("/A", 4., 1), ("/B", "hello", 2)])
        for events in ((wire_record(2, b"\0"),),
                       (start(2, "/A", "double"), wire_record(0, b"\1" + struct.pack("<I", 2)), wire_record(2, struct.pack("<d", 0))),
                       (wire_record(0, b"\2" + struct.pack("<I", 2) + text("")),),
                       (wire_record(0, b"\3" + struct.pack("<I", 2)),)):
            with self.subTest(events=events), self.assertRaises(FormatError):
                self.rows(log_bytes(*events))

    def test_unknown_types_schemas_and_nonfinite_preserve_raw_and_unavailable(self):
        rows = self.rows(log_bytes(start(2, "/Unknown", "proto:Thing"), wire_record(2, b"opaque"),
                                   start(3, "/NoSchema", "struct:Absent"), wire_record(3, b"1234"),
                                   start(4, "/Voltage", "double"), wire_record(4, struct.pack("<d", float("nan")))))
        observations = [r for r in rows if r["kind"] == "observation" and r["entry_id"] != 1]
        self.assertEqual([r["validity"] for r in observations], ["unsupported", "unsupported", "nonfinite"])
        self.assertTrue(all(r["value"] is None and r["raw_hex"] for r in observations))
        json.dumps(rows, allow_nan=False)

    def test_generic_nested_struct_and_fixed_arrays_use_recorded_schema(self):
        layouts = Structs({"Point": "double x;double y", "Bundle": "Point points[2];uint8 flags[3];char label[4]"})
        data = struct.pack("<ddddBBB4s", 1, 2, 3, 4, 5, 6, 7, b"a\0\0\0")
        self.assertEqual(decode("struct:Bundle", data, layouts), {"points": [{"x": 1., "y": 2.}, {"x": 3., "y": 4.}], "flags": [5, 6, 7], "label": "a"})
        with self.assertRaises(FormatError):
            decode("struct:Bundle", data[:-1], layouts)
        self.assertEqual(decode("struct:Point[]", b"", layouts), [])

    def test_primitive_arrays_string_arrays_and_payload_lengths(self):
        layouts = Structs({})
        self.assertEqual(decode("int64[]", struct.pack("<qq", -4, 5), layouts), [-4, 5])
        self.assertEqual(decode("boolean[]", b"\0\2", layouts), [False, True])
        self.assertEqual(decode("string[]", struct.pack("<I", 2) + text("") + text("é"), layouts), ["", "é"])
        for kind, payload in (("double", b"\0"), ("double[]", b"\0"), ("string[]", b"\0"),
                              ("string[]", struct.pack("<I", 0) + b"\0"), ("string", b"\xff")):
            with self.subTest(kind=kind), self.assertRaises(FormatError):
                decode(kind, payload, layouts)

    def test_framing_rejects_truncation_reserved_flags_and_overflow(self):
        data = log_bytes()
        for invalid in (data[:-1], data + b"\0", b"BROKEN" + data[6:],
                        data[:24] + bytes([data[24] | 0x80]) + data[25:],
                        log_bytes(wire_record(1, struct.pack("<q", 2), (1 << 63)))):
            with self.subTest(data=invalid[-12:]), self.assertRaises(FormatError):
                list(records(io.BytesIO(invalid)))
        with self.assertRaises(ResourceLimit):
            list(records(io.BytesIO(data), max_record_bytes=4))

    def test_all_nanosecond_integer_fields_are_decimal_strings(self):
        rows = self.rows(log_bytes(start(2, "/New/Timestamp", "int64", '{"unit":"nanoseconds"}'), wire_record(2, struct.pack("<q", 42)),
                                   start(3, "/New/TimestampArray", "int64[]", '{"unit":"nanoseconds"}'), wire_record(3, struct.pack("<qq", 42, 1791003600020000001))))
        scalar = next(r for r in rows if r["kind"] == "observation" and r["field"] == "/New/Timestamp")
        array = next(r for r in rows if r["kind"] == "observation" and r["field"] == "/New/TimestampArray")
        self.assertEqual(scalar["value"], "42")
        self.assertEqual(array["value"], ["42", "1791003600020000001"])
        self.assertEqual(array["value_representation"], "decimal_string_int64_array")
        hub_rows = self.rows(log_bytes(start(2, "/RealOutputs/TestHub/RobotMonotonicNs", "int64"), wire_record(2, struct.pack("<q", 42)),
                                      start(3, "/RealOutputs/TestHub/RobotMonotonicNsUnit", "string"), wire_record(3, b"nanoseconds")))
        hub = next(r for r in hub_rows if r["kind"] == "observation" and r["field"] == "/RealOutputs/TestHub/RobotMonotonicNs")
        self.assertEqual(hub["value"], "42")
        self.assertEqual(hub["unit_source"], "explicit_profile_mapping")
        with self.assertRaises(UnsupportedProfile):
            self.rows(log_bytes(start(2, "/RealOutputs/TestHub/RobotMonotonicNsUnit", "string"), wire_record(2, b"microseconds")))

    def test_explicit_profile_and_payload_units_reject_incompatible_inputs(self):
        with self.assertRaises(UnsupportedProfile):
            list(extract(FIXTURES / "alpha7-main.wpilog", "2026"))
        data = log_bytes(start(1, "/Timestamp", "int64", '{"unit":"microseconds"}'),
                         wire_record(1, struct.pack("<q", 1020000000)))
        with self.assertRaises(UnsupportedProfile):
            self.rows(data)
        with self.assertRaises(UnsupportedProfile):
            self.rows(log_bytes(start(2, "/Metadata/FixtureProfile", "string"), wire_record(2, b"older")))
        with self.assertRaises(FormatError):
            self.rows(log_bytes(wire_record(1, struct.pack("<q", 1000000000))))

    def test_missing_and_mismatched_odometry_arrays_report_quality(self):
        field = "/Drive/Module0/OdometryTimestamps"
        position = "/Drive/Module0/OdometryDrivePositionsRad"
        rows = self.rows(log_bytes(start(2, field, "double[]"), wire_record(2, struct.pack("<dd", 1, 2)),
                                   start(3, position, "double[]"), wire_record(3, struct.pack("<d", 3))))
        self.assertEqual(len([r for r in rows if r["kind"] == "quality"]), 1)
        self.assertFalse(any(r["kind"] == "sample" for r in rows))


if __name__ == "__main__":
    unittest.main()
