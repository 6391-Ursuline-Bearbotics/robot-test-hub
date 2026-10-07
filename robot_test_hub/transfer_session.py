"""Read-only, redacted local status polling; no physical transfer qualification."""
import argparse
from contextlib import contextmanager
import http.client
import json
import math
import os
import sys
import time

MAX_BODY = 1024 * 1024
MAX_SAMPLES = 100000
STATES = {'starting', 'waiting', 'paused', 'discovering', 'downloading', 'verifying',
          'caught_up', 'attention', 'stopping'}
SOURCES = {'synthetic_demo', 'systemcore_sftp_unqualified', 'unconfigured'}
COUNTS = ('remaining_bytes', 'transferable_bytes', 'blocked_bytes', 'blocked_files',
          'transferable_files', 'pending_files', 'completed_files', 'verification_pending_files',
          'open_bytes', 'pending_digest_bytes', 'outstanding_bytes', 'max_outstanding_bytes',
          'outstanding_byte_limit')
NUMBERS = ('bytes_per_second', 'historical_bytes_per_second', 'eta_seconds',
           'snapshot_age_seconds', 'verification_seconds')
FLAGS = ('paused_by_operator', 'discovery_complete', 'queue_is_last_known')
READER_COUNTS = ('restart_count', 'consecutive_failures')
READER_FLAGS = ('restart_pending', 'reader_running')
READER_CODES = {'status_bridge_not_started', 'status_bridge_starting', 'status_bridge_start_failed',
                'status_bridge_shutdown_failed', 'status_bridge_stopped', 'status_bridge_process_exited',
                'status_bridge_clock_probe_timeout', 'status_bridge_io_failed',
                'status_bridge_protocol_invalid', 'status_bridge_clock_probe_delay'}


class SnapshotUnavailable(Exception):
    """A fixed code, never a provider error message."""


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate field')
        result[key] = value
    return result


def fetch_status(port, timeout=2.):
    """Fixed loopback HTTP, without proxy handling or redirect following."""
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=timeout)
    deadline = time.monotonic() + timeout
    response = None
    try:
        connection.request('GET', '/api/v1/status', headers={'Accept': 'application/json'})
        wire_socket = connection.sock
        response = connection.getresponse()
        if response.status != 200:
            raise SnapshotUnavailable('http_status')
        chunks = []
        size = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SnapshotUnavailable('local_http_unavailable')
            if wire_socket is not None:
                wire_socket.settimeout(remaining)
            block = response.read1(min(65536, MAX_BODY + 1 - size))
            if not block:
                if response.length not in (None, 0):
                    raise SnapshotUnavailable('local_http_unavailable')
                break
            chunks.append(block)
            size += len(block)
            if size > MAX_BODY:
                raise SnapshotUnavailable('response_too_large')
        body = b''.join(chunks)
        try:
            return json.loads(body.decode('utf-8'), object_pairs_hook=_pairs,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError, UnicodeError, RecursionError):
            raise SnapshotUnavailable('invalid_json') from None
    except (OSError, http.client.HTTPException):
        raise SnapshotUnavailable('local_http_unavailable') from None
    finally:
        try:
            if response is not None:
                response.close()
        finally:
            connection.close()


def _scalar(value, kind):
    if value is None:
        return None
    if kind == 'bool' and type(value) is bool:
        return value
    if kind == 'count' and type(value) is int and 0 <= value <= 2**63-1:
        return value
    if (kind == 'number' and type(value) in (int, float) and 0 <= value <= 2**63-1
            and (type(value) is int or math.isfinite(value))):
        return value
    raise ValueError('Invalid status scalar')


