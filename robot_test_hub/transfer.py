"""Bounded transfer primitives; no source deletion or robot control APIs."""
from __future__ import annotations

from dataclasses import dataclass, field
from collections import deque
import threading
import time


class TransferError(OSError):
    code = "transient"
    retryable = True


class AuthenticationError(TransferError):
    code, retryable = "authentication", False


class IdentityError(TransferError):
    code, retryable = "identity_conflict", False


class SourceMissing(TransferError):
    code, retryable = "source_missing", False


class TransferCancelled(TransferError):
    code = "permission_revoked"


class TransferTimeout(TransferError):
    code = "io_timeout"


@dataclass
class Cancellation:
    generation: tuple
    deadline: float
    event: threading.Event = field(default_factory=threading.Event)

    def cancel(self):
        self.event.set()

    @property
    def cancelled(self):
        return self.event.is_set() or time.monotonic() >= self.deadline

    def check(self):
        if self.event.is_set():
            raise TransferCancelled("Permission revoked")
        if time.monotonic() >= self.deadline:
            raise TransferTimeout("Source I/O deadline exceeded")


@dataclass(frozen=True)
class ManifestPage:
    """One atomic revision; final page has complete=True and no next cursor."""
    revision: str
    files: tuple
    next_cursor: str | None = None
    complete: bool = True
    open_bytes: int = 0
    pending_digest_bytes: int = 0


class BoundedIO:
    """One outstanding operation, even when a faulty adapter ignores cancellation.

    Client waits are bounded; the unfinished call retains the slot. Transport-side
    timeout/cancel compliance is still required to bound network cancellation tail.
    """
    def __init__(self):
        self.thread = None
        self.done = threading.Event()
        self.result = self.error = None
        self.token = None
        self.outstanding_bytes = 0
        self.maximum_bytes = 0

    @property
    def busy(self):
        return self.thread is not None and self.thread.is_alive()

    def cancel(self):
        if self.token is not None:
            self.token.cancel()

    def call(self, function, token, timeout, *, length=0, permitted=lambda: True):
        if self.busy:
            raise TransferTimeout("Previous source operation is still outstanding")
        token.check()
        if not permitted():
            token.cancel()
            raise TransferCancelled("Permission revoked before source request")
        self.done.clear()
        self.result = self.error = None
        self.token = token
        self.outstanding_bytes = length
        self.maximum_bytes = max(self.maximum_bytes, length)
        def run():
            try:
                token.check()
                self.result = function()
            except BaseException as exc:
                self.error = exc
            finally:
                self.outstanding_bytes = 0
                self.done.set()
        self.thread = threading.Thread(target=run, name="hub-source-io", daemon=True)
        self.thread.start()
        end = time.monotonic() + timeout
        while not self.done.wait(0.01):
            if not permitted() or token.event.is_set():
                token.cancel()
                raise TransferCancelled("Permission revoked during source I/O")
            if time.monotonic() >= min(end, token.deadline):
                token.cancel()
                raise TransferTimeout("Source I/O deadline exceeded")
        if self.error is not None:
            raise self.error
        return self.result


class Throughput:
    """Window in active seconds; deliberately paused wall time is excluded."""
    def __init__(self, window=15.0, minimum=2.0, historical_max_age=300.0):
        self.window, self.minimum = window, minimum
        self.historical_max_age = historical_max_age
        self.samples = deque()
        self.total_active = 0.0
        self.last_at = None
        self.historical = None
        self.profile = None

    def add(self, duration, size, now):
        duration = max(duration, 0.001)
        self.total_active += duration
        self.samples.append((duration, size))
        # Prorate the oldest sample at the active-time boundary.
        excess = sum(s[0] for s in self.samples) - self.window
        while excess > 0 and self.samples:
            elapsed, count = self.samples.popleft()
            if elapsed > excess:
                self.samples.appendleft((elapsed - excess, count * (elapsed - excess) / elapsed))
                break
            excess -= elapsed
        self.last_at = now
        rate = self.rate
        if rate is not None:
            self.historical = rate

    @property
    def rate(self):
        duration = sum(s[0] for s in self.samples)
        return sum(s[1] for s in self.samples) / duration if duration >= self.minimum else None

    def reset(self):
        self.samples.clear()

    def change_profile(self, profile):
        if self.profile != profile:
            self.reset()
            self.historical = None
            self.last_at = None
            self.profile = profile

    def estimate(self, remaining, now, *, active, complete, blocked):
        if self.last_at is not None and now - self.last_at > self.historical_max_age:
            self.reset()
            self.historical = None
        rate = self.rate if active else None
        historical = self.historical
        eta = remaining / rate if rate and not blocked else None
        rates = sorted(size / duration for duration, size in self.samples if size > 0)
        bounds = [remaining / rates[-1], remaining / rates[0]] if len(rates) >= 2 and not blocked else None
        basis = "blocked" if blocked else ("lower_bound" if not complete else ("active" if rate else "estimating"))
        if not active and historical:
            basis = "paused_historical" if not blocked else "blocked"
        return {"bytes_per_second": rate, "eta_seconds": eta,
                "historical_bytes_per_second": historical,
                "historical_eta_seconds": remaining / historical if historical and not blocked else None,
                "eta_range_seconds": bounds, "eta_basis": basis,
                "eta_is_lower_bound": not complete,
                "eta_kind": "idle transfer time; excludes future runs, waiting, and verification",
                "rate_sample_active_seconds": sum(s[0] for s in self.samples),
                "rate_link_profile": self.profile,
                "historical_rate_age_seconds": max(0, now - self.last_at) if self.last_at is not None else None,
                "historical_rate_observed_monotonic_ns": str(int(self.last_at * 1e9)) if self.last_at is not None else None}
