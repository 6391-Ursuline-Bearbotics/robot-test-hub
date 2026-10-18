"""Read-only reconstruction of exact saved automatic-analyzer evidence.

The private archive is trusted to its account owner, not to concurrent hostile
filesystem replacement. Ordinary changes during capture are detected. SQLite
may create/use its WAL coordination sidecars; SQL and evidence bytes are read-only. Exporters
must rehash each original while copying after this snapshot has closed.
"""
from contextlib import ExitStack, closing
from dataclasses import asdict
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile

from .analysis import Evidence, VERSION as FRAMEWORK_VERSION, canonical, identity, OUTCOMES
from . import reports
from .wpilog import PROFILE, EXTRACTOR_VERSION, MAPPING_REVISION
from .swerve import ROBOT_MAPPING_VERSION

MAX_ORIGINALS = 16
MAX_ORIGINAL_BYTES = 2 * 1024**3
MAX_TOTAL_ORIGINAL_BYTES = 8 * 1024**3
MAX_DATASET_BYTES = 2 * 1024**3
MAX_TOTAL_DATASET_BYTES = 8 * 1024**3
MAX_INPUT_BYTES = 64 * 1024**2
MAX_METADATA_BYTES = 64 * 1024**2
MAX_MANIFEST_BYTES = 4 * 1024**2
MAX_LINE_BYTES = 1024**2
MAX_VM_STEPS = 5000000
# Selector support is separate from a candidate evaluator/presentation version.
SUPPORTED_SELECTOR_VERSIONS = ('automatic-reports-5','automatic-reports-6','automatic-reports-7')
HASH = re.compile(r'[0-9a-f]{64}\Z')
REFERENCE = re.compile(r'[A-Za-z0-9_-]{1,128}\Z')


class EvidenceError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _fail(code='regression_evidence_integrity'):
    raise EvidenceError(code)


def _pairs(items):
    out = {}
    for key, value in items:
        if key in out: _fail()
        out[key] = value
    return out


def _json(raw):
    if not isinstance(raw, (str, bytes)): _fail()
    if len(raw if isinstance(raw, bytes) else raw.encode('utf-8')) > MAX_METADATA_BYTES:
        _fail('regression_evidence_resource_limit')
    def integer(value):
        if len(value.lstrip('-')) > 30: _fail()
        return int(value)
    def constant(value): _fail()
    value = json.loads(raw, object_pairs_hook=_pairs, parse_int=integer, parse_constant=constant)
    # Reject exponent overflow as well as literal NaN/Infinity.
    canonical(value)
    return value


def _hash(value):
    return type(value) is str and HASH.fullmatch(value) is not None


def _chain(path):
    path = Path(os.path.abspath(path))
    for part in (path, *path.parents):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            _fail()
    return path


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_mode, info.st_nlink)


def _open(root, relative, expected, limit, stack):
    # Only canonical importer-owned locations are supplied by the caller.
    if not _hash(expected): _fail()
    path = _chain(root / relative)
    if not path.is_relative_to(root): _fail()
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1: _fail()
    if info.st_size > limit: _fail('regression_evidence_resource_limit')
    stream = stack.enter_context(path.open('rb'))
    if _signature(os.fstat(stream.fileno())) != _signature(info): _fail()
    digest = hashlib.sha256()
    consumed = 0
    while True:
        chunk = stream.read(1024**2)
        if not chunk: break
        consumed += len(chunk)
        if consumed > limit: _fail('regression_evidence_resource_limit')
        digest.update(chunk)
    if digest.hexdigest() != expected: _fail()
    stream.seek(0)
    return path, stream, info


def _unchanged(opened):
    for path, stream, info in opened:
        if (_signature(path.lstat()) != _signature(info) or
                _signature(os.fstat(stream.fileno())) != _signature(info)):
            _fail()


def _dataset(stream):
    while True:
        line = stream.readline(MAX_LINE_BYTES + 1)
        if not line: return
        if len(line) > MAX_LINE_BYTES: _fail('regression_evidence_resource_limit')
        row = _json(line)
        if type(row) is not dict: _fail()
        yield row