def project_status(value):
    """Only fixed enums and bounded scalar fields may leave the HTTP boundary."""
    if (not isinstance(value, dict) or type(value.get('schema_version')) is not int
            or value['schema_version'] != 1 or type(value.get('state')) is not str
            or value['state'] not in STATES or type(value.get('source_type')) is not str
            or value['source_type'] not in SOURCES):
        raise ValueError('Invalid status schema')
    source = value.get('source_status')
    if not isinstance(source, dict) or type(source.get('fresh')) is not bool:
        raise ValueError('Missing source status')
    result = dict(state=value['state'], source_type=value['source_type'],
                  source_status={key: _scalar(source.get(key), 'bool')
                                 for key in ('enabled', 'transfer_allowed', 'fresh')})
    if source['fresh'] and (type(source.get('enabled')) is not bool
                            or type(source.get('transfer_allowed')) is not bool):
        raise ValueError('Ambiguous fresh status')
    result['source_status']['age_seconds'] = _scalar(source.get('age_seconds'), 'number')
    missing = []
    for fields, kind in ((COUNTS, 'count'), (NUMBERS, 'number'), (FLAGS, 'bool')):
        for key in fields:
            if key not in value:
                missing.append(key)
            result[key] = _scalar(value.get(key), kind)
    connection = value.get('connection')
    reader = connection.get('status_reader') if isinstance(connection, dict) else None
    result['status_reader'] = None
    if reader is not None:
        if not isinstance(reader, dict):
            raise ValueError('Invalid reader status')
        safe = {}
        for fields, kind in ((READER_COUNTS, 'count'), (READER_FLAGS, 'bool'), (('retry_in_seconds',), 'number')):
            for key in fields:
                if key not in reader:
                    missing.append('status_reader.' + key)
                safe[key] = _scalar(reader.get(key), kind)
        code = reader.get('error_code')
        if code is not None and (type(code) is not str or code not in READER_CODES):
            code = 'other_status_error'
        safe['error_code'] = code
        result['status_reader'] = safe
    result.update(missing_fields=missing, boot_identity_available=False,
                  cumulative_durable_bytes_available=False)
    return result


def _category(snapshot):
    if snapshot is None or not snapshot['source_status']['fresh']:
        return 'unavailable'
    if (snapshot['source_status']['enabled'] is True
            or snapshot['source_status']['transfer_allowed'] is False
            or snapshot['paused_by_operator'] is True):
        return 'paused'
    if snapshot['state'] == 'downloading':
        if snapshot['paused_by_operator'] is not False:
            return 'unavailable'
        return 'active'
    if snapshot['state'] in {'paused', 'waiting', 'stopping'}:
        return 'paused'
    return 'other'


@contextmanager
def _session_output(output, summary, finalize_failure):
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as stream:
        try:
            yield stream
        except BaseException as exc:
            try:
                finalize_failure()
            except Exception:
                pass
            summary['completion'] = 'interrupted' if isinstance(exc, KeyboardInterrupt) else 'failed'
            try:
                stream.write(json.dumps(summary, allow_nan=False, separators=(',', ':')) + '\n')
                stream.flush()
                os.fsync(stream.fileno())
            except OSError:
                pass
            raise
        else:
            stream.write(json.dumps(summary, allow_nan=False, separators=(',', ':')) + '\n')
            stream.flush()
            os.fsync(stream.fileno())


