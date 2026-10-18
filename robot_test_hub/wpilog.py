"""Strict, portable extractor for the explicitly qualified Alpha7/AK Alpha6 profile.

No robot code, official iterator, timestamps inferred from filenames, or replay-held
observations. Wire units and table payload units are distinct throughout.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_EVEN
from functools import lru_cache
import json
import math
from pathlib import Path
import re
import struct
from typing import NamedTuple

PROFILE = "wpilib-2027.0.0-alpha-7_akit-27.0.0-alpha-6"
EXTRACTOR_VERSION = "wpilog-python-1"
MAPPING_REVISION = "alpha7-aliases-2"
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
    "real_metadata_robot_id": "/RealMetadata/RobotId", "real_metadata_boot_id": "/RealMetadata/BootId",
    "source_sha256": "/RealMetadata/SourceSHA256", "artifact_sha256": "/RealMetadata/ArtifactSHA256",
    "configuration_sha256": "/RealOutputs/TestHub/ConfigurationSHA256",
}
# Historical 2026 roboRIO logs (WPILib 2026, AdvantageKit 26). The library versions
# are not recorded in the file; selecting this profile is an explicit caller
# assertion, like the Alpha 7 profile. AdvantageKit 26 logs /Timestamp as int64
# microseconds without unit metadata, equal to the record header time of every
# record in that logger cycle.
PROFILE_AKIT26 = "wpilib-2026_akit-26"
MAPPING_REVISION_AKIT26 = "akit26-aliases-1"
PROFILE_MAPPINGS = {PROFILE: MAPPING_REVISION, PROFILE_AKIT26: MAPPING_REVISION_AKIT26}
ALIASES_AKIT26 = {
    "enabled": "/DriverStation/Enabled", "ds_autonomous": "/DriverStation/Autonomous",
    "ds_test": "/DriverStation/Test", "fms_attached": "/DriverStation/FMSAttached",
    "match_time": "/DriverStation/MatchTime", "match_type": "/DriverStation/MatchType",
    "match_number": "/DriverStation/MatchNumber", "event_name": "/DriverStation/EventName",
    "epoch_us": "/SystemStats/EpochTimeMicros", "robot_id": "/SystemStats/SerialNumber",
    "team_number": "/SystemStats/TeamNumber",
}
# Epoch values before 2020-01-01 mean the roboRIO clock was not yet set by the DS.
AKIT26_EPOCH_VALID_AFTER_US = 1_577_836_800_000_000
# EpochTimeMicros is read during the cycle, not at the cycle timestamp. On a real
# September 2026 match log its offset from the cycle time moved by 1-10 ms per cycle
# and occasionally ~100-150 ms (loop overruns). Each anchor states this uncertainty.
AKIT26_EPOCH_UNCERTAINTY_NS = 200_000_000
AKIT26_DERIVED = {
    "robot_mode": {"derived_from": ["/DriverStation/Enabled", "/DriverStation/Autonomous", "/DriverStation/Test"],
                   "derivation": "AUTONOMOUS if Autonomous, TEST if Test, else TELEOPERATED (held DS booleans)"},
    "epoch_valid": {"derived_from": ["/SystemStats/EpochTimeMicros"],
                    "derivation": f"EpochTimeMicros > {AKIT26_EPOCH_VALID_AFTER_US} (clock set after 2020-01-01)"},
}


def aliases_for(profile):
    return ALIASES_AKIT26 if profile == PROFILE_AKIT26 else ALIASES


ODOMETRY = {
    f"/Drive/Module{i}/OdometryTimestamps": f"/Drive/Module{i}/OdometryDrivePositionsRad"
    for i in range(4)
}
ODOMETRY_POSITIONS = frozenset(ODOMETRY.values())
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


class Record(NamedTuple):
    index: int
    offset: int
    entry: int
    timestamp_us: int
    payload: bytes


READ_CHUNK_BYTES = 1024 * 1024
MAX_RECORD_HEADER_BYTES = 1 + 4 + 4 + 8


def _read(stream, count, label):
    data = stream.read(count)
    if len(data) != count:
        raise FormatError(f"Truncated {label}")
    return data


def records(stream, max_record_bytes=MAX_RECORD_BYTES, truncated_tail=None):
    """Consume all framing, including valid records smaller than 16 bytes at EOF.

    A dict passed as truncated_tail accepts one incomplete final record (a log cut
    off by power loss): its offset and byte count are stored there and iteration
    ends. Otherwise incomplete framing raises FormatError. Records are parsed from
    bounded read-ahead chunks; memory stays bounded by the record limit.
    """
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
    from_bytes, limit_us = int.from_bytes, MAX_NS // 1000
    buffer, pos, base, eof, index = b"", 0, stream.tell(), False, 0

    def fill(needed):
        # Returns the buffer holding at least `needed` unread bytes, unless EOF comes first.
        nonlocal buffer, pos, base, eof
        parts = [buffer[pos:]]
        have = len(parts[0])
        base += pos
        while have < needed and not eof:
            block = stream.read(max(READ_CHUNK_BYTES, needed - have))
            if not block:
                eof = True
            parts.append(block)
            have += len(block)
        buffer, pos = b"".join(parts), 0

    while True:
        if len(buffer) - pos < MAX_RECORD_HEADER_BYTES and not eof:
            fill(MAX_RECORD_HEADER_BYTES)
        if pos >= len(buffer):
            return
        offset = base + pos
        flag = buffer[pos]
        if flag & 0x80:
            raise FormatError(f"Reserved record header flag at byte {offset}")
        entry_end = pos + 1 + (flag & 3) + 1
        size_end = entry_end + ((flag >> 2) & 3) + 1
        header_end = size_end + ((flag >> 4) & 7) + 1
        try:
            if header_end > len(buffer):
                raise FormatError("Truncated record header")
            entry = from_bytes(buffer[pos + 1:entry_end], "little")
            size = from_bytes(buffer[entry_end:size_end], "little")
            timestamp = from_bytes(buffer[size_end:header_end], "little")
            if timestamp > limit_us:
                raise FormatError("Wire timestamp cannot be represented as Alpha7 nanoseconds")
            if size > max_record_bytes:
                raise ResourceLimit("Payload exceeds configured record limit")
            if header_end + size > len(buffer):
                header_end -= pos
                fill(header_end + size)
                if header_end + size > len(buffer):
                    raise FormatError("Truncated record payload")
            payload = buffer[header_end:header_end + size]
        except FormatError as error:
            if truncated_tail is None or not str(error).startswith("Truncated"):
                raise
            end = stream.seek(0, 2)
            truncated_tail.update(offset=offset, bytes=end - offset, reason=str(error))
            return
        pos = header_end + size
        yield Record(index, offset, entry, timestamp, payload)
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
        self.plans = {}

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

    def _plan(self, name):
        # Precompiled unpackers for a validated layout; same field order and semantics.
        fields, size = self.layout(name)
        steps = [(kind, field, count, width, array,
                  struct.Struct("<" + PRIMITIVES[kind] * count) if kind in PRIMITIVES else None,
                  kind.removeprefix("struct:"))
                 for kind, field, count, width, array in fields]
        self.plans[name] = plan = (size, steps)
        return plan

    def decode(self, name, payload):
        size, steps = self.plans.get(name) or self._plan(name)
        if len(payload) != size:
            raise FormatError(f"Struct {name} payload size differs from recorded schema")
        result, pos = {}, 0
        for kind, field, count, width, array, packer, nested in steps:
            end = pos + width * count
            if packer is not None:
                values = packer.unpack(payload[pos:end])
                result[field] = list(values) if array else values[0]
            elif kind == "char":
                result[field] = _text(payload[pos:end].split(b"\0", 1)[0])
            else:
                values = [self.decode(nested, payload[i:i + width]) for i in range(pos, end, width)]
                result[field] = values if array else values[0]
            pos = end
        return result


SCALARS = {"boolean": "?", "int64": "q", "float": "f", "double": "d"}
_SCALAR_STRUCTS = {kind: struct.Struct("<" + code) for kind, code in SCALARS.items()}


def decode(kind, payload, structs):
    unpacker = _SCALAR_STRUCTS.get(kind)
    if unpacker is not None:
        if len(payload) != unpacker.size:
            raise FormatError(f"Invalid {kind} payload length")
        return unpacker.unpack(payload)[0]
    scalar = SCALARS
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


MAX_SAFE_INTEGER = (1 << 53) - 1


def _safe(value):
    # Exact-type fast paths first; the isinstance chain below keeps subclass behavior.
    kind = type(value)
    if kind is float:
        return value if math.isfinite(value) else None
    if kind is list:
        return [_safe(v) for v in value]
    if kind is bool or kind is str or value is None:
        return value
    if kind is int:
        return str(value) if abs(value) > MAX_SAFE_INTEGER else value
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
    kind = type(value)
    if kind is float:
        return math.isfinite(value)
    if kind is list or kind is dict:
        for item in (value.values() if kind is dict else value):
            if not _finite(item):
                return False
        return True
    if kind is bool or kind is int or kind is str or value is None:
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, (list, dict)):
        return all(_finite(v) for v in (value.values() if isinstance(value, dict) else value))
    return True


@lru_cache(maxsize=16384)
def category(field):
    for prefix, result in (("/RealOutputs/", "real_output"), ("/ReplayOutputs/", "replay_output"),
                           ("/RealMetadata/", "metadata"), ("/ReplayMetadata/", "replay_metadata"),
                           ("/Metadata/", "metadata"), ("/.schema/", "schema")):
        if field.startswith(prefix):
            return result
    return "input"


def _unit(row):
    raw = row["metadata_raw"]
    # Entry metadata repeats on every record of an entry; parse each distinct string once.
    return _cached_unit(raw) if type(raw) is str else _parse_unit(raw)


def _parse_unit(raw):
    try:
        metadata = json.loads(raw)
    except (ValueError, TypeError):
        metadata = None
    return metadata.get("unit") if isinstance(metadata, dict) and isinstance(metadata.get("unit"), str) else None


_cached_unit = lru_cache(maxsize=16384)(_parse_unit)


def extract(path: Path, profile=PROFILE):
    """Yield controls, physical observations, explicitly held cycle aliases and sample rows.

    Two bounded-record passes retain schemas, not the whole recording. A caller must
    select the exact profile; wire signatures alone do not prove library versions.
    """
    if profile == PROFILE_AKIT26:
        yield from _extract_akit26(Path(path))
        return
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
    cycle, timestamp_index, previous_timestamp, lengths = None, None, None, None
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
                    lengths = None
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
                    lengths = lengths if lengths is not None else _length_fields(state)
                    yield from _cycle_rows(cycle, timestamp_index, state, lengths=lengths)
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
            if field in ODOMETRY_POSITIONS and entry["type"] == "double[]":
                row["unit"] = row["unit"] or "radians"
                row["unit_source"] = "explicit_profile_mapping" if _unit(entry) is None else "record_metadata"
            if field in ODOMETRY_ARRAY_TIMESTAMPS:
                row["sample_timestamp_field"] = ODOMETRY_ARRAY_TIMESTAMPS[field]
            if field in POSE_FIELDS:
                row["coordinate_frame"] = "2026-rebuilt-blue-origin"
                row["coordinate_frame_source"] = "explicit_profile_mapping"
            _canonical(row)
            if field not in state:
                lengths = None
            state[field] = row
            yield row
        if cycle is not None:
            yield from _cycle_rows(cycle, timestamp_index, state, lengths=_length_fields(state))


def _observation(record, entry, structs, cycle):
    field = entry["field"]
    unsupported = None
    try:
        value = decode(entry["type"], record.payload, structs)
    except UnsupportedType as error:
        value, unsupported = None, str(error)
    finite = _finite(value)
    row = dict(record_index=record.index, byte_offset=record.offset,
               record_timestamp_ns=str(record.timestamp_us * 1000), **entry, kind="observation",
               entry_id=record.entry, category=category(field),
               cycle_timestamp_ns=str(cycle) if cycle is not None else None,
               unit=_unit(entry), value=_safe(value), decoded=unsupported is None,
               validity="unsupported" if unsupported else ("valid" if finite else "nonfinite"),
               coordinate_frame=None)
    if unsupported or not finite:
        row["raw_hex"] = record.payload.hex()
    if unsupported:
        row["unavailable_reason"] = unsupported
    return row


def _extract_akit26(path):
    """Historical AdvantageKit 26 logs: cycles are runs of equal record timestamps."""
    schemas, active, generations, found_data = {}, {}, {}, False
    with path.open("rb") as stream:
        for record in records(stream, truncated_tail={}):
            if record.entry == 0:
                _control(record, active, generations)
                continue
            if record.entry not in active:
                raise FormatError("Data references an inactive entry")
            entry = active[record.entry]
            found_data = True
            if entry["field"] == "/Timestamp" and (entry["type"] != "int64" or _unit(entry) not in (None, "microseconds")):
                raise UnsupportedProfile("AdvantageKit 26 /Timestamp must be int64 microseconds")
            if entry["field"] == "/SystemStats/EpochTimeMicros" and entry["type"] != "int64":
                raise UnsupportedProfile("AdvantageKit 26 EpochTimeMicros must be int64")
            if entry["type"] == "structschema":
                name = entry["field"].removeprefix("/.schema/struct:")
                schema = _text(record.payload)
                if name in schemas and schemas[name] != schema:
                    raise UnsupportedType("Changing struct schemas require a new importer profile")
                schemas[name] = schema
    if not found_data:
        raise UnsupportedProfile("No AdvantageKit data records")
    structs, active, generations, state, tail = Structs(schemas), {}, {}, {}, {}
    cycle, timestamp_index, lengths = None, None, None
    with path.open("rb") as stream:
        for record in records(stream, truncated_tail=tail):
            if record.entry == 0:
                old_field = active.get(int.from_bytes(record.payload[1:5], "little"), {}).get("field")
                control = _control(record, active, generations)
                if control["control"] in ("finish", "start"):
                    if old_field is not None:
                        state.pop(old_field, None)
                    state.pop(control["field"], None)
                    lengths = None
                yield dict(record_index=record.index, byte_offset=record.offset,
                           record_timestamp_ns=str(record.timestamp_us * 1000), kind="control", **control)
                continue
            if record.entry not in active:
                raise FormatError("Data references an inactive entry")
            timestamp = record.timestamp_us * 1000
            if cycle is None or timestamp != cycle:
                if cycle is not None and timestamp < cycle:
                    raise FormatError("Nonadvancing record timestamp within one recording")
                if cycle is not None:
                    lengths = lengths if lengths is not None else _length_fields(state)
                    yield from _cycle_rows(cycle, timestamp_index, state, profile=PROFILE_AKIT26, lengths=lengths)
                cycle, timestamp_index = timestamp, record.index
            row = _observation(record, active[record.entry], structs, cycle)
            field = row["field"]
            if field == "/Timestamp":
                if row["value"] != record.timestamp_us:
                    raise FormatError("AdvantageKit 26 /Timestamp differs from its record header time")
                if row["unit"] is None:
                    row["unit"], row["unit_source"] = "microseconds", "explicit_profile_mapping"
            if field == "/SystemStats/EpochTimeMicros" and row["unit"] is None:
                row["unit"], row["unit_source"] = "microseconds", "explicit_profile_mapping"
            if field in ODOMETRY and row["type"] == "double[]":
                row["unit_source"] = "explicit_profile_mapping" if row["unit"] is None else "record_metadata"
                row["unit"] = row["unit"] or "seconds"
            if field in ODOMETRY_POSITIONS and row["type"] == "double[]":
                row["unit_source"] = "explicit_profile_mapping" if row["unit"] is None else "record_metadata"
                row["unit"] = row["unit"] or "radians"
            if field in ODOMETRY_ARRAY_TIMESTAMPS:
                row["sample_timestamp_field"] = ODOMETRY_ARRAY_TIMESTAMPS[field]
            if field in POSE_FIELDS:
                row["coordinate_frame"] = "2026-rebuilt-blue-origin"
                row["coordinate_frame_source"] = "explicit_profile_mapping"
            _canonical(row)
            if field not in state:
                lengths = None
            state[field] = row
            yield row
        if cycle is not None:
            yield from _cycle_rows(cycle, timestamp_index, state, profile=PROFILE_AKIT26, lengths=_length_fields(state))
    if tail:
        yield {"kind": "quality", "cycle_timestamp_ns": str(cycle) if cycle is not None else None,
               "field": None, "reason": "Final record cut off (log ended mid-write, usually power loss)",
               "truncated_tail_offset": tail["offset"], "truncated_tail_bytes": tail["bytes"]}


def _akit26_derived(state, timestamp_index):
    def ref(field):
        row = state.get(field)
        return _reference(row, timestamp_index) if row else None
    derived = {}
    enabled, auto, test = ref("/DriverStation/Enabled"), ref("/DriverStation/Autonomous"), ref("/DriverStation/Test")
    if auto and test and type(auto["value"]) is bool and type(test["value"]) is bool:
        mode = "AUTONOMOUS" if auto["value"] else "TEST" if test["value"] else "TELEOPERATED"
        sources = [r for r in (enabled, auto, test) if r]
        derived["robot_mode"] = {"value": mode, "validity": "valid", "derived": True,
                                 "updated_in_cycle": any(r["updated_in_cycle"] for r in sources),
                                 "sources": sources, **AKIT26_DERIVED["robot_mode"]}
    epoch = ref("/SystemStats/EpochTimeMicros")
    if epoch:
        epoch["uncertainty_ns"] = AKIT26_EPOCH_UNCERTAINTY_NS
        epoch["uncertainty_basis"] = "epoch read during cycle, not at cycle timestamp (AdvantageKit 26 profile)"
        derived["epoch_us"] = epoch
    if epoch and epoch["validity"] == "valid" and type(epoch["value"]) is int:
        derived["epoch_valid"] = {"value": epoch["value"] > AKIT26_EPOCH_VALID_AFTER_US, "validity": "valid",
                                  "derived": True, "updated_in_cycle": epoch["updated_in_cycle"],
                                  "sources": [epoch], **AKIT26_DERIVED["epoch_valid"]}
    return derived


CANONICAL_FACTORS = {"meters": (1, "meters"), "centimeters": (.01, "meters"),
                     "meters per second": (1, "meters per second"), "radians": (1, "radians"), "seconds": (1, "seconds")}


def _canonical(row):
    unit = row["unit"]
    if row["validity"] == "valid" and unit in CANONICAL_FACTORS:
        factor, canonical_unit = CANONICAL_FACTORS[unit]
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


def _length_fields(state):
    # Nested-array length fields in state order; callers recompute after any key insertion/removal.
    return [field for field in state if field.endswith("/length")]


def _cycle_rows(cycle, timestamp_index, state, profile=PROFILE, lengths=None):
    aliases = {alias: _reference(state[field], timestamp_index) for alias, field in aliases_for(profile).items() if field in state}
    if profile == PROFILE_AKIT26:
        aliases.update(_akit26_derived(state, timestamp_index))
    yield {"kind": "cycle", "timestamp_ns": str(cycle), "timestamp_record_index": timestamp_index,
           "bootstrap_snapshot": None, "aliases": aliases}
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
    for field in (_length_fields(state) if lengths is None else lengths):
        length_row = state[field]
        if length_row["type"] != "int64":
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
