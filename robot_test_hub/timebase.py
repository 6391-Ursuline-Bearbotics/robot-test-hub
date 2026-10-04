"""Boot-scoped, bounded clock mappings and explicit local-time resolution.

All arithmetic on epoch values is integer/rational. Mappings never extrapolate
past measured anchors or bridge invalid samples, missing intervals, or UTC steps.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

NS = 1_000_000_000
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def datetime_ns(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("A timezone-aware date/time is required")
    delta = value.astimezone(timezone.utc) - EPOCH
    return (delta.days * 86400 + delta.seconds) * NS + delta.microseconds * 1000


def local_candidates(value: str, zone: str) -> list[dict]:
    """Return both DST occurrences; reject gaps rather than guessing an offset."""
    try:
        naive = datetime.fromisoformat(value)
        tz = ZoneInfo(zone)
    except ZoneInfoNotFoundError:
        raise ValueError("Timezone unavailable; install the pinned tzdata dependency") from None
    except (ValueError, TypeError):
        raise ValueError("Use an ISO local date/time and an IANA timezone") from None
    if naive.tzinfo is not None:
        raise ValueError("Local time must omit an offset; choose the timezone separately")
    result = {}
    for fold in (0, 1):
        aware = naive.replace(tzinfo=tz, fold=fold)
        utc = aware.astimezone(timezone.utc)
        if utc.astimezone(tz).replace(tzinfo=None) != naive:
            continue
        ns = datetime_ns(utc)
        result.setdefault(ns, {"utc_ns": str(ns), "local": aware.isoformat(), "timezone": zone,
                               "fold": fold, "utc": utc.isoformat()})
    if not result:
        raise ValueError("This local time does not exist because the clock moved forward")
    return [result[key] for key in sorted(result)]


def utc_iso(ns: int) -> str:
    seconds, remainder = divmod(ns, NS)
    dt = EPOCH + timedelta(seconds=seconds)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{remainder:09d}Z"


@dataclass(frozen=True)
class Anchor:
    robot_ns: int
    utc_ns: int | None
    valid: bool
    uncertainty_ns: int = 1_000_000

    def __post_init__(self):
        if (type(self.robot_ns) is not int or self.robot_ns < 0
                or type(self.valid) is not bool
                or (self.utc_ns is not None and type(self.utc_ns) is not int)
                or type(self.uncertainty_ns) is not int or self.uncertainty_ns < 0):
            raise ValueError("Invalid clock anchor")
        if self.valid and self.utc_ns is None:
            raise ValueError("A valid clock anchor requires UTC")


@dataclass(frozen=True)
class ClockPiece:
    anchors: tuple[Anchor, ...]
    reason: str

    @property
    def start_ns(self):
        return self.anchors[0].robot_ns

    @property
    def end_ns(self):
        return self.anchors[-1].robot_ns

    def map(self, robot_ns: int) -> tuple[int, int] | None:
        if not self.start_ns <= robot_ns <= self.end_ns:
            return None
        for anchor in self.anchors:
            if anchor.robot_ns == robot_ns:
                return anchor.utc_ns, anchor.uncertainty_ns
        for left, right in zip(self.anchors, self.anchors[1:]):
            if left.robot_ns < robot_ns < right.robot_ns:
                f = Fraction(robot_ns - left.robot_ns, right.robot_ns - left.robot_ns)
                utc = Fraction(left.utc_ns) + f * (right.utc_ns - left.utc_ns)
                # Bound by the less certain endpoint plus integer rounding.
                return round(utc), max(left.uncertainty_ns, right.uncertainty_ns) + 1
        return None


@dataclass(frozen=True)
class ClockMapping:
    boot_id: str
    revision: str
    pieces: tuple[ClockPiece, ...]
    rejected: tuple[dict, ...]

    def map(self, boot_id: str, robot_ns: int) -> dict:
        if boot_id != self.boot_id:
            raise ValueError("Monotonic timestamps cannot be mapped across boots")
        if type(robot_ns) is not int:
            raise ValueError("Robot timestamp must be integer nanoseconds")
        for index, piece in enumerate(self.pieces):
            point = piece.map(robot_ns)
            if point is not None:
                return {"utc_ns": str(point[0]), "uncertainty_ns": str(point[1]),
                        "mapping_revision": self.revision, "piece": index, "quality": "anchored"}
        return {"utc_ns": None, "uncertainty_ns": None, "mapping_revision": self.revision,
                "piece": None, "quality": "unavailable", "reason": "outside_valid_anchor_coverage"}


def build_mapping(boot_id: str, revision: str, anchors: list[Anchor], *,
                  max_gap_ns: int = NS, max_drift_ppm: int = 1000,
                  jump_tolerance_ns: int = 5_000_000) -> ClockMapping:
    """Versioned policy splits steps/gaps; tolerances are explicit, not calibration."""
    if not boot_id or not revision:
        raise ValueError("Boot identity and mapping revision are required")
    for value in (max_gap_ns, max_drift_ppm, jump_tolerance_ns):
        if type(value) is not int or value < 0:
            raise ValueError("Clock policy limits must be nonnegative integers")
    if max_gap_ns == 0:
        raise ValueError("Maximum anchor gap must be positive")
    pieces, rejected, active = [], [], []
    reason = "first_anchor"
    previous_ns = None

    def finish():
        if active:
            pieces.append(ClockPiece(tuple(active), reason))
            active.clear()

    for anchor in anchors:
        if previous_ns is not None and anchor.robot_ns <= previous_ns:
            raise ValueError("Anchors must be strictly increasing within one boot")
        previous_ns = anchor.robot_ns
        if not anchor.valid or anchor.utc_ns is None or anchor.utc_ns <= 0:
            finish()
            rejected.append({"robot_ns": str(anchor.robot_ns), "reason": "invalid_epoch"})
            reason = "after_invalid_epoch"
            continue
        if active:
            left = active[-1]
            delta = anchor.robot_ns - left.robot_ns
            error = abs((anchor.utc_ns - left.utc_ns) - delta)
            tolerance = max(jump_tolerance_ns, left.uncertainty_ns + anchor.uncertainty_ns,
                            (delta * max_drift_ppm + 999999) // 1000000)
            split = ("anchor_gap" if delta > max_gap_ns else
                     "utc_discontinuity" if error > tolerance or anchor.utc_ns <= left.utc_ns else None)
            if split:
                finish()
                reason = split
        active.append(anchor)
    finish()
    return ClockMapping(boot_id, revision, tuple(pieces), tuple(rejected))