def _bounded_row(db, table, columns, where, args, *, limit=MAX_METADATA_BYTES):
    # All identifiers are internal constants. Avoid allocating oversized TEXT in Python.
    measure = '+'.join('COALESCE(length(CAST('+column+' AS BLOB)),0)' for column in columns)
    row = db.execute('SELECT '+measure+', CASE WHEN '+measure+'<=? THEN json_array('+','.join(columns)+') END FROM '+table+' WHERE '+where,
                     (limit, *args)).fetchone()
    if row is None: _fail('regression_evidence_unavailable')
    if row[0] > limit: _fail('regression_evidence_resource_limit')
    return dict(zip(columns, _json(row[1])))


def _saved(db, review_id, revision):
    record = _bounded_row(db, 'review_records', ('payload_json',),
        "kind='finding_review' AND record_id=? AND revision=?", (review_id, revision), limit=1024**2)
    review = _json(record['payload_json'])
    if (type(review) is not dict or review.get('id') != review_id or
            type(review.get('revision')) is not int or review['revision'] != revision or
            review.get('action_scope') != 'review_record_only' or not _hash(review.get('job_id'))): _fail()
    columns = ('job_id','framework_version','analyzer_id','analyzer_version','declaration_json',
               'input_json','configuration_json','outcome','result_json')
    saved = _bounded_row(db, 'analyzer_jobs', columns, 'job_id=?', (review['job_id'],))
    if saved['framework_version'] != FRAMEWORK_VERSION: _fail('regression_evidence_unsupported')
    declaration, provenance, config, job = (_json(saved[k]) for k in
        ('declaration_json','input_json','configuration_json','result_json'))
    if type(provenance) is not dict or type(provenance.get('context')) is not dict: _fail()
    version = provenance['context'].get('pipeline_version')
    selection = provenance['context'].get('row_selection')
    try: reports.selector_policy(version, selection)
    except (ValueError, TypeError): _fail('regression_evidence_unsupported')
    supported = {a.declaration.analyzer_id: json.loads(canonical(asdict(a.declaration)))
                 for a in reports.analyzers(pipeline_version=version)}
    if declaration != supported.get(saved['analyzer_id']): _fail('regression_evidence_unsupported')
    if saved['analyzer_version'] != declaration['version'] or saved['outcome'] not in OUTCOMES: _fail()
    if (type(job) is not dict or job.get('job_id') != saved['job_id'] or
            job.get('analyzer_id') != saved['analyzer_id'] or job.get('analyzer_version') != saved['analyzer_version'] or
            job.get('outcome') != saved['outcome'] or job.get('declaration') != declaration or
            job.get('provenance') != provenance or job.get('configuration') != config or
            identity([FRAMEWORK_VERSION, declaration, provenance, config]) != saved['job_id']): _fail()
    index = review.get('finding_index')
    if (type(index) is not int or not isinstance(job.get('findings'), list) or
            not 0 <= index < len(job['findings']) or
            review.get('finding_identity') != identity([job['job_id'], index, job['findings'][index]])): _fail()
    if type(provenance) is not dict or type(provenance.get('context')) is not dict: _fail()
    return review, job, provenance


def _capture(root, db, stack, review_id, revision):
    review, job, provenance = _saved(db, review_id, revision)
    return _reconstruct(root, db, stack, review, job, provenance)


