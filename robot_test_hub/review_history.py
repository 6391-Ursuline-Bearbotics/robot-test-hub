"""Read-only bounded human review history and exact repair references.

Pagination assumes the supported append-only review_records contract. Reading
PRAGMA schema_version invalidates cursors after schema changes and VACUUM:
https://www.sqlite.org/pragma.html#pragma_schema_version
ROWIDs are not asserted stable across manual database rewrites or file restores.
"""
from contextlib import closing
import base64
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlencode

from .review import KINDS

MAX_RECORD_BYTES = 1024 * 1024
MAX_JOB_BYTES = 64 * 1024 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
SUMMARY_KEYS = ('id', 'revision', 'label', 'reviewer', 'rationale', 'disposition',
                'robot_id', 'location', 'component_id', 'kind', 'start_utc_ns',
                'end_utc_ns', 'effective_utc_ns', 'job_id', 'validation_job_id')
PREFIX = '/api/v1/review/'


class HistoryError(ValueError):
    def __init__(self, status, code):
        super().__init__(code)
        self.status = status
        self.code = code


def _id(value):
    if type(value) is not str or not re.fullmatch('[A-Za-z0-9_-]{1,128}', value):
        raise ValueError('invalid_review_identity')
    return value


def _int(value, low=1, high=(1 << 63) - 1):
    if type(value) is not str or not re.fullmatch('0|[1-9][0-9]{0,18}', value):
        raise ValueError('invalid_review_query')
    number = int(value)
    if not low <= number <= high:
        raise ValueError('invalid_review_query')
    return number


def _json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('malformed_review_record')
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=unique,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError, RecursionError):
        raise ValueError('malformed_review_record') from None


def _bounded(value):
    if len(json.dumps(value, allow_nan=False, separators=(',', ':')).encode()) > MAX_RESPONSE_BYTES:
        raise ValueError('oversized_review_response')
    return value


def _url(route, kind, identity, revision):
    return PREFIX + route + '?' + urlencode(dict(kind=kind, id=identity, revision=revision))


def _pin(kind, identity, revision, relation='record', state='pinned'):
    return dict(relation=relation, state=state, kind=kind, id=identity, revision=revision,
                url=_url('record', kind, identity, revision))


def _load(db, kind, identity, revision):
    row = db.execute('SELECT length(CAST(payload_json AS BLOB)), CASE WHEN '
        'length(CAST(payload_json AS BLOB))<=? THEN payload_json END '
        'FROM review_records WHERE kind=? AND record_id=? AND revision=?',
        (MAX_RECORD_BYTES, kind, identity, revision)).fetchone()
    if row is None:
        raise KeyError('review_record_not_found')
    if row[1] is None or not 0 < row[0] <= MAX_RECORD_BYTES:
        raise ValueError('oversized_review_record')
    value = _json(row[1])
    if (not isinstance(value, dict) or value.get('id') != identity
            or type(value.get('revision')) is not int or value['revision'] != revision):
        raise ValueError('malformed_review_record')
    return value


