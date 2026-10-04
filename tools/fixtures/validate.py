"""Independent literal oracle and strict framing checks for public synthetic fixtures."""
from __future__ import annotations

import json
import math
import struct

CASES = {
    "alpha7-main": {
        "boot_id": "synthetic-boot-a",
        "timestamp_ns": [1000000000, 1020000123, 1040000000, 1060000000, 1080000000, 1500000000, 1520000000],
        "epoch_us": [0, 1791003600020000, 1791003600040000, 1791003602060000, 0, 1791003602500000, 1791003602520000],
        "epoch_valid": [False, True, True, True, False, True, True],
        "modes": ["DISABLED", "AUTONOMOUS", "AUTONOMOUS", "TELEOP", "DISABLED", "TELEOP", "DISABLED"],
        "terminal_event": True,
        "record_count": 143, "short_tail_name": "/Fixture/Mode", "short_tail_record_bytes": 14,
        "cases": ["invalid_epoch", "epoch_becomes_valid", "utc_jump_plus_2s", "auto_to_teleop_without_disable", "two_runs", "420ms_gap", "disconnected_missing_odometry", "sub_microsecond_timestamp_payload"],
    },
    "alpha7-boot-b": {
        "boot_id": "synthetic-boot-b",
        "timestamp_ns": [1000000000, 1020000000, 1040000000],
        "epoch_us": [1791003605000000, 1791003605020000, 1791003605040000],
        "epoch_valid": [True, True, True],
        "modes": ["DISABLED", "TELEOP", "TELEOP"],
        "terminal_event": False,
        "record_count": 81, "short_tail_name": "/Fixture/NestedArray/2", "short_tail_record_bytes": 14,
        "cases": ["reboot_overlapping_monotonic_times", "enabled_last_sample_no_terminal_event"],
    },
    "alpha7-dst-overlap": {
        "boot_id": "synthetic-boot-fall", "timestamp_ns": [1000000000, 3601000000000],
        "epoch_us": [1793514600000000, 1793518200000000], "epoch_valid": [True, True],
        "modes": ["DISABLED", "DISABLED"], "terminal_event": True,
        "record_count": 69, "short_tail_name": "/Fixture/NestedArray/2", "short_tail_record_bytes": 15,
        "cases": ["America/Chicago_2026-11-01_01:30_twice_distinct_utc"],
    },
    "alpha7-dst-gap": {
        "boot_id": "synthetic-boot-spring", "timestamp_ns": [1000000000, 2000000000],
        "epoch_us": [1772956799000000, 1772956800000000], "epoch_valid": [True, True],
        "modes": ["DISABLED", "DISABLED"], "terminal_event": True,
        "record_count": 69, "short_tail_name": "/Fixture/NestedArray/2", "short_tail_record_bytes": 14,
        "cases": ["America/Chicago_2026-03-08_01:59:59_to_03:00:00"],
    },
}


def framed_records(data):
    """Validate every byte, including trailing incomplete records official iteration may ignore.

    This tiny test-only framing oracle is deliberately not a hub importer or profile detector.
    Record-header timestamps here retain their on-disk microsecond unit.
    """
    if len(data) < 12 or data[:6] != b"WPILOG" or int.from_bytes(data[6:8], "little") != 0x100:
        raise AssertionError("Invalid WPILOG 1.0 header")
    header_size = int.from_bytes(data[8:12], "little")
    if data[12:12 + header_size] != b"AdvantageKit":
        raise AssertionError("Expected genuine AdvantageKit extra header")
    position = 12 + header_size
    result = []
    while position < len(data):
        flag = data[position]
        if flag & 0x80:
            raise AssertionError("Reserved record header bit set")
        widths = [(flag & 3) + 1, ((flag >> 2) & 3) + 1, ((flag >> 4) & 7) + 1]
        cursor = position + 1
        numbers = []
        for width in widths:
            if cursor + width > len(data):
                raise AssertionError("Truncated record header")
            numbers.append(int.from_bytes(data[cursor:cursor + width], "little"))
            cursor += width
        entry, size, timestamp_us = numbers
        if cursor + size > len(data):
            raise AssertionError("Truncated record payload")
        result.append((entry, timestamp_us, data[cursor:cursor + size]))
        position = cursor + size
    return result