def _reconstruct(root, db, stack, review, job, provenance):
    context = copy.deepcopy(provenance['context'])
    if (provenance.get('profile') != PROFILE or provenance.get('mapping_revision') != MAPPING_REVISION or
            provenance.get('importer_version') != EXTRACTOR_VERSION or
            context.get('pipeline_version') not in SUPPORTED_SELECTOR_VERSIONS or
            context.get('swerve_mapping_version') != ROBOT_MAPPING_VERSION): _fail('regression_evidence_unsupported')
    try: reports.selector_policy(context.get('pipeline_version'), context.get('row_selection'))
    except (ValueError, TypeError): _fail('regression_evidence_unsupported')
    max_rows = context.get('max_rows')
    if type(max_rows) is not int or not 2 <= max_rows <= reports.MAX_ROWS: _fail('regression_evidence_resource_limit')
    ids = context.get('import_job_ids')
    if type(ids) is not list or not ids or any(not _hash(v) for v in ids) or len(set(ids)) != len(ids): _fail()
    if len(ids) > MAX_ORIGINALS: _fail('regression_evidence_resource_limit')
    plan = context.get('analysis_plan')
    if plan is not None and type(plan) is not dict: _fail()
    columns = ('id','artifact_sha256','extractor_version','profile','mapping_revision','state',
               'dataset_path','dataset_sha256','manifest_path','manifest_sha256')
    jobs, manifests, datasets, opened, originals = [], {}, {}, [], {}
    original_bytes = dataset_bytes = 0
    for pinned in ids:
        source = _bounded_row(db, 'import_jobs', columns, 'id=?', (pinned,), limit=16384)
        if source['state'] not in ('succeeded','succeeded_with_unsupported'): _fail()
        if (source['profile'] != PROFILE or source['extractor_version'] != EXTRACTOR_VERSION or
                source['mapping_revision'] != MAPPING_REVISION): _fail('regression_evidence_unsupported')
        digest = source['artifact_sha256']
        if not _hash(digest) or identity([digest,EXTRACTOR_VERSION,PROFILE,MAPPING_REVISION]) != pinned: _fail()
        artifact = _bounded_row(db, 'import_artifacts', ('sha256','size_bytes','relative_path','source_type'), 'sha256=?', (digest,), limit=16384)
        raw_path = f'raw/{digest[:2]}/{digest}.wpilog'
        if artifact['relative_path'] != raw_path or type(artifact['size_bytes']) is not int or artifact['size_bytes'] < 0: _fail()
        if digest not in originals:
            original = _open(root, raw_path, digest, min(MAX_ORIGINAL_BYTES, MAX_TOTAL_ORIGINAL_BYTES-original_bytes), stack)
            opened.append(original)
            if original[2].st_size != artifact['size_bytes']: _fail()
            original_bytes += original[2].st_size
            if original_bytes > MAX_TOTAL_ORIGINAL_BYTES: _fail('regression_evidence_resource_limit')
            originals[digest] = {'path':original[0], 'sha256':digest, 'size_bytes':original[2].st_size}
        dataset_path, manifest_path = f'derived/{pinned}/signals.jsonl', f'derived/{pinned}/manifest.json'
        if source['dataset_path'] != dataset_path or source['manifest_path'] != manifest_path: _fail()
        dataset = _open(root, dataset_path, source['dataset_sha256'], min(MAX_DATASET_BYTES, MAX_TOTAL_DATASET_BYTES-dataset_bytes), stack)
        opened.append(dataset); dataset_bytes += dataset[2].st_size
        if dataset_bytes > MAX_TOTAL_DATASET_BYTES: _fail('regression_evidence_resource_limit')
        manifest_file = _open(root, manifest_path, source['manifest_sha256'], MAX_MANIFEST_BYTES, stack)
        opened.append(manifest_file)
        manifest = _json(manifest_file[1].read(MAX_MANIFEST_BYTES+1))
        expected = {'schema_version':1,'job_id':pinned,'source_sha256':digest,'source_size_bytes':artifact['size_bytes'],
            'raw_path':raw_path,'source_type':artifact['source_type'],'extractor_version':EXTRACTOR_VERSION,
            'profile':PROFILE,'mapping_revision':MAPPING_REVISION,'dataset_path':dataset_path,'dataset_sha256':source['dataset_sha256']}
        if type(manifest) is not dict or any(manifest.get(k) != v for k,v in expected.items()): _fail()
        manifests[pinned] = manifest; datasets[pinned] = dataset[1]; jobs.append(source)
    jobs.sort(key=lambda j:(int(manifests[j['id']].get('first_cycle_timestamp_ns') or 0),j['id']))
    for key in reports.DERIVED_CONTEXT_FIELDS: context.pop(key, None)
    source_types = {manifests[j['id']]['source_type'] for j in jobs}
    if not source_types <= {'SYNTHETIC','MANUAL_LOCAL','VERIFIED_TRANSFER','TRANSFER_CHECKSUM_MISMATCH'}: _fail()
    mode = context.get('runtime_mode')
    source_type = ('synthetic' if 'SYNTHETIC' in source_types else
        'simulation' if mode == 'SIM' and source_types <= {'MANUAL_LOCAL','VERIFIED_TRANSFER'} else
        'real' if mode == 'REAL' else 'historical')
    context['mapping_revision'] = MAPPING_REVISION
    context['dataset_hashes'] = sorted(j['dataset_sha256'] for j in jobs)
    context['original_import_source_types'] = sorted(source_types)
    context['swerve_freshness_policy'] = ('ideal_simulation_cycle' if plan and
        plan['ideal_simulation_cycle_policy'] and source_type == 'simulation' else 'physical_acquisition_time_unknown')
    run = {key:context[key] for key in ('start_monotonic_ns','end_monotonic_ns','runtime_mode')}
    budget = MAX_INPUT_BYTES-len(canonical(context).encode('utf-8'))-4096
    if budget <= 0: _fail('regression_evidence_resource_limit')
    rows, derived = reports.select_rows(run, jobs, manifests, (_dataset(datasets[j['id']]) for j in jobs),
        max_rows=max_rows, plan=plan, max_bytes=budget,
        pipeline_version=context['pipeline_version'], row_selection=context['row_selection'])
    context.update(derived)
    evidence = Evidence(PROFILE, tuple(sorted(originals)), tuple(rows), context, MAPPING_REVISION, EXTRACTOR_VERSION, source_type)
    normalized = asdict(evidence)
    if len(canonical(normalized).encode('utf-8')) > MAX_INPUT_BYTES: _fail('regression_evidence_resource_limit')
    reference = {k:v for k,v in normalized.items() if k != 'rows'}
    reference['rows_digest'] = identity(evidence.rows)
    if json.loads(canonical(reference)) != provenance: _fail()
    expected_configuration = plan['swerve_configuration'] if job['analyzer_id'] == 'swerve-tracking' and plan else {}
    if job['analyzer_id'] == 'recorded-steering-observation' and plan:
        expected_configuration=plan.get('steering_configuration',{})
    if job['configuration'] != expected_configuration: _fail()
    _unchanged(opened)
    return {'review':review, 'job':job, 'framework_version':FRAMEWORK_VERSION,
            'evidence':normalized, 'originals':[originals[key] for key in sorted(originals)],
            'imports':[{'job_id':j['id'], **{k:j[k] for k in ('artifact_sha256','profile','extractor_version',
                'mapping_revision','dataset_sha256','manifest_sha256')}, 'source_type':manifests[j['id']]['source_type']}
                for j in sorted(jobs,key=lambda j:j['id'])]}