def _fence(db, highwater):
    if highwater == 0:
        return hashlib.sha256(b'empty').hexdigest()
    row = db.execute('SELECT kind,record_id,revision,created_utc_ns,'
        'CASE WHEN length(CAST(payload_json AS BLOB))<=? THEN payload_json END '
        'FROM review_records WHERE rowid=?', (MAX_RECORD_BYTES, highwater)).fetchone()
    if row is None:
        raise ValueError('review_catalog_changed')
    if row[4] is None:
        raise ValueError('oversized_review_record')
    return hashlib.sha256(json.dumps(list(row), ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()


def _encode(state):
    raw = json.dumps(state, sort_keys=True, separators=(',', ':')).encode()
    return base64.urlsafe_b64encode(raw + hashlib.sha256(raw).digest()).decode().rstrip('=')


def _decode(token, kind, identity):
    try:
        if type(token) is not str or not re.fullmatch('[A-Za-z0-9_-]{1,2048}', token):
            raise ValueError()
        raw = base64.b64decode(token + '=' * (-len(token) % 4), altchars=b'-_', validate=True)
        payload, digest = raw[:-32], raw[-32:]
        if hashlib.sha256(payload).digest() != digest:
            raise ValueError()
        value = _json(payload)
        if (type(value) is not dict or set(value) != {'version', 'kind', 'record_id', 'highwater', 'schema', 'fence', 'boundary'}
                or type(value['version']) is not int or value['version'] != 1
                or value['kind'] != kind or value['record_id'] != identity
                or any(type(value[key]) is not int for key in ('highwater', 'schema', 'boundary'))
                or not 1 <= value['boundary'] <= value['highwater'] < 1 << 63
                or not 0 <= value['schema'] < 1 << 32
                or type(value['fence']) is not str or not re.fullmatch('[a-f0-9]{64}', value['fence'])
                or _encode(value) != token):
            raise ValueError()
        return value
    except (ValueError, TypeError, KeyError, OverflowError):
        raise ValueError('invalid_review_cursor') from None


def history(db, query):
    if set(query) - {'kind', 'record_id', 'limit', 'cursor'} or 'kind' not in query:
        raise ValueError('invalid_review_query')
    kind = query['kind']
    if kind not in KINDS:
        raise ValueError('invalid_review_kind')
    identity = _id(query['record_id']) if 'record_id' in query else None
    limit = _int(query.get('limit', '20'), 1, 50)
    highwater = db.execute('SELECT COALESCE(MAX(rowid),0) FROM review_records').fetchone()[0]
    schema = db.execute('PRAGMA schema_version').fetchone()[0]
    if 'cursor' in query:
        state = _decode(query['cursor'], kind, identity)
        if (state['schema'] != schema or state['highwater'] > highwater
                or state['fence'] != _fence(db, state['highwater'])):
            raise ValueError('review_catalog_changed')
    else:
        state = dict(version=1, kind=kind, record_id=identity, highwater=highwater,
                     schema=schema, fence=_fence(db, highwater), boundary=highwater + 1)
    where = 'r.kind=? AND r.rowid<=?'
    args = [kind, state['highwater']]
    if identity is not None:
        where += ' AND r.record_id=?'
        args.append(identity)
    else:
        where += (' AND r.revision=(SELECT MAX(x.revision) FROM review_records x '
                  'WHERE x.kind=r.kind AND x.record_id=r.record_id AND x.rowid<=?)')
        args.append(state['highwater'])
    if 'cursor' in query and db.execute('SELECT 1 FROM review_records r WHERE ' + where + ' AND r.rowid=?',
                                        (*args, state['boundary'])).fetchone() is None:
        raise ValueError('invalid_review_cursor')
    # Large nested bundle/job evidence never crosses into Python for a list.
    # JSON validity and encoded projection size are checked in SQL first.
    projected = "json_object('display_projection',json('true')," + ','.join(
        "'" + key + "',CASE WHEN json_type(r.payload_json,'$." + key +
        "') IN ('text','integer','real','null','true','false') THEN json_extract(r.payload_json,'$." +
        key + "') END" for key in SUMMARY_KEYS) + ')'
    unique = ('NOT EXISTS(SELECT 1 FROM json_tree(r.payload_json) '
              'WHERE key IS NOT NULL GROUP BY parent,key HAVING COUNT(*)>1)')
    identity_valid = ("json_type(r.payload_json,'$.id')='text' AND "
                      "json_type(r.payload_json,'$.revision')='integer'")
    guarded = ('CASE WHEN length(CAST(r.payload_json AS BLOB))<=' + str(MAX_RECORD_BYTES) +
               ' THEN CASE WHEN json_valid(r.payload_json) THEN CASE WHEN ' + unique +
               ' AND ' + identity_valid + ' THEN ' + projected + ' END END END')
    sql = ('WITH page AS (SELECT r.rowid AS position,r.record_id,r.revision,r.created_utc_ns,'
           'length(CAST(r.payload_json AS BLOB)) AS original_size,' + guarded +
           ' AS summary FROM review_records r WHERE ' + where +
           ' AND r.rowid<? ORDER BY r.rowid DESC LIMIT ?) '
           'SELECT position,record_id,revision,created_utc_ns,original_size,'
           'CASE WHEN length(CAST(summary AS BLOB))<=32768 THEN summary END '
           'FROM page ORDER BY position DESC')
    rows = db.execute(sql, (*args, state['boundary'], limit + 1)).fetchall()
    items = []
    for rowid, record_id, revision, created, original_size, raw in rows[:limit]:
        _id(record_id)
        if type(revision) is not int or revision < 1 or not re.fullmatch('[0-9]{1,19}', str(created)):
            raise ValueError('malformed_review_record')
        if not 0 < original_size <= MAX_RECORD_BYTES or raw is None:
            raise ValueError('oversized_or_malformed_review_record')
        value = _json(raw)
        if (not isinstance(value, dict) or value.get('id') != record_id
                or type(value.get('revision')) is not int or value['revision'] != revision):
            raise ValueError('malformed_review_record')
        summary = {key: value[key] for key in SUMMARY_KEYS if key in value
                   and (value[key] is None or type(value[key]) in (str, int, bool, float))}
        summary['display_projection'] = True
        if len(json.dumps(summary).encode()) > 32768:
            raise ValueError('oversized_review_metadata')
        items.append(dict(kind=kind, id=record_id, revision=revision, created_utc_ns=str(created),
            record=summary, record_url=_url('record', kind, record_id, revision),
            chain_url=_url('chain', kind, record_id, revision) if kind in ('finding_review', 'regression_bundle') else None))
    cursor = _encode({**state, 'boundary': rows[limit - 1][0]}) if len(rows) > limit else None
    return _bounded(dict(schema_version=1, kind=kind, record_id=identity, records=items,
        next_cursor=cursor, snapshot=dict(highwater=state['highwater'], schema_version=schema), read_only=True))


def _job(db, identity, relation):
    _id(identity)
    node = dict(relation=relation, state='missing', job_id=identity)
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='analyzer_jobs'").fetchone() is None:
        return node
    row = db.execute('SELECT length(CAST(result_json AS BLOB)),CASE WHEN '
                     'length(CAST(result_json AS BLOB))<=? THEN json_valid(result_json) END '
                     'FROM analyzer_jobs WHERE job_id=?', (MAX_JOB_BYTES, identity)).fetchone()
    if row is None:
        return node
    if row[1] != 1 or not 0 < row[0] <= MAX_JOB_BYTES:
        return dict(node, state='unavailable', reason='invalid_or_oversized_analysis_job')
    duplicates = db.execute('SELECT 1 FROM analyzer_jobs j,json_tree(j.result_json) t '
        'WHERE j.job_id=? AND t.key IS NOT NULL GROUP BY t.parent,t.key HAVING COUNT(*)>1 LIMIT 1', (identity,)).fetchone()
    if duplicates:
        return dict(node, state='unavailable', reason='malformed_analysis_job')
    modules = db.execute("SELECT m.key,m.type,json_type(m.value,'$.evidence_trace'),json_array_length(m.value,'$.evidence_trace') "
        "FROM analyzer_jobs j,json_each(j.result_json,'$.coverage.modules') m WHERE j.job_id=? LIMIT 17", (identity,)).fetchall()
    findings = db.execute("SELECT f.key,f.type,json_array_length(f.value,'$.intervals_ns'),json_array_length(f.value,'$.source_references') "
        "FROM analyzer_jobs j,json_each(j.result_json,'$.findings') f WHERE j.job_id=? LIMIT 65", (identity,)).fetchall()
    if (len(modules) > 16 or len(findings) > 64 or any(m[1] != 'object' or type(m[0]) is not int for m in modules)
            or any(f[1] != 'object' or type(f[0]) is not int for f in findings)):
        return dict(node, state='unavailable', reason='unsupported_analysis_job_shape')
    paths = ['$.provenance.context.review_comparison_history', '$.provenance.context.maintenance_references',
             '$.provenance.context.approved_baseline.comparison_history', '$.provenance.context.approved_baseline.job_references']
    paths += [f'$.coverage.modules[{m[0]}].evidence_trace' for m in modules]
    for finding in findings:
        paths += [f'$.findings[{finding[0]}].intervals_ns', f'$.findings[{finding[0]}].source_references']
    expression = 'json_remove(result_json,' + ','.join('?' for _ in paths) + ')'
    sql = 'WITH p AS (SELECT ' + expression + ' AS value FROM analyzer_jobs WHERE job_id=?) '
    raw = db.execute(sql + 'SELECT CASE WHEN length(CAST(value AS BLOB))<=? THEN value END FROM p',
                     (*paths, identity, MAX_RECORD_BYTES)).fetchone()[0]
    if raw is None:
        return dict(node, state='unavailable', reason='oversized_analysis_metadata')
    value = _json(raw)
    if (not isinstance(value, dict) or value.get('job_id') != identity
            or not isinstance(value.get('coverage', {}), dict)
            or not isinstance(value.get('provenance', {}), dict)
            or not isinstance(value.get('provenance', {}).get('context', {}), dict)
            or not isinstance(value.get('findings', []), list)
            or not isinstance(value.get('coverage', {}).get('modules', []), list)):
        return dict(node, state='unavailable', reason='malformed_analysis_job')
    from .reports import public_check
    value = public_check(value, trace_limit=0)
    for module in modules:
        value['coverage']['modules'][module[0]].update(evidence_trace_total=module[3] or 0,
                                                     evidence_trace_truncated=bool(module[3]))
    for finding in findings:
        for key, total in (('intervals_ns', finding[2]), ('source_references', finding[3])):
            if total is not None:
                value['findings'][finding[0]].update({key: [], key+'_total': total, key+'_truncated': total > 0})
    links = []
    candidates = []
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='analysis_reports'").fetchone():
        candidates = db.execute("SELECT r.report_id,CASE WHEN json_valid(r.result_json) THEN json_extract(r.result_json,'$.run_id') END "
            "FROM analysis_reports r WHERE CASE WHEN length(CAST(r.result_json AS BLOB))<=? AND json_valid(r.result_json) "
            "THEN EXISTS(SELECT 1 FROM json_each(r.result_json,'$.checks') c WHERE json_extract(c.value,'$.job_id')=?) ELSE 0 END "
            "ORDER BY r.report_id LIMIT 21", (MAX_JOB_BYTES, identity)).fetchall()
        for report_id, run_id in candidates[:20]:
            _id(report_id); _id(run_id)
            links.append(dict(report_id=report_id, run_id=run_id,
                              url='/api/v1/runs/' + run_id + '/reports/' + report_id))
    return dict(node, state='pinned', job=value, report_links=links,
                report_state='pinned' if links else 'missing', report_links_truncated=len(candidates) > 20)


def _embedded(value, kind, relation):
    if not isinstance(value, dict):
        return dict(relation=relation, state='missing', kind=kind)
    identity = _id(value.get('id'))
    revision = value.get('revision')
    if type(revision) is not int or revision < 1:
        return dict(relation=relation, state='unpinned', kind=kind, id=identity)
    return dict(_pin(kind, identity, revision, relation, 'embedded'), record=value)


def _nodes(db, kind, identity, revision, value):
    nodes = [dict(_pin(kind, identity, revision, 'selected_record'), record=value)]
    review = value
    if kind == 'regression_bundle':
        review = value.get('finding_review')
        nodes.append(_embedded(review, 'finding_review', 'finding_review'))
        maintenance = value.get('maintenance')
        nodes.append(_embedded(maintenance, 'maintenance', 'maintenance'))
    elif kind == 'finding_review':
        maintenance_id = value.get('maintenance_id')
        nodes.append(dict(relation='maintenance', state='unpinned' if maintenance_id else 'missing',
                          kind='maintenance', **({'id': _id(maintenance_id)} if maintenance_id else {})))
    else:
        jobs = value.get('job_ids', [])
        if not isinstance(jobs, list) or len(jobs) > 100:
            raise ValueError('malformed_review_record')
        for job in jobs:
            nodes.append(dict(relation='baseline_job', state='pinned', job_id=_id(job)))
        return nodes
    if isinstance(review, dict):
        for field, relation in (('job_id', 'incident_job'), ('validation_job_id', 'validation_job')):
            job = review.get(field)
            if job:
                try:
                    node = _job(db, job, relation)
                except (ValueError, TypeError, AttributeError, sqlite3.Error):
                    node = dict(relation=relation, state='unavailable', reason='malformed_analysis_job')
                nodes.append(node)
            else:
                nodes.append(dict(relation=relation, state='missing'))
    return nodes


def detail(db, query, chain=False):
    if set(query) != {'kind', 'id', 'revision'} or query['kind'] not in KINDS:
        raise ValueError('invalid_review_query')
    kind, identity, revision = query['kind'], _id(query['id']), _int(query['revision'])
    if chain and kind not in ('finding_review', 'regression_bundle'):
        raise ValueError('unsupported_review_chain_kind')
    value = _load(db, kind, identity, revision)
    nodes = _nodes(db, kind, identity, revision, value)
    result = dict(schema_version=1, kind=kind, id=identity, revision=revision, read_only=True)
    if chain:
        result.update(nodes=nodes, limitations=['Historical references do not establish physical repair effectiveness.',
            'Unpinned references are not resolved to current records. Embedded bundle records preserve their saved content.'])
    else:
        result.update(record=value, links=[{key: item for key, item in node.items() if key not in ('record', 'job')}
                                          for node in nodes[1:]])
    return _bounded(result)


def _get(root, path, query):
    """No constructors/migrations, source files or writable SQLite connections."""
    if path not in (PREFIX+'history', PREFIX+'record', PREFIX+'chain'):
        raise KeyError('route_not_found')
    if not isinstance(query, dict) or any(type(k) is not str or type(v) is not str for k, v in query.items()):
        raise ValueError('invalid_review_query')
    database = (Path(root) / 'catalog.sqlite3').resolve()
    if not database.is_file():
        raise KeyError('review_catalog_unavailable')
    try:
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            if path == PREFIX+'history':
                return history(db, query)
            return detail(db, query, chain=path == PREFIX+'chain')
    except sqlite3.Error:
        raise ValueError('review_catalog_unavailable') from None


def get(root, path, query):
    """Public error contract contains no private paths, payloads or SQL text."""
    try:
        return _get(root, path, query)
    except KeyError as exc:
        if exc.args and exc.args[0] == 'review_record_not_found':
            raise HistoryError(404, 'review_record_not_found') from None
        raise HistoryError(503, 'review_history_unavailable') from None
    except (ValueError, TypeError, OverflowError, RecursionError) as exc:
        message = str(exc)
        if message == 'review_catalog_changed':
            raise HistoryError(409, 'review_history_changed') from None
        if message.startswith(('invalid_review_', 'unsupported_review_chain_kind')):
            raise HistoryError(400, 'invalid_review_history_request') from None
        raise HistoryError(503, 'review_history_unavailable') from None
    except (sqlite3.Error, OSError):
        raise HistoryError(503, 'review_history_unavailable') from None