def capture(output, *, port=6391, duration=60., interval=1., fetch=fetch_status,
            monotonic_ns=time.monotonic_ns, utc_ns=time.time_ns, sleep=time.sleep):
    """Exclusive JSONL creation. Durations describe held polling observations only."""
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('Invalid port')
    for value, low, high in ((duration, 1., 86400.), (interval, .1, 60.)):
        if (type(value) not in (int, float) or not low <= value <= high
                or (type(value) is float and not math.isfinite(value))):
            raise ValueError('Invalid polling bounds')
    count = math.ceil(duration / interval)
    if count > MAX_SAMPLES:
        raise ValueError('Too many polling samples')
    summary = dict(schema_version=1, record_type='summary', completion='incomplete',
                   samples=0, valid_snapshots=0, missing_snapshots=0,
                   partial_snapshots=0, source_transitions=0, reader_restarts_observed=0,
                   reader_counter_resets=0, monotonic_clock_resets=0, utc_clock_reversals=0,
                   elapsed_seconds=0.,
                   polling_seconds={key: 0. for key in ('active', 'paused', 'unavailable', 'other')},
                   first_queue_bytes=None, last_queue_bytes=None, queue_changes=0,
                   reported_rate_min_bytes_per_second=None, reported_rate_max_bytes_per_second=None,
                   hardware_qualified=False, physical_network_measurement=False,
                   cancellation_tail_measured=False, boot_changes_observable=False,
                   cumulative_durable_bytes_available=False)
    elapsed = 0
    previous_tick = monotonic_ns()
    previous_utc = previous_source = previous_reader = previous_queue = None
    category = 'unavailable'
    def advance_elapsed():
        nonlocal previous_tick, elapsed
        tick = monotonic_ns()
        delta = tick - previous_tick
        reset = delta < 0
        if reset:
            summary['monotonic_clock_resets'] += 1
        else:
            elapsed += delta
            summary['polling_seconds'][category] += delta / 1e9
        previous_tick = tick
        summary['elapsed_seconds'] = elapsed/1e9
        return reset
    with _session_output(output, summary, advance_elapsed) as stream:
        for index in range(count):
            target = int(min(index * interval, duration) * 1e9)
            if elapsed < target:
                sleep((target - elapsed) / 1e9)
            reset = advance_elapsed()
            if elapsed >= duration * 1e9:
                break
            record = dict(schema_version=1, record_type='sample', sample_index=index,
                          elapsed_monotonic_ns=elapsed, monotonic_clock_reset=reset, snapshot=None)
            try:
                snapshot = project_status(fetch(port, timeout=min(2., max(.001, duration-elapsed/1e9))))
            except SnapshotUnavailable as exc:
                code = str(exc)
                record['error_code'] = code if code in {'http_status', 'response_too_large', 'invalid_json',
                    'local_http_unavailable'} else 'snapshot_unavailable'
            except (ValueError, TypeError, OverflowError, RecursionError):
                record['error_code'] = 'invalid_snapshot'
            except Exception:
                record['error_code'] = 'snapshot_unavailable'
            else:
                record['snapshot'] = snapshot
                summary['valid_snapshots'] += 1
                summary['partial_snapshots'] += bool(snapshot['missing_fields'])
                source = snapshot['source_type']
                if previous_source is not None and source != previous_source:
                    summary['source_transitions'] += 1
                    previous_reader = previous_queue = None
                previous_source = source
                reader = snapshot['status_reader']
                restarts = reader.get('restart_count') if reader else None
                if restarts is not None and previous_reader is not None:
                    summary['reader_restarts_observed'] += max(0, restarts-previous_reader)
                    summary['reader_counter_resets'] += restarts < previous_reader
                previous_reader = restarts
                queue = snapshot['remaining_bytes']
                if queue is not None:
                    if summary['first_queue_bytes'] is None:
                        summary['first_queue_bytes'] = queue
                    summary['last_queue_bytes'] = queue
                    summary['queue_changes'] += previous_queue is not None and queue != previous_queue
                previous_queue = queue
                rate = snapshot['bytes_per_second']
                if rate is not None:
                    for key, choose in (('reported_rate_min_bytes_per_second', min),
                                        ('reported_rate_max_bytes_per_second', max)):
                        summary[key] = rate if summary[key] is None else choose(summary[key], rate)
            if advance_elapsed():
                record['monotonic_clock_reset'] = True
            record['elapsed_monotonic_ns'] = elapsed
            stamp = utc_ns()
            if previous_utc is not None and stamp < previous_utc:
                summary['utc_clock_reversals'] += 1
            previous_utc = stamp
            record['utc_ns'] = stamp
            category = _category(record['snapshot'])
            summary['samples'] += 1
            summary['missing_snapshots'] += record['snapshot'] is None
            stream.write(json.dumps(record, allow_nan=False, separators=(',', ':')) + '\n')
            stream.flush()
        remaining = max(0., duration-elapsed/1e9)
        if remaining:
            sleep(remaining)
        advance_elapsed()
        summary['completion'] = 'complete'
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description='Save redacted local hub status polling; no robot control.')
    parser.add_argument('--output', required=True, help='New private JSONL file; existing files are never overwritten')
    parser.add_argument('--port', type=int, default=6391)
    parser.add_argument('--duration', type=float, default=60.)
    parser.add_argument('--interval', type=float, default=1.)
    parser.add_argument('--json', action='store_true', help='Print a redacted summary as JSON')
    args = parser.parse_args(argv)
    try:
        summary = capture(args.output, port=args.port, duration=args.duration, interval=args.interval)
    except (OSError, ValueError):
        print('Session capture failed: check polling bounds and choose a new writable output file.', file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print('Session interrupted; saved polling records remain available.', file=sys.stderr)
        return 130
    if args.json:
        print(json.dumps(summary, allow_nan=False))
    else:
        times = summary['polling_seconds']
        print(f"Saved {summary['samples']} polling samples; {summary['missing_snapshots']} unavailable, "
              f"{summary['partial_snapshots']} partial. Observed active {times['active']:.1f}s, "
              f"paused {times['paused']:.1f}s, unavailable {times['unavailable']:.1f}s.")
        print(f"Known queue bytes: {summary['first_queue_bytes']} to {summary['last_queue_bytes']}; "
              f"reported rate range: {summary['reported_rate_min_bytes_per_second']} to "
              f"{summary['reported_rate_max_bytes_per_second']} bytes/s (None means unavailable).")
        print('Rates and ETA are hub estimates. Polling does not measure network traffic or cancellation tails.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