def capture(root, *, review_id, review_revision):
    """Reconstruct an exact supported review's input without writing the archive."""
    if (type(review_id) is not str or REFERENCE.fullmatch(review_id) is None or
            type(review_revision) is not int or not 1 <= review_revision <= 1000000):
        _fail('invalid_regression_reference')
    try:
        root = _chain(Path(root)).resolve(strict=True)
        catalog = _chain(root / 'catalog.sqlite3')
        if not root.is_dir() or not stat.S_ISREG(catalog.lstat().st_mode) or catalog.lstat().st_nlink != 1: _fail()
        with ExitStack() as stack, closing(sqlite3.connect(catalog.as_uri()+'?mode=ro', uri=True, timeout=1)) as db:
            db.execute('PRAGMA query_only=ON')
            if MAX_VM_STEPS <= 0: _fail('regression_evidence_resource_limit')
            steps = 0
            def progress():
                nonlocal steps
                steps += 1000
                return steps > MAX_VM_STEPS
            db.set_progress_handler(progress, 1000)
            db.execute('BEGIN')
            return _capture(root, db, stack, review_id, review_revision)
    except EvidenceError:
        raise
    except reports.SelectionLimit:
        _fail('regression_evidence_resource_limit')
    except sqlite3.OperationalError as exc:
        if getattr(exc, 'sqlite_errorcode', None) == sqlite3.SQLITE_INTERRUPT or str(exc) == 'interrupted':
            _fail('regression_evidence_resource_limit')
        _fail('regression_evidence_unavailable')
    except (OSError, sqlite3.Error):
        _fail('regression_evidence_unavailable')
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, RecursionError, OverflowError):
        _fail()