def _string(data, position):
    assert position + 4 <= len(data), "Truncated control string length"
    length = struct.unpack_from("<I", data, position)[0]
    position += 4
    assert position + length <= len(data), "Truncated control string"
    return data[position:position + length].decode("utf-8"), position + length


def validate_bytes(data, case):
    entries = {}
    rows = []
    framed = framed_records(data)
    assert len(framed) == case["record_count"]
    for entry, timestamp_us, payload in framed:
        if entry == 0:
            assert payload, "Empty control record"
            if payload[0] == 0:
                assert len(payload) >= 5, "Truncated start control"
                identity = struct.unpack_from("<I", payload, 1)[0]
                assert identity != 0 and identity not in entries, "Invalid start identity"
                name, position = _string(payload, 5)
                kind, position = _string(payload, position)
                metadata, position = _string(payload, position)
                assert position == len(payload)
                entries[identity] = (name, kind, metadata)
            elif payload[0] == 2:
                assert len(payload) >= 5, "Truncated metadata control"
                identity = struct.unpack_from("<I", payload, 1)[0]
                assert identity in entries, "Metadata references unknown entry"
                metadata, position = _string(payload, 5)
                assert position == len(payload)
                name, kind, _ = entries[identity]
                entries[identity] = (name, kind, metadata)
            else:
                raise AssertionError("Unexpected fixture control record")
            continue
        assert entry in entries, "Data references unknown entry"
        name, kind, metadata = entries[entry]
        if kind == "int64": value = struct.unpack("<q", payload)[0]
        elif kind == "double": value = struct.unpack("<d", payload)[0]
        elif kind == "boolean":
            assert len(payload) == 1 and payload[0] in (0, 1)
            value = bool(payload[0])
        elif kind == "string": value = payload.decode("utf-8")
        elif kind == "double[]": value = list(struct.unpack("<" + "d" * (len(payload) // 8), payload))
        elif kind == "struct:Pose2d":
            x, y, angle = struct.unpack("<ddd", payload)
            value = {"x_m": x, "y_m": y, "heading_rad": angle}
        elif kind == "struct:Pose2d[]":
            assert len(payload) == 48
            value = [{"x_m": x, "y_m": y, "heading_rad": a} for x, y, a in struct.iter_unpack("<ddd", payload)]
        elif kind == "structschema": value = payload.decode("utf-8")
        else: raise AssertionError(f"Unexpected fixture type: {kind}")
        rows.append({"name": name, "type": kind, "timestamp_ns": timestamp_us * 1000,
                     "value": value, "metadata": metadata})
    assert any(name == "/Timestamp" and json.loads(metadata)["unit"] == "nanoseconds" for name, _, metadata in entries.values())
    assert any(name == "/SystemStats/EpochTime" and json.loads(metadata)["unit"] == "microseconds" for name, _, metadata in entries.values())
    validate_rows(rows, case)
    unit_rows = [row for row in rows if row["name"] == "/Fixture/UnitsChange"]
    assert [json.loads(row["metadata"])["unit"] for row in unit_rows] == ["meters"] + ["centimeters"] * (len(case["timestamp_ns"]) - 1)
    assert rows[-1]["name"] == case["short_tail_name"]
    return rows


def validate_rows(rows, case):
    samples = [row for row in rows if row["name"] == "/Timestamp"]
    assert [row["value"] for row in samples] == case["timestamp_ns"]
    assert [row["timestamp_ns"] for row in samples] == [value // 1000 * 1000 for value in case["timestamp_ns"]]
    values = {}
    previous = -1
    snapshots = []
    for row in rows:
        if row["name"] == "/Timestamp":
            if previous >= 0: snapshots.append(dict(values))
            previous += 1
        else:
            values[row["name"]] = row["value"]
    snapshots.append(dict(values))
    assert len(snapshots) == len(case["timestamp_ns"])
    for i, snapshot in enumerate(snapshots):
        assert snapshot["/Metadata/BootId"] == case["boot_id"]
        assert snapshot["/Metadata/SourceType"] == "SYNTHETIC"
        assert snapshot["/Metadata/RobotId"] == "synthetic-team-6391"
        assert snapshot["/Metadata/BuildId"] == "fixture-source-v1"
        assert snapshot["/Metadata/FixtureProfile"] == "wpilib-2027.0.0-alpha-7_akit-27.0.0-alpha-6"
        assert snapshot["/Fixture/Sequence"] == i
        assert snapshot["/Fixture/Mode"] == case["modes"][i]
        assert snapshot["/DriverStation/Enabled"] == (case["modes"][i] != "DISABLED")
        assert snapshot["/DriverStation/Autonomous"] == (case["modes"][i] == "AUTONOMOUS")
        assert snapshot["/SystemStats/EpochTime"] == case["epoch_us"][i]
        assert snapshot["/SystemStats/EpochTimeValid"] == case["epoch_valid"][i]
        assert snapshot["/RealOutputs/Drive/RequestedSpeed"] == 1 + i
        assert snapshot["/ReplayOutputs/Drive/RequestedSpeed"] == 10 + i
        assert snapshot["/Fixture/NestedArray/length"] == 3
        assert snapshot["/Fixture/NestedArray/0"] == [i + .5, -i - .5]
        assert snapshot["/Fixture/NestedArray/1"] == []
        assert snapshot["/Fixture/NestedArray/2"] == [i + 2.0]
        assert snapshot["/Drive/Pose"] == {"x_m": 1.25 + i, "y_m": -2.5, "heading_rad": .25}
        assert snapshot["/Drive/PoseArray"][0] == snapshot["/Drive/Pose"]
        assert snapshot["/Drive/PoseArray"][1] == {"x_m": -1., "y_m": 2., "heading_rad": -.5}
        assert snapshot["/Drive/Module0/Connected"] == (i != 3)
        source_i = i - 1 if i == 3 else i
        odometry = snapshot["/Drive/Module0/OdometryTimestamps"]
        assert math.isclose(odometry[0], case["timestamp_ns"][source_i] / 1e9 - .005, abs_tol=1e-10)
        assert math.isclose(odometry[1], case["timestamp_ns"][source_i] / 1e9, abs_tol=1e-10)
        assert snapshot["/Drive/Module0/OdometryDrivePositionsRad"] == [source_i + .25, source_i + .5]
        assert snapshot["/Fixture/UnitsChange"] == (1.0 if i == 0 else 100.0 + i)
    assert ("/Fixture/TerminalEvent" in snapshots[-1]) == case["terminal_event"]
    if len(samples) > 3:
        missing_timestamp = case["timestamp_ns"][3] // 1000 * 1000
        assert not any(row["name"] == "/Drive/Module0/OdometryTimestamps" and row["timestamp_ns"] == missing_timestamp for row in rows)
    schemas = {r["name"]: r["value"] for r in rows if r["type"] == "structschema"}
    assert schemas["/.schema/struct:Pose2d"] == "Translation2d translation;Rotation2d rotation"
    assert schemas["/.schema/struct:Translation2d"] == "double x;double y"
    assert schemas["/.schema/struct:Rotation2d"] == "double value"


def validate_official(report, case):
    assert report["version"] == 256 and report["extra_header"] == "AdvantageKit"
    assert report["record_header_unit_on_disk"] == "microseconds"
    assert report["reader_timestamp_unit"] == "nanoseconds"
    validate_rows([r for r in report["records"] if "value" in r], case)
    cycles = report["advantagekit_replay_cycles"]
    assert [c["timestamp_ns"] for c in cycles] == case["timestamp_ns"]
    assert [c["sequence"] for c in cycles] == list(range(len(case["timestamp_ns"])))
    assert [c["real_speed"] for c in cycles] == list(range(1, len(cycles) + 1))
    assert not any(c["has_replay_output"] for c in cycles)
    assert report["foreach_record_count"] == case["record_count"]
    assert report["iterator_record_count"] == case["record_count"] - 1
    # Preserve the pinned AK reader's genuine last-mode omission rather than hiding it by padding.
    expected_replay_modes = list(case["modes"])
    if case["boot_id"] == "synthetic-boot-a": expected_replay_modes[-1] = "TELEOP"
    assert [c["mode"] for c in cycles] == expected_replay_modes
    starts = {r["name"]: r for r in report["records"] if r.get("control") == "start"}
    assert json.loads(starts["/Timestamp"]["metadata"])["unit"] == "nanoseconds"
    assert json.loads(starts["/SystemStats/EpochTime"]["metadata"])["unit"] == "microseconds"
    changes = [r for r in report["records"] if r.get("control") == "metadata" and r["name"] == "/Fixture/UnitsChange"]
    assert any(json.loads(r["metadata"])["unit"] == "centimeters" for r in changes)
