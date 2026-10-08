"""Durable hub-only annotations. Client event time is never recomputed at receipt."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import json
import math
from pathlib import Path
import re
import sqlite3
import time

ID = re.compile(r"[A-Za-z0-9_-]{1,100}")
NS = re.compile(r"-?(?:0|[1-9][0-9]{0,18})")
MAX_BODY = 16384


class NotebookError(ValueError):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.status = code, status


def install_schema(db):
    """Called within the owning catalog migration/startup transaction."""
    db.execute("""CREATE TABLE IF NOT EXISTS annotation_revisions (
        event_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision > 0),
        payload_json TEXT NOT NULL, submitted_utc_ns INTEGER,
        event_utc_start_ns INTEGER, event_utc_end_ns INTEGER,
        hub_received_utc_ns INTEGER NOT NULL,
        PRIMARY KEY(event_id, revision),
        CHECK((event_utc_start_ns IS NULL AND event_utc_end_ns IS NULL)
            OR (event_utc_start_ns IS NOT NULL AND event_utc_end_ns >= event_utc_start_ns)))""")
    db.execute("CREATE INDEX IF NOT EXISTS annotation_interval ON annotation_revisions(event_utc_start_ns,event_utc_end_ns)")


def _string(value, field, maximum, *, empty=False):
    try:
        valid = isinstance(value, str) and (empty or bool(value.strip())) and len(value.encode("utf-8")) <= maximum and "\x00" not in value
    except UnicodeEncodeError:
        valid = False
    if not valid:
        raise NotebookError("invalid_annotation", f"{field} must be {'at most' if empty else 'nonempty and at most'} {maximum} UTF-8 bytes")
    return value


def _id(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise NotebookError("invalid_annotation", "event_id must be 1–100 letters, digits, hyphens, or underscores")
    return value


def nanoseconds(value, field, *, nullable=True):
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not NS.fullmatch(value) or value == "-0":
        raise NotebookError("invalid_annotation", f"{field} must be a decimal nanosecond string or null")
    result = int(value)
    if not -(2**63) <= result < 2**63:
        raise NotebookError("invalid_annotation", f"{field} is outside signed 64-bit nanosecond range")
    return result


def _iso_utc_ns(value, field):
    _string(value, field, 128)
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?)(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})", value)
    if match is None:
        raise NotebookError("invalid_annotation", f"{field} must be an ISO date/time with an explicit UTC offset")
    try:
        date = datetime.fromisoformat(match[1] + ("+00:00" if match[3] == "Z" else match[3]))
        delta = date.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
        fraction = int((match[2] or "").ljust(9, "0"))
        return (delta.days * 86400 + delta.seconds) * 1000000000 + fraction
    except (ValueError, OverflowError):
        raise NotebookError("invalid_annotation", f"{field} contains an invalid calendar date/time") from None


def validate(payload):
    fields = {"schema_version", "event_id", "revision", "submitted_utc_ns", "client_monotonic_ns",
              "event_utc_start_ns", "event_utc_end_ns", "when", "uncertainty_ms", "clock_domain",
              "clock_quality", "text", "tags", "source", "author", "device_id", "run_id"}
    required = fields - {"author", "device_id", "run_id"}
    if not isinstance(payload, dict) or payload.keys() - fields or required - payload.keys():
        raise NotebookError("invalid_annotation", "Annotation has missing or unsupported fields")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise NotebookError("invalid_annotation", "schema_version must be 1")
    _id(payload["event_id"])
    if type(payload["revision"]) is not int or not 1 <= payload["revision"] <= 1000000:
        raise NotebookError("invalid_annotation", "revision must be an integer between 1 and 1000000")
    times = {key: nanoseconds(payload[key], key) for key in (
        "submitted_utc_ns", "client_monotonic_ns", "event_utc_start_ns", "event_utc_end_ns")}
    start, end = times["event_utc_start_ns"], times["event_utc_end_ns"]
    if (start is None) != (end is None) or (start is not None and end < start):
        raise NotebookError("invalid_annotation", "Event endpoints must both be unknown or form an ordered interval")
    _string(payload["clock_domain"], "clock_domain", 128)
    if payload["clock_quality"] not in ("unverified_client", "user_estimate", "unknown"):
        raise NotebookError("invalid_annotation", "clock_quality must be unverified_client, user_estimate, or unknown")
    uncertainty = payload["uncertainty_ms"]
    if uncertainty is not None and (type(uncertainty) not in (int, float) or not 0 <= uncertainty <= 86400000 or not math.isfinite(uncertainty)):
        raise NotebookError("invalid_annotation", "uncertainty_ms must be a finite nonnegative number up to 86400000, or null")
    if start is not None and uncertainty is None and payload["clock_quality"] != "unknown":
        raise NotebookError("invalid_annotation", "A known event interval requires an uncertainty estimate or unknown clock quality")
    when = payload["when"]
    if not isinstance(when, dict):
        raise NotebookError("invalid_annotation", "when must describe the original button/time input")
    kind = when.get("kind")
    if kind in ("now", "unknown"):
        if set(when) != {"kind"}:
            raise NotebookError("invalid_annotation", "Unexpected when fields")
        seconds = 0
    elif kind == "seconds_ago":
        seconds = when.get("seconds")
        if (set(when) != {"kind", "seconds"} or type(seconds) not in (int, float)
                or not 0 <= seconds <= 604800 or not math.isfinite(seconds)):
            raise NotebookError("invalid_annotation", "seconds_ago requires a finite seconds value between 0 and 604800")
    elif kind == "exact_interval":
        if set(when) != {"kind", "start", "end"}:
            raise NotebookError("invalid_annotation", "exact_interval requires original start and end input")
        raw_start = _iso_utc_ns(when["start"], "when.start")
        raw_end = _iso_utc_ns(when["end"], "when.end")
        if start != raw_start or end != raw_end:
            raise NotebookError("invalid_annotation", "Exact event endpoints must match the original ISO time input")
        seconds = None
    else:
        raise NotebookError("invalid_annotation", "Unsupported when kind")
    if kind == "unknown" and start is not None:
        raise NotebookError("invalid_annotation", "Unknown event time must have null UTC endpoints")
    if seconds is not None and kind != "unknown" and times["submitted_utc_ns"] is not None and start is not None:
        expected = times["submitted_utc_ns"] - int(Decimal(str(seconds)) * 1000000000)
        if start != expected or end != expected:
            raise NotebookError("invalid_annotation", "Relative event time must be computed at the client action, not hub receipt")
    _string(payload["text"], "text", 4096, empty=True)
    _string(payload["source"], "source", 128)
    if not isinstance(payload["tags"], list) or len(payload["tags"]) > 20:
        raise NotebookError("invalid_annotation", "tags must be a list of at most 20 text labels")
    for tag in payload["tags"]:
        _string(tag, "tag", 64)
    result = dict(payload)
    result["author"] = _string(payload.get("author", "unspecified"), "author", 128)
    result["device_id"] = _string(payload.get("device_id", "unspecified"), "device_id", 128)
    if payload.get("run_id") is not None:
        raise NotebookError("invalid_annotation", "Run linking is unavailable; leave run_id null")
    result["run_id"] = None
    return result, times


class Notebook:
    def __init__(self, path: Path, *, clock_ns=time.time_ns):
        self.path, self.clock_ns = Path(path), clock_ns
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            with db:
                install_schema(db)

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.path, timeout=2)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            yield db
        finally:
            db.close()

    def _annotation(self, row):
        result = json.loads(row["payload_json"])
        result.update(hub_received_utc_ns=str(row["hub_received_utc_ns"]),
                      storage_state="saved_in_hub", delivery_state="historical_hub_only")
        return result

    def save(self, payload, *, expected_previous_revision=0):
        normalized, times = validate(payload)
        revision, event_id = normalized["revision"], normalized["event_id"]
        if type(expected_previous_revision) is not int or expected_previous_revision != revision - 1:
            raise NotebookError("invalid_annotation", "expected_previous_revision must equal revision minus one")
        canonical = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            with db:
                existing = db.execute("SELECT * FROM annotation_revisions WHERE event_id=? AND revision=?", (event_id, revision)).fetchone()
                if existing is not None:
                    if existing["payload_json"] != canonical:
                        raise NotebookError("revision_conflict", "This event/revision already has different content", 409)
                    return {"schema_version": 1, "annotation": self._annotation(existing), "idempotent": True}
                latest = db.execute("SELECT MAX(revision) FROM annotation_revisions WHERE event_id=?", (event_id,)).fetchone()[0] or 0
                if latest != expected_previous_revision:
                    raise NotebookError("revision_conflict", "The expected previous revision is no longer current; refresh this note", 409)
                received = self.clock_ns()
                db.execute("INSERT INTO annotation_revisions VALUES (?,?,?,?,?,?,?)", (
                    event_id, revision, canonical, times["submitted_utc_ns"], times["event_utc_start_ns"],
                    times["event_utc_end_ns"], received))
                row = db.execute("SELECT * FROM annotation_revisions WHERE event_id=? AND revision=?", (event_id, revision)).fetchone()
                result = {"schema_version": 1, "annotation": self._annotation(row), "idempotent": False}
            return result

    def exact_revision(self, event_id, revision):
        """Return exact committed normalized bytes; not a current-revision substitution."""
        import hashlib
        _id(event_id)
        if type(revision) is not int or not 1 <= revision <= 1000000:
            raise NotebookError('invalid_annotation', 'Invalid saved revision')
        with self._connection() as db:
            row = db.execute('SELECT payload_json FROM annotation_revisions WHERE event_id=? AND revision=?', (event_id, revision)).fetchone()
            if row is None:
                raise NotebookError('annotation_revision_not_found', 'Saved note revision unavailable', 404)
            encoded = row['payload_json']
            return {'event_id': event_id, 'revision': revision, 'payload_json': encoded,
                    'sha256': hashlib.sha256(encoded.encode('utf-8')).hexdigest()}

    def list(self, *, start_ns=None, end_ns=None, include_unknown=True, limit=50, cursor=None):
        if type(limit) is not int or not 1 <= limit <= 100 or type(include_unknown) is not bool:
            raise NotebookError("invalid_query", "limit must be 1–100 and include_unknown a boolean")
        start = nanoseconds(start_ns, "from")
        end = nanoseconds(end_ns, "to")
        if (start is None) != (end is None) or (start is not None and end < start):
            raise NotebookError("invalid_query", "from/to must both be decimal UTC ns strings forming an ordered interval")
        if cursor is not None:
            _id(cursor)
        clauses, params = ["revision=(SELECT MAX(r.revision) FROM annotation_revisions r WHERE r.event_id=a.event_id)"], []
        if cursor is not None:
            clauses.append("a.event_id > ?")
            params.append(cursor)
        if start is not None:
            clauses.append("((event_utc_start_ns <= ? AND event_utc_end_ns >= ?)" + (" OR event_utc_start_ns IS NULL)" if include_unknown else ")"))
            params.extend((end, start))
        elif not include_unknown:
            clauses.append("event_utc_start_ns IS NOT NULL")
        params.append(limit + 1)
        with self._connection() as db:
            rows = db.execute("SELECT a.* FROM annotation_revisions a WHERE " + " AND ".join(clauses) + " ORDER BY a.event_id LIMIT ?", params).fetchall()
        return {"schema_version": 1, "annotations": [self._annotation(row) for row in rows[:limit]],
                "next_cursor": rows[limit - 1]["event_id"] if len(rows) > limit else None,
                "time_basis": "intended_utc_interval; clock uncertainty is displayed separately"}

    def history(self, event_id, *, limit=50, after_revision=0):
        _id(event_id)
        if type(limit) is not int or not 1 <= limit <= 100 or type(after_revision) is not int or not 0 <= after_revision <= 1000000:
            raise NotebookError("invalid_query", "History limit must be 1–100 and after_revision 0–1000000")
        with self._connection() as db:
            latest = db.execute("SELECT * FROM annotation_revisions WHERE event_id=? ORDER BY revision DESC LIMIT 1", (event_id,)).fetchone()
            if latest is None:
                raise NotebookError("annotation_not_found", "No saved note has this event_id", 404)
            rows = db.execute("SELECT * FROM annotation_revisions WHERE event_id=? AND revision>? ORDER BY revision LIMIT ?", (event_id, after_revision, limit + 1)).fetchall()
        return {"schema_version": 1, "annotation": self._annotation(latest),
                "revisions": [self._annotation(row) for row in rows[:limit]],
                "next_revision_cursor": rows[limit - 1]["revision"] if len(rows) > limit else None}