def verify_originals(originals, imports, evidence, job):
    """Reimport preserved originals locally and prove their exact saved row relation.

    The caller has already verified its case envelope. This independently checks
    every pin again; only a fresh owned temporary archive is opened for writing.
    No current run catalog, plans, user archive or network is consulted.
    """
    from .storage import open_catalog
    from .importer import Importer
    fields = {'job_id','artifact_sha256','profile','extractor_version','mapping_revision',
              'dataset_sha256','manifest_sha256','source_type'}
    try:
        if (type(originals) is not list or type(imports) is not list or not originals or not imports or
                len(originals)>MAX_ORIGINALS or len(imports)>MAX_ORIGINALS): _fail('regression_evidence_resource_limit')
        if type(evidence) is not dict or type(job) is not dict or type(job.get('provenance')) is not dict: _fail()
        if len(canonical(evidence).encode('utf-8')) > MAX_INPUT_BYTES:
            _fail('regression_evidence_resource_limit')
        if identity([FRAMEWORK_VERSION,job.get('declaration'),job['provenance'],job.get('configuration')]) != job.get('job_id'): _fail()
        supplied = {k:v for k,v in evidence.items() if k != 'rows'}
        supplied['rows_digest'] = identity(evidence.get('rows'))
        if json.loads(canonical(supplied)) != job['provenance']: _fail()
        by_sha = {};total = 0
        with ExitStack() as inputs:
            for original in originals:
                if type(original) is not dict or set(original) != {'path','sha256','size_bytes'}: _fail()
                digest = original['sha256']; path = Path(original['path'])
                if not _hash(digest) or digest in by_sha or not path.is_absolute(): _fail()
                opened = _open(path.parent,path.name,digest,min(MAX_ORIGINAL_BYTES, MAX_TOTAL_ORIGINAL_BYTES-total),inputs)
                if type(original['size_bytes']) is not int or opened[2].st_size != original['size_bytes']: _fail()
                total += opened[2].st_size
                if total > MAX_TOTAL_ORIGINAL_BYTES: _fail('regression_evidence_resource_limit')
                by_sha[digest] = opened
            seen = set()
            for pin in imports:
                if type(pin) is not dict or set(pin) != fields: _fail()
                if (pin['profile'] != PROFILE or pin['extractor_version'] != EXTRACTOR_VERSION or
                        pin['mapping_revision'] != MAPPING_REVISION): _fail('regression_evidence_unsupported')
                if (any(not _hash(pin[k]) for k in ('job_id','artifact_sha256','dataset_sha256','manifest_sha256')) or
                        pin['artifact_sha256'] not in by_sha or pin['job_id'] in seen or
                        pin['source_type'] not in ('SYNTHETIC','MANUAL_LOCAL','VERIFIED_TRANSFER','TRANSFER_CHECKSUM_MISMATCH')): _fail()
                seen.add(pin['job_id'])
            if {p['artifact_sha256'] for p in imports} != set(by_sha): _fail()
            with tempfile.TemporaryDirectory(prefix='hub-regression-proof-') as temporary:
                root = Path(temporary).resolve()
                with closing(open_catalog(root)) as db:
                    importer = Importer(root,db)
                    for pin in imports:
                        decoded = importer.import_file(by_sha[pin['artifact_sha256']][0], profile=pin['profile'],
                            expected_sha256=pin['artifact_sha256'], source_type=pin['source_type'], mapping_revision=pin['mapping_revision'])
                        if (decoded['state'] not in ('succeeded','succeeded_with_unsupported') or
                                decoded['id'] != pin['job_id'] or
                                any(decoded[k] != pin[k] for k in ('artifact_sha256','profile','extractor_version',
                                    'mapping_revision','dataset_sha256','manifest_sha256'))): _fail()
                    with ExitStack() as selected:
                        reconstructed = _reconstruct(root,db,selected,None,job,job['provenance'])
                    if (json.loads(canonical(reconstructed['evidence'])) != json.loads(canonical(evidence)) or
                            sorted(reconstructed['imports'],key=lambda p:p['job_id']) != sorted(imports,key=lambda p:p['job_id'])): _fail()
            _unchanged(by_sha.values())
        return {'verified':True,'original_count':len(originals),'import_count':len(imports),
                'rows_digest':job['provenance']['rows_digest']}
    except EvidenceError:
        raise
    except reports.SelectionLimit:
        _fail('regression_evidence_resource_limit')
    except (OSError,sqlite3.Error):
        _fail('regression_evidence_unavailable')
    except (ValueError,TypeError,KeyError,IndexError,AttributeError,RecursionError,OverflowError):
        _fail()
