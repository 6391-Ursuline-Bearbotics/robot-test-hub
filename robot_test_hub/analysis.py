"""Versioned, deterministic analyzer jobs over immutable imported evidence."""
from __future__ import annotations
from dataclasses import asdict, dataclass, field
import hashlib
import copy
import json
import math
import time
from typing import Callable

OUTCOMES = ('evaluated_no_finding', 'finding', 'insufficient_data', 'unsupported', 'failed')
VERSION = 'analysis-framework-2'
CONNECTION_FIELDS = frozenset(
    f'/Drive/Module{i}/{name}' for i in range(4)
    # Connected is the explicit synthetic fixture/importer alias; the three
    # device-specific paths are the pinned robot ModuleIO connection fields.
    for name in ('DriveConnected', 'TurnConnected', 'TurnEncoderConnected', 'Connected')
) | {'/Drive/Gyro/Connected'}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def identity(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


@dataclass(frozen=True)
class Signal:
    path: str
    unit: str | None
    category: str = 'input'


@dataclass(frozen=True)
class Declaration:
    analyzer_id: str
    version: str
    supported_profiles: tuple[str, ...]
    required: tuple[Signal, ...] = ()
    optional: tuple[Signal, ...] = ()
    eligible_windows: str = 'entire run'
    minimum_coverage: float = 0
    grouping_keys: tuple[str, ...] = ()
    baseline_required: bool = False

    def __post_init__(self):
        if not self.analyzer_id or not self.version or not self.supported_profiles:
            raise ValueError('Analyzer identity/version/profiles are required')
        if not 0 <= self.minimum_coverage <= 1:
            raise ValueError('Minimum coverage must be between zero and one')


@dataclass(frozen=True)
class Evidence:
    profile: str
    source_hashes: tuple[str, ...]
    rows: tuple[dict, ...]
    context: dict
    mapping_revision: str
    importer_version: str
    source_type: str

    def __post_init__(self):
        if (not self.source_hashes or any(not isinstance(v, str) or len(v) != 64 or any(c not in '0123456789abcdef' for c in v) for v in self.source_hashes)
                or not self.profile or not self.mapping_revision or not self.importer_version
                or self.source_type not in ('synthetic', 'real', 'simulation', 'historical')):
            raise ValueError('Immutable evidence hashes, versions, profile and explicit source type required')
        canonical(asdict(self))  # Reject nonfinite/unserializable context rather than coerce.


def result(outcome, *, metrics=None, coverage=None, findings=None, unavailable=None):
    if outcome not in OUTCOMES:
        raise ValueError('Invalid analyzer outcome')
    return {'outcome': outcome, 'metrics': metrics or [], 'coverage': coverage or {},
            'findings': findings or [], 'unavailable': unavailable or []}


def metric(name, value, unit, *, sample_count=0, eligible_seconds=0):
    if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
        raise ValueError('Metric value must be finite or null')
    return {'name': name, 'value': value, 'unit': unit, 'sample_count': sample_count,
            'eligible_seconds': eligible_seconds}


def install_schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS analyzer_jobs (
        job_id TEXT PRIMARY KEY, framework_version TEXT NOT NULL,
        analyzer_id TEXT NOT NULL, analyzer_version TEXT NOT NULL,
        declaration_json TEXT NOT NULL, input_json TEXT NOT NULL,
        configuration_json TEXT NOT NULL, outcome TEXT NOT NULL,
        result_json TEXT NOT NULL, created_utc_ns TEXT NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS analysis_reports (
        report_id TEXT PRIMARY KEY, provenance_json TEXT NOT NULL,
        result_json TEXT NOT NULL, created_utc_ns TEXT NOT NULL)''')


class Analyzer:
    def __init__(self, declaration: Declaration, evaluate: Callable):
        self.declaration, self.evaluate = declaration, evaluate


class Runner:
    def __init__(self, db):
        self.db = db
        if db.in_transaction:
            install_schema(db)
        else:
            with db:
                install_schema(db)

    def run(self, analyzer, evidence: Evidence, configuration=None):
        configuration = json.loads(canonical(configuration or {}))
        declaration = asdict(analyzer.declaration)
        # Pin content as well as references. Different content under the same source
        # reference cannot silently reuse an earlier result.
        input_ref = {k:v for k,v in asdict(evidence).items() if k != 'rows'}
        input_ref['rows_digest'] = identity(evidence.rows)
        job_id = identity([VERSION, declaration, input_ref, configuration])
        old = self.db.execute('SELECT result_json FROM analyzer_jobs WHERE job_id=?', (job_id,)).fetchone()
        if old:
            return json.loads(old[0])
        unsupported, missing = [], []
        if evidence.profile not in analyzer.declaration.supported_profiles:
            unsupported.append('unsupported_schema_profile')
        for signal in analyzer.declaration.required:
            values = [r for r in evidence.rows if r.get('kind') == 'observation' and r.get('field') == signal.path and r.get('category') == signal.category]
            if not values or not any(r.get('validity') == 'valid' and r.get('value') is not None for r in values):
                missing.append('missing_or_invalid_signal:' + signal.path)
            elif signal.unit is not None and any(r.get('unit') != signal.unit for r in values):
                unsupported.append('incompatible_unit:' + signal.path)
        if analyzer.declaration.baseline_required and not evidence.context.get('approved_baseline_version'):
            missing.append('approved_baseline_unavailable')
        if unsupported:
            output = result('unsupported', unavailable=unsupported)
        elif missing:
            output = result('insufficient_data', unavailable=missing)
        else:
            try:
                output = analyzer.evaluate(copy.deepcopy(evidence), copy.deepcopy(configuration))
                if output.get('outcome') not in OUTCOMES:
                    raise ValueError('Analyzer returned invalid outcome')
                fraction = output.get('coverage',{}).get('fraction')
                if analyzer.declaration.minimum_coverage and output['outcome'] in ('evaluated_no_finding','finding'):
                    if type(fraction) not in (int,float) or not math.isfinite(fraction) or fraction < analyzer.declaration.minimum_coverage:
                        output = result('insufficient_data', metrics=output.get('metrics'), coverage=output.get('coverage'), unavailable=['declared_minimum_coverage_not_met'])
                if output['outcome'] == 'evaluated_no_finding' and output.get('unavailable'):
                    # A no-finding result may include unrelated unavailable checks;
                    # the report always carries their explicit reasons and coverage.
                    output['coverage']['has_unavailable_checks'] = True
                canonical(output)
            except Exception as exc:
                output = result('failed', unavailable=['analyzer_exception:' + type(exc).__name__])
        output.update({'job_id': job_id, 'analyzer_id': analyzer.declaration.analyzer_id,
                       'analyzer_version': analyzer.declaration.version, 'provenance': input_ref,
                       'configuration': configuration, 'declaration': declaration})
        output = json.loads(canonical(output))
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO analyzer_jobs VALUES (?,?,?,?,?,?,?,?,?,?)',
                (job_id, VERSION, analyzer.declaration.analyzer_id, analyzer.declaration.version,
                 canonical(declaration), canonical(input_ref), canonical(configuration), output['outcome'],
                 canonical(output), str(time.time_ns())))
        return output

    def report(self, analyzers, evidence, configurations=None):
        configurations = configurations or {}
        checks = [self.run(a, evidence, configurations.get(a.declaration.analyzer_id)) for a in analyzers]
        report_id = identity([VERSION, [c['job_id'] for c in checks]])
        doc = {'report_id': report_id, 'framework_version': VERSION, 'source_type': evidence.source_type,
               'source_hashes': list(evidence.source_hashes), 'run_id': evidence.context.get('run_id'),
               'checks': checks, 'evaluated_checks': sum(c['outcome'] in ('finding','evaluated_no_finding') for c in checks),
               'unavailable_checks': sum(c['outcome'] in ('insufficient_data','unsupported','failed') for c in checks),
               'finding_count': sum(len(c['findings']) for c in checks), 'overall_health': 'not_assessed'}
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO analysis_reports VALUES (?,?,?,?)', (report_id, canonical({'source_hashes': evidence.source_hashes, 'context': evidence.context}), canonical(doc), str(time.time_ns())))
        return doc


def data_quality(evidence, config):
    max_gap_ns = config.get('max_gap_ns', 250_000_000)
    if type(max_gap_ns) is not int or max_gap_ns <= 0:
        raise ValueError('max_gap_ns must be positive integer nanoseconds')
    cycles = [r for r in evidence.rows if r.get('kind') == 'cycle']
    if len(cycles) < 2:
        return result('insufficient_data', unavailable=['at_least_two_cycles_required'])
    stamps, invalid = [], 0
    for row in cycles:
        try:
            stamp = int(row['timestamp_ns'])
            if stamp < 0:
                raise ValueError()
            stamps.append(stamp)
        except (KeyError, ValueError, TypeError):
            invalid += 1
    nonadvancing = sum(b <= a for a,b in zip(stamps, stamps[1:]))
    gaps = [(a,b) for a,b in zip(stamps, stamps[1:]) if b-a > max_gap_ns]
    observations = [r for r in evidence.rows if r.get('kind') == 'observation']
    invalid_observations = [r for r in observations if r.get('validity') != 'valid']
    stale = [r for r in observations if r.get('fresh') is False]
    connection_observations = [r for r in observations if r.get('field') in CONNECTION_FIELDS]
    disconnected = [r for r in observations if r.get('connected') is False or
                    (r.get('field') in CONNECTION_FIELDS and r.get('validity') == 'valid'
                     and r.get('type') == 'boolean' and r.get('value') is False)]
    samples = [r for r in evidence.rows if r.get('kind') == 'sample']
    # The importer represents acquisition-array availability separately from
    # raw connection observations. Preserve its source references, deduplicating
    # a false status seen both as an observation and in multiple samples.
    seen = {(r.get('source_sha256'), r.get('record_index'), r.get('field')) for r in disconnected}
    for row in samples:
        if row.get('sensor_availability') != 'disconnected':
            continue
        references = row.get('connection_sources', {})
        false_sources = [(field, ref) for field, ref in references.items()
                         if ref.get('validity') == 'valid' and ref.get('value') is False]
        candidates = [dict(ref, field=field, source_sha256=row.get('source_sha256'))
                      for field, ref in false_sources] or [row]
        for source in candidates:
            key = (source.get('source_sha256'), source.get('record_index'), source.get('field'))
            if key not in seen:
                disconnected.append(source)
                seen.add(key)
    clock_valid = [r.get('aliases',{}).get('epoch_valid',{}).get('value') for r in cycles]
    unknown_clock = sum(v is not True for v in clock_valid)
    duration = (max(stamps)-min(stamps))/1e9 if len(stamps)>1 else 0
    valid_duration = sum((b-a)/1e9 for a,b in zip(stamps,stamps[1:]) if 0 < b-a <= max_gap_ns)
    findings = []
    for name, values in (('recording_gap', gaps), ('invalid_observations', invalid_observations), ('stale_observations', stale), ('disconnected_observations', disconnected)):
        if values:
            findings.append({'kind':name, 'severity':'warning', 'observed_behavior':name.replace('_',' '),
                'count':len(values), 'source_hashes':list(evidence.source_hashes),
                'intervals_ns':[[str(a),str(b)] for a,b in gaps] if name=='recording_gap' else [],
                'record_indices':[r.get('record_index') for r in values if isinstance(r,dict)],
                'source_references':[{'source_sha256':r.get('source_sha256'), 'record_index':r.get('record_index'),
                    'field':r.get('field')} for r in values if isinstance(r,dict)],
                'next_check':'Inspect recording coverage and source device status before interpreting control metrics'})
    if invalid or nonadvancing:
        findings.append({'kind':'invalid_cycle_timestamps','severity':'warning','count':invalid+nonadvancing,
                         'next_check':'Inspect source clock domain and importer profile'})
    unavailable = ['wall_clock_invalid_or_unknown'] if unknown_clock else []
    explicit_connections = [r for r in observations if type(r.get('connected')) is bool]
    known_connections = [r for r in connection_observations
                         if r.get('validity') == 'valid' and r.get('type') == 'boolean'
                         and type(r.get('value')) is bool]
    if not (known_connections or explicit_connections or any(
            r.get('sensor_availability') in ('connected','disconnected') for r in samples)):
        unavailable.append('sensor_connection_status_not_recorded')
    if any(r.get('sensor_availability') == 'unknown' for r in samples):
        unavailable.append('sample_sensor_connection_status_unknown')
    if not any(type(r.get('fresh')) is bool for r in observations):
        unavailable.append('sensor_acquisition_freshness_not_recorded')
    metrics = [metric('recording_span_seconds',duration,'seconds',sample_count=len(cycles)),
               metric('valid_cycle_coverage',valid_duration/duration if duration else None,'fraction'),
               metric('gap_count',len(gaps),'count'), metric('invalid_observation_count',len(invalid_observations),'count'),
               metric('unknown_or_invalid_epoch_cycles',unknown_clock,'count')]
    return result('finding' if findings else 'evaluated_no_finding', metrics=metrics,
                  coverage={'cycle_count':len(cycles),'valid_seconds':valid_duration,'span_seconds':duration,
                            'wall_clock_valid_cycles':len(cycles)-unknown_clock,
                            'known_connection_observations':len(known_connections),
                            'disconnected_source_count':len(disconnected)}, findings=findings, unavailable=unavailable)


def data_quality_analyzer(profiles):
    return Analyzer(Declaration('data-quality','2',tuple(profiles), eligible_windows='all recorded cycles'), data_quality)
