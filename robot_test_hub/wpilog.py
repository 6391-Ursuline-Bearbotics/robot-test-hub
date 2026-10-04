"""Strict, portable extractor for the explicitly qualified Alpha7/AK Alpha6 profile.

No robot code, official iterator, timestamps inferred from filenames, or replay-held
observations. Wire units and table payload units are distinct throughout.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_EVEN
import json
import math
from pathlib import Path
import re
import struct

PROFILE = "wpilib-2027.0.0-alpha-7_akit-27.0.0-alpha-6"
EXTRACTOR_VERSION = "wpilog-python-1"
MAPPING_REVISION = "alpha7-aliases-1"
MAX_RECORD_BYTES = 64 * 1024 * 1024
MAX_NS = (1 << 63) - 1
ALIASES = {
    "enabled": "/DriverStation/Enabled", "robot_mode": "/DriverStation/RobotMode",
    "fixture_mode": "/Fixture/Mode", "epoch_us": "/SystemStats/EpochTime",
    "epoch_valid": "/SystemStats/EpochTimeValid", "robot_id": "/Metadata/RobotId",
    "boot_id": "/Metadata/BootId", "run_id": "/RealOutputs/TestHub/RunId",
    "terminal_event": "/Fixture/TerminalEvent",
    "state_known": "/RealOutputs/TestHub/StateKnown", "run_active": "/RealOutputs/TestHub/RunActive",
    "status_robot_id": "/RealOutputs/TestHub/RobotId", "status_boot_id": "/RealOutputs/TestHub/BootId",
    "status_enabled": "/RealOutputs/TestHub/Enabled", "status_mode": "/RealOutputs/TestHub/Mode",
    "status_monotonic_ns": "/RealOutputs/TestHub/RobotMonotonicNs",
    "status_generation": "/RealOutputs/TestHub/ModeGeneration", "status_sequence": "/RealOutputs/TestHub/Sequence",
    "transfer_allowed": "/RealOutputs/TestHub/TransferAllowed", "runtime_mode": "/RealOutputs/TestHub/RuntimeMode",
}
ODOMETRY = {
    f"/Drive/Module{i}/OdometryTimestamps": f"/Drive/Module{i}/OdometryDrivePositionsRad"
    for i in range(4)
}
ODOMETRY_ARRAY_TIMESTAMPS = {position: timestamp for timestamp, position in ODOMETRY.items()}
ODOMETRY_ARRAY_TIMESTAMPS.update({f"/Drive/Module{i}/OdometryTurnPositions": f"/Drive/Module{i}/OdometryTimestamps" for i in range(4)})
POSE_FIELDS = {"/Drive/Pose", "/Drive/PoseArray", "/RealOutputs/Odometry/Robot",
               "/ReplayOutputs/Odometry/Robot", "/RealOutputs/Odometry/TrajectorySetpoint",
               "/ReplayOutputs/Odometry/TrajectorySetpoint"}
LONG_NS_FIELDS = {f"/{category}/TestHub/RobotMonotonicNs" for category in ("RealOutputs", "ReplayOutputs")}


class FormatError(ValueError):
    pass


class UnsupportedProfile(ValueError):
    pass


class UnsupportedType(ValueError):
    pass


class ResourceLimit(ValueError):
    pass


@dataclass(frozen=True)
class Record:
    index: int
    offset: int
    entry: int
    timestamp_us: int
    payload: bytes


def _read(stream, count, label):
    data = stream.read(count)
    if len(data) != count:
        raise FormatError(f"Truncated {label}")
    return data


def records(stream, max_record_bytes=MAX_RECORD_BYTES):
    """Consume all framing, including valid records smaller than 16 bytes at EOF."""
    header = _read(stream, 12, "WPILOG header")
    if header[:6] != b"WPILOG":
        raise FormatError("Invalid WPILOG magic")
    if int.from_bytes(header[6:8], "little") != 0x100:
        raise UnsupportedProfile("Only WPILOG wire version 1.0 is qualified")
    size = int.from_bytes(header[8:12], "little")
    if size > max_record_bytes:
        raise ResourceLimit("Extra header exceeds configured record limit")
    if _read(stream, size, "extra header") != b"AdvantageKit":
        raise UnsupportedProfile("Only the AdvantageKit extra header is qualified")
    index = 0
    while True:
        offset = stream.tell()
        first = stream.read(1)
        if not first:
            return
        flag = first[0]
        if flag & 0x80:
            raise FormatError(f"Reserved record header flag at byte {offset}")
        widths = ((flag & 3) + 1, ((flag >> 2) & 3) + 1, ((flag >> 4) & 7) + 1)
        entry, size, timestamp = (int.from_bytes(_read(stream, n, "record header"), "little") for n in widths)
        if timestamp > MAX_NS // 1000:
            raise FormatError("Wire timestamp cannot be represented as Alpha7 nanoseconds")
        if size > max_record_bytes:
            raise ResourceLimit("Payload exceeds configured record limit")
        yield Record(index, offset, entry, timestamp, _read(stream, size, "record payload"))
        index += 1


def _text(data):
    try:
        return data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise FormatError("Invalid UTF-8 payload") from error


def _string(data, pos):
    if pos + 4 > len(data):
        raise FormatError("Truncated control/string-array length")
    size = int.from_bytes(data[pos:pos + 4], "little")
    pos += 4
    if pos + size > len(data):
        raise FormatError("Truncated control/string-array value")
    return _text(data[pos:pos + size]), pos + size


def _control(record, active, generations):
    p = record.payload
    if len(p) < 5:
        raise FormatError("Truncated control record")
    identity = int.from_bytes(p[1:5], "little")
    if identity == 0:
        raise FormatError("Control refers to reserved entry zero")
    if p[0] == 0:
        name, pos = _string(p, 5)
        kind, pos = _string(p, pos)
        metadata, pos = _string(p, pos)
        if not name or not kind:
            raise FormatError("Empty entry name/type")
        # A new start replaces the previous entry definition; IDs are not field identities.
        generations[identity] = generations.get(identity, 0) + 1
        active[identity] = {"field": name, "type": kind, "metadata_raw": metadata,
                            "entry_generation": generations[identity]}
        result = dict(active[identity], control="start", entry_id=identity)
    elif p[0] == 1:
        pos = 5
        if identity not in active:
            raise FormatError("Finish references an inactive entry")
        result = dict(active.pop(identity), control="finish", entry_id=identity)
    elif p[0] == 2:
        if identity not in active:
            raise FormatError("Metadata references an inactive entry")
        metadata, pos = _string(p, 5)
        active[identity]["metadata_raw"] = metadata
        result = dict(active[identity], control="metadata", entry_id=identity)
    else:
        raise FormatError("Unknown control record")
    if pos != len(p):
        raise FormatError("Trailing control payload bytes")
    return result


PRIMITIVES = {"bool": "?", "int8": "b", "uint8": "B", "int16": "h", "uint16": "H",
              "int32": "i", "uint32": "I", "int64": "q", "uint64": "Q", "float": "f", "double": "d"}
FIELD = re.compile(r"^([A-Za-z_][A-Za-z_0-9:]*)\s+([A-Za-z_][A-Za-z_0-9]*)(?:\[(\d+)\])?$")


class Structs:
    """Recorded-schema decoder. Unsupported bitfields/enums remain raw, never guessed."""
    def __init__(self, schemas):
        self.schemas = schemas
        self.cache = {}

    def layout(self, name, stack=()):
        if name in self.cache:
            return self.cache[name]
        if name in stack or len(stack) > 32:
            raise UnsupportedType("Recursive/deep struct schema")
        if name not in self.schemas:
            raise UnsupportedType(f"Missing recorded struct schema: {name}")
        fields, size, names = [], 0, set()
        for declaration in self.schemas[name].split(";"):
            if not declaration.strip():
                continue
            match = FIELD.fullmatch(declaration.strip())
            if not match:
                raise UnsupportedType(f"Unsupported struct declaration: {declaration}")
            kind, field, count = match.groups()
            count = int(count) if count else 1
            if count > 100000 or count == 0 or field in names:
                raise UnsupportedType("Invalid/oversized struct field")
            names.add(field)
            if kind in PRIMITIVES:
                width = struct.calcsize("<" + PRIMITIVES[kind])
            elif kind == "char":
                width = 1
            else:
                width = self.layout(kind.removeprefix("struct:"), stack + (name,))[1]
            fields.append((kind, field, count, width, match.group(3) is not None))
            size += width * count
        if size == 0 or size > MAX_RECORD_BYTES:
            raise UnsupportedType("Empty/oversized struct schema")
        self.cache[name] = fields, size
        return fields, size

    def decode(self, name, payload):
        fields, size = self.layout(name)
        if len(payload) != size:
            raise FormatError(f"Struct {name} payload size differs from recorded schema")
        result, pos = {}, 0
        for kind, field, count, width, array in fields:
            data = payload[pos:pos + width * count]
            pos += len(data)
            if kind in PRIMITIVES:
                values = list(struct.unpack("<" + PRIMITIVES[kind] * count, data))
            elif kind == "char":
                result[field] = _text(data.split(b"\0", 1)[0])
                continue
            else:
                values = [self.decode(kind.removeprefix("struct:"), data[i * width:(i + 1) * width]) for i in range(count)]
            result[field] = values if array else values[0]
        return result


def decode(kind, payload, structs):
    scalar = {"boolean": "?", "int64": "q", "float": "f", "double": "d"}
    if kind in scalar:
        if len(payload) != struct.calcsize("<" + scalar[kind]):
            raise FormatError(f"Invalid {kind} payload length")
        return struct.unpack("<" + scalar[kind], payload)[0]
    if kind in ("string", "json", "structschema"):
        return _text(payload)
    if kind == "string[]":
        if len(payload) < 4:
            raise FormatError("Missing string-array count")
        count = int.from_bytes(payload[:4], "little")
        if count > (len(payload) - 4) // 4:
            raise FormatError("Invalid string-array count")
        result, pos = [], 4
        for _ in range(count):
            value, pos = _string(payload, pos)
            result.append(value)
        if pos != len(payload):
            raise FormatError("Trailing string-array bytes")
        return result
    if kind.endswith("[]") and kind[:-2] in scalar:
        code = scalar[kind[:-2]]
        size = struct.calcsize("<" + code)
        if len(payload) % size:
            raise FormatError(f"Invalid {kind} payload length")
        return list(struct.unpack("<" + code * (len(payload) // size), payload))
    if kind.startswith("struct:"):
        name = kind[7:].removesuffix("[]")
        size = structs.layout(name)[1]
        if kind.endswith("[]"):
            if len(payload) % size:
                raise FormatError("Struct-array payload size differs from schema")
            return [structs.decode(name, payload[i:i + size]) for i in range(0, len(payload), size)]
        return structs.decode(name, payload)
    raise UnsupportedType(f"Unqualified field type: {kind}")


def _safe(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, int) and not isinstance(value, bool) and abs(value) > (1 << 53) - 1:
        return str(value)
    if isinstance(value, list):
        return [_safe(v) for v in value]
    if isinstance(value, dict):
        return {k: _safe(v) for k, v in value.items()}
    return value


def _finite(value):
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, (list, dict)):
        return all(_finite(v) for v in (value.values() if isinstance(value, dict) else value))
    return True


def category(field):
    for prefix, result in (("/RealOutputs/", "real_output"), ("/ReplayOutputs/", "replay_output"),
                           ("/Metadata/", "metadata"), ("/.schema/", "schema")):
        if field.startswith(prefix):
            return result
    return "input"


def _unit(row):
    try:
        metadata = json.loads(row["metadata_raw"])
    except (ValueError, TypeError):
        metadata = None
    return metadata.get("unit") if isinstance(metadata, dict) and isinstance(metadata.get("unit"), str) else None


def extract(path: Path, profile=PROFILE):
    """Yield controls, physical observations, explicitly held cycle aliases and sample rows.

    Two bounded-record passes retain schemas, not the whole recording. A caller must
    select the exact profile; wire signatures alone do not prove library versions.
    """
    if profile != PROFILE:
        raise UnsupportedProfile(f"Unsupported format profile: {profile}")
    schemas, active, generations = {}, {}, {}
    found_timestamp = False
    with Path(path).open("rb") as stream:
        for record in records(stream):
            if record.entry == 0:
                _control(record, active, generations)
                continue
            if record.entry not in active:
                raise FormatError("Data references an inactive entry")
            entry = active[record.entry]
            if entry["field"] == "/Timestamp":
                if entry["type"] != "int64" or _unit(entry) != "nanoseconds":
                    raise UnsupportedProfile("Alpha7 /Timestamp requires int64 nanoseconds metadata")
                found_timestamp = True
            if entry["field"] == "/SystemStats/EpochTime" and (entry["type"] != "double" or _unit(entry) != "microseconds"):
                raise UnsupportedProfile("Alpha7 epoch time requires double microseconds metadata")
            if entry["field"] in LONG_NS_FIELDS and (entry["type"] != "int64" or _unit(entry) not in (None, "nanoseconds")):
                raise UnsupportedProfile("TestHub RobotMonotonicNs requires int64 nanoseconds")
            if entry["field"] in {name + "Unit" for name in LONG_NS_FIELDS} and (entry["type"] != "string" or _text(record.payload) != "nanoseconds"):
                raise UnsupportedProfile("TestHub RobotMonotonicNsUnit differs from nanoseconds")
            if entry["field"] == "/Metadata/FixtureProfile":
                if entry["type"] != "string" or _text(record.payload) != profile:
                    raise UnsupportedProfile("Embedded profile differs from selected profile")
            if entry["type"] == "structschema":
                name = entry["field"].removeprefix("/.schema/struct:")
                schema = _text(record.payload)
                if name in schemas and schemas[name] != schema:
                    raise UnsupportedType("Changing struct schemas require a new importer profile")
                schemas[name] = schema
    if not found_timestamp:
        raise UnsupportedProfile("No qualified AdvantageKit /Timestamp entry")
    structs, active, generations, state = Structs(schemas), {}, {}, {}
    cycle, timestamp_index, previous_timestamp = None, None, None
    with Path(path).open("rb") as stream:
        for record in records(stream):
            base = {"record_index": record.index, "byte_offset": record.offset,
                    "record_timestamp_ns": str(record.timestamp_us * 1000)}
            if record.entry == 0:
                old_field = active.get(int.from_bytes(record.payload[1:5], "little"), {}).get("field")
                control = _control(record, active, generations)
                if control["control"] in ("finish", "start"):
                    if old_field is not None:
                        state.pop(old_field, None)
                    state.pop(control["field"], None)
                yield dict(base, kind="control", **control)
                continue
            if record.entry not in active:
                raise FormatError("Data references an inactive entry")
            entry = dict(active[record.entry])
            field = entry["field"]
            unsupported = None
            try:
                value = decode(entry["type"], record.payload, structs)
            except UnsupportedType as error:
                value, unsupported = None, str(error)
            if field == "/Timestamp":
                if not isinstance(value, int) or value < 0 or value > MAX_NS:
                    raise FormatError("Invalid AdvantageKit timestamp payload")
                if previous_timestamp is not None and value <= previous_timestamp:
                    raise FormatError("Nonadvancing cycle timestamp within one recording")
                if cycle is not None:
                    yield from _cycle_rows(cycle, timestamp_index, state)
                cycle, timestamp_index, previous_timestamp = value, record.index, value
            row = dict(base, **entry, kind="observation", entry_id=record.entry,
                       category=category(field), cycle_timestamp_ns=str(cycle) if cycle is not None else None,
                       unit=_unit(entry), value=_safe(value), decoded=unsupported is None,
                       validity="unsupported" if unsupported else ("valid" if _finite(value) else "nonfinite"),
                       coordinate_frame=None)
            if unsupported or not _finite(value):
                row["raw_hex"] = record.payload.hex()
            if unsupported:
                row["unavailable_reason"] = unsupported
            if field in LONG_NS_FIELDS and row["unit"] is None:
                row["unit"], row["unit_source"] = "nanoseconds", "explicit_profile_mapping"
            if row["unit"] == "nanoseconds" and entry["type"] in ("int64", "int64[]") and unsupported is None:
                row["value"] = [str(item) for item in value] if isinstance(value, list) else str(value)
                row["value_representation"] = "decimal_string_int64_array" if isinstance(value, list) else "decimal_string_int64"
            if field in ODOMETRY and entry["type"] == "double[]":
                row["unit"] = row["unit"] or "seconds"
                row["unit_source"] = "explicit_profile_mapping" if _unit(entry) is None else "record_metadata"
            if field in ODOMETRY.values() and entry["type"] == "double[]":
                row["unit"] = row["unit"] or "radians"
                row["unit_source"] = "explicit_profile_mapping" if _unit(entry) is None else "record_metadata"
            if field in ODOMETRY_ARRAY_TIMESTAMPS:
                row["sample_timestamp_field"] = ODOMETRY_ARRAY_TIMESTAMPS[field]
            if field in POSE_FIELDS:
                row["coordinate_frame"] = "2026-rebuilt-blue-origin"
                row["coordinate_frame_source"] = "explicit_profile_mapping"
            _canonical(row)
            state[field] = row
            yield row
        if cycle is not None:
            yield from _cycle_rows(cycle, timestamp_index, state)


def _canonical(row):
    unit = row["unit"]
    factors = {"meters": (1, "meters"), "centimeters": (.01, "meters"),
               "meters per second": (1, "meters per second"), "radians": (1, "radians"), "seconds": (1, "seconds")}
    if row["validity"] == "valid" and unit in factors:
        factor, canonical_unit = factors[unit]
        def convert(v):
            if isinstance(v, list):
                return [convert(x) for x in v]
            return v * factor if isinstance(v, (float, int)) and not isinstance(v, bool) else v
        row["canonical_value"] = convert(row["value"])
        row["canonical_unit"] = canonical_unit


def _reference(row, timestamp_index):
    return {"value": row["value"], "record_index": row["record_index"],
            "record_timestamp_ns": row["record_timestamp_ns"], "validity": row["validity"],
            "field": row["field"], "type": row["type"], "unit": row["unit"],
            "entry_generation": row["entry_generation"],
            "updated_in_cycle": row["record_index"] >= timestamp_index}


def _cycle_rows(cycle, timestamp_index, state):
    yield {"kind": "cycle", "timestamp_ns": str(cycle), "timestamp_record_index": timestamp_index,
           "bootstrap_snapshot": None,
           "aliases": {alias: _reference(state[field], timestamp_index) for alias, field in ALIASES.items() if field in state}}
    for time_field, position_field in ODOMETRY.items():
        time_row, position_row = state.get(time_field), state.get(position_field)
        if not time_row or time_row["record_index"] < timestamp_index:
            continue
        if (not position_row or time_row["validity"] != "valid" or position_row["validity"] != "valid"
                or time_row["type"] != "double[]" or position_row["type"] != "double[]"
                or time_row["unit"] != "seconds" or position_row["unit"] != "radians"
                or len(time_row["value"]) != len(position_row["value"])):
            yield {"kind": "quality", "cycle_timestamp_ns": str(cycle), "field": time_field,
                   "reason": "Odometry arrays missing, invalid, or unequal in length"}
            continue
        for i, (seconds, position) in enumerate(zip(time_row["value"], position_row["value"])):
            if seconds < 0 or seconds > MAX_NS / 1e9:
                yield {"kind": "quality", "cycle_timestamp_ns": str(cycle), "field": time_field, "reason": "Invalid odometry sample timestamp"}
                continue
            module = position_field.rsplit("/", 1)[0]
            connection_sources = {field: _reference(state[field], timestamp_index) for field in (module + "/DriveConnected", module + "/Connected") if field in state}
            statuses = [ref["value"] for ref in connection_sources.values() if ref["validity"] == "valid" and isinstance(ref["value"], bool)]
            availability = "disconnected" if False in statuses else ("connected" if statuses else "unknown")
            yield {"kind": "sample", "field": position_field, "cycle_timestamp_ns": str(cycle),
                   "sample_timestamp_seconds": seconds,
                   "sample_timestamp_ns": str(int((Decimal(str(seconds)) * 1000000000).to_integral_value(rounding=ROUND_HALF_EVEN))),
                   "timestamp_precision": "rounded_from_float_seconds", "sample_index": i,
                   "value": position, "unit": "radians", "validity": "valid",
                   "sensor_availability": availability, "connection_sources": connection_sources,
                   "timestamp_source": _reference(time_row, timestamp_index),
                   "value_source": _reference(position_row, timestamp_index)}
    for field, length_row in state.items():
        if not field.endswith("/length") or length_row["type"] != "int64":
            continue
        parent = field[:-7]
        length = length_row["value"]
        if not isinstance(length, int) or not 0 <= length <= 10000:
            continue
        children = [state.get(parent + "/" + str(i)) for i in range(length)]
        if not any(child and child["type"].endswith("[]") for child in children):
            continue
        if not any(row and row["record_index"] >= timestamp_index for row in [length_row] + children):
            continue
        yield {"kind": "nested_array", "field": parent, "cycle_timestamp_ns": str(cycle),
               "value": [child["value"] if child else None for child in children],
               "validity": "valid" if all(child and child["validity"] == "valid" for child in children) else "missing_or_invalid",
               "length_source": _reference(length_row, timestamp_index),
               "element_sources": [_reference(child, timestamp_index) if child else None for child in children]}
