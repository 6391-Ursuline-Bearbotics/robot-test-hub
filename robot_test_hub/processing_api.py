"""Catalog-only per-recording flow; no original/source/media reads or migrations.

Pagination is live SHA/identity keyset membership, not an immutable archive
snapshot. Refresh from the first page to see inserts before a saved cursor.
Verification is a stored receipt, never a fresh disk or hardware health check.
Report metadata scans stop at 32 MiB aggregate stored JSON or 5 million SQLite
VM steps; core projections have a separate 50 million step budget. Optional
report limits preserve other stages with an explicit unavailable/null count.
A maintained SHA-to-report reverse index is still needed for season scale.
"""
from contextlib import closing
import base64
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlencode

from .wpilog import EXTRACTOR_VERSION, MAPPING_REVISION, PROFILE

PREFIX = '/api/v1/processing'
MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_REPORT_SCAN_BYTES = 32 * 1024 * 1024
SECTIONS = ('transfers', 'imports', 'runs', 'reports')
SUCCESS = ('succeeded', 'succeeded_with_unsupported')
IMPORT_STATES = (*SUCCESS, 'pending', 'running', 'unsupported', 'invalid', 'failed', 'checksum_mismatch')
ERROR_CODES = {'unsupported_profile', 'unsupported_schema', 'invalid_format', 'resource_limit',
               'import_io_or_integrity', 'extractor_failure', 'checksum_mismatch'}
SOURCES = {'SYNTHETIC', 'MANUAL_LOCAL', 'VERIFIED_TRANSFER', 'TRANSFER_CHECKSUM_MISMATCH'}
PARAMS = dict(extractor=EXTRACTOR_VERSION, mapping=MAPPING_REVISION, profile=PROFILE)


class ProcessingError(ValueError):
    def __init__(self, status, code):
        super().__init__(code)
        self.status, self.code = status, code


def _sha(value):
    if type(value) is not str or not re.fullmatch('[a-f0-9]{64}', value):
        raise ValueError('query')
    return value


def _id(value):
    if type(value) is not str or not re.fullmatch('[A-Za-z0-9_-]{1,128}', value):
        raise ValueError('evidence')
    return value


def _token(value, maximum=256):
    return value if type(value) is str and re.fullmatch('[A-Za-z0-9_.-]{1,'+str(maximum)+'}', value) else 'unavailable'


def _limit(query):
    text = query.get('limit', '20')
    if not re.fullmatch('[1-9][0-9]?', text) or not 1 <= int(text) <= 50:
        raise ValueError('query')
    return int(text)


def _cursor(scope, boundary):
    raw = json.dumps(dict(version=1, scope=scope, boundary=boundary), sort_keys=True, separators=(',', ':')).encode()
    return base64.urlsafe_b64encode(raw+hashlib.sha256(raw).digest()).decode().rstrip('=')


def _after(query, scope):
    text = query.get('cursor')
    if text is None:
        return ''
    try:
        if not re.fullmatch('[A-Za-z0-9_-]{1,2048}', text):
            raise ValueError()
        raw = base64.b64decode(text+'='*(-len(text)%4), altchars=b'-_', validate=True)
        body, checksum = raw[:-32], raw[-32:]
        value = json.loads(body)
        if (hashlib.sha256(body).digest() != checksum or type(value) is not dict
                or set(value) != {'version','scope','boundary'} or type(value['version']) is not int
                or value['version'] != 1 or value['scope'] != scope
                or type(value['boundary']) is not str or not value['boundary']
                or len(value['boundary']) > 128 or _cursor(scope, value['boundary']) != text):
            raise ValueError()
        if scope.startswith('list:'):
            _sha(value['boundary'])
        else:
            _id(value['boundary'])
        return value['boundary']
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise ValueError('query') from None


def _base(indexer, revision):
    state = indexer.get('state') if isinstance(indexer, dict) else None
    return dict(schema_version=1, catalog_revision=revision, read_only=True,
        hardware_qualified=False, overall_health='not_assessed', membership='live_sha_keyset',
        indexer=dict(state=state if state in ('starting','running','failed','stopped') else 'unknown',
                     error_code='indexer_failed' if state == 'failed' else None))


# Every JSON expansion stays inside SQLite; only bounded scalar projections
# cross the Python interface. Immutable report documents are hashed in chunks.
COMMON = """
WITH originals AS (
 SELECT sha256 AS sha FROM files UNION SELECT sha256 FROM import_artifacts
), fm AS (
 SELECT f.*,v.state AS verification_state,v.digest AS verified_digest,
 CASE WHEN length(CAST(m.metadata_json AS BLOB))<=32768 THEN
  CASE WHEN json_valid(m.metadata_json) THEN json_extract(m.metadata_json,'$.format') END END AS format,
 CASE WHEN length(CAST(m.metadata_json AS BLOB))<=32768 THEN
  CASE WHEN json_valid(m.metadata_json) THEN json_extract(m.metadata_json,'$.format_profile') END END AS profile
 FROM files f LEFT JOIN verification_jobs v ON v.file_id=f.id
 LEFT JOIN manifest_items m ON m.file_id=f.id AND m.snapshot_id=(SELECT MAX(x.snapshot_id) FROM manifest_items x WHERE x.file_id=f.id)
), profiles AS (
 SELECT sha256 AS sha,COALESCE(profile,'unspecified') AS profile FROM fm WHERE format='wpilog'
 UNION SELECT artifact_sha256,profile FROM import_jobs
 UNION SELECT a.sha256,:profile FROM import_artifacts a WHERE NOT EXISTS(SELECT 1 FROM import_jobs j WHERE j.artifact_sha256=a.sha256)
), current_jobs AS (
 SELECT j.* FROM import_jobs j WHERE j.extractor_version=:extractor AND j.mapping_revision=:mapping
 AND EXISTS(SELECT 1 FROM profiles p WHERE p.sha=j.artifact_sha256 AND p.profile=j.profile)
), current_doc AS (
 SELECT r.document,s.revision FROM run_catalog_state s JOIN run_catalog_revisions r ON r.id=s.revision WHERE s.id=1
), catalog_inputs AS (
 SELECT json_extract(i.value,'$[0]') AS job_id FROM current_doc c,json_each(c.document,'$.inputs') i
), run_refs AS (
 SELECT DISTINCT j.artifact_sha256 AS sha,json_extract(r.value,'$.run_id') AS run_id,
 CASE WHEN EXISTS(SELECT 1 FROM json_each(r.value,'$.segment_ids') s2 JOIN current_jobs j2 ON j2.id=s2.value
 WHERE j2.artifact_sha256=j.artifact_sha256 AND j2.state IN ('succeeded','succeeded_with_unsupported')) THEN 1 ELSE 0 END AS current_toolchain
 FROM current_doc c,json_each(c.document,'$.runs') r,json_each(r.value,'$.segment_ids') s
 JOIN import_jobs j ON j.id=s.value WHERE j.state IN ('succeeded','succeeded_with_unsupported')
), report_refs AS (
 SELECT DISTINCT h.value AS sha,a.report_id,
 json_extract(a.result_json,'$.run_id') AS run_id FROM (SELECT report_id,result_json FROM analysis_reports WHERE :reports_available=1) a,
 json_each(a.result_json,'$.checks') c,json_each(c.value,'$.provenance.source_hashes') h
), stats AS (
 SELECT o.sha,
 (SELECT COUNT(*) FROM fm f WHERE f.sha256=o.sha) AS transfer_count,
 (SELECT COUNT(*) FROM import_artifacts a WHERE a.sha256=o.sha) AS artifact_count,
 (SELECT COUNT(*) FROM fm f WHERE f.sha256=o.sha AND f.state='complete' AND typeof(f.size)='integer' AND f.size>=0 AND typeof(f.offset)='integer' AND f.offset=f.size AND f.verification_state='complete' AND f.verified_digest=f.sha256) AS verified_transfers,
 (SELECT COUNT(*) FROM fm f WHERE f.sha256=o.sha AND (f.verification_state IN ('failed','error') OR (f.verification_state='complete' AND f.verified_digest IS NOT f.sha256))) AS verification_failures,
 (SELECT COUNT(*) FROM fm f WHERE f.sha256=o.sha AND f.offset>0) AS downloaded,
 (SELECT COUNT(*) FROM fm f WHERE f.sha256=o.sha AND f.format='wpilog') AS wpilog_transfers,
 (SELECT COUNT(*) FROM fm f WHERE f.sha256=o.sha AND f.format IS NULL) AS unknown_formats,
 (SELECT COUNT(*) FROM profiles p WHERE p.sha=o.sha) AS expected_profiles,
 (SELECT COUNT(*) FROM import_jobs j WHERE j.artifact_sha256=o.sha) AS import_count,
 (SELECT COUNT(*) FROM current_jobs j WHERE j.artifact_sha256=o.sha) AS current_count,
 (SELECT COUNT(*) FROM current_jobs j WHERE j.artifact_sha256=o.sha AND j.state NOT IN ('pending','running','succeeded','succeeded_with_unsupported')) AS import_failures,
 (SELECT COUNT(*) FROM current_jobs j WHERE j.artifact_sha256=o.sha AND j.state IN ('succeeded','succeeded_with_unsupported')) AS successful_current,
 (SELECT COUNT(DISTINCT j.state) FROM current_jobs j WHERE j.artifact_sha256=o.sha) AS state_count,
 (SELECT MIN(j.state) FROM current_jobs j WHERE j.artifact_sha256=o.sha) AS one_state,
 (SELECT COUNT(*) FROM run_refs r WHERE r.sha=o.sha) AS run_count,
 (SELECT COUNT(*) FROM run_refs r WHERE r.sha=o.sha AND r.current_toolchain=1) AS current_run_count,
 (SELECT COUNT(*) FROM catalog_inputs c JOIN import_jobs j ON j.id=c.job_id WHERE j.artifact_sha256=o.sha AND j.state IN ('succeeded','succeeded_with_unsupported')) AS indexed_inputs,
 (SELECT COUNT(*) FROM report_refs r WHERE r.sha=o.sha) AS report_count,
 (SELECT COUNT(*) FROM import_artifacts a WHERE a.sha256=o.sha AND a.source_type='TRANSFER_CHECKSUM_MISMATCH') AS source_mismatch
 FROM originals o
), flow AS (
 SELECT *,CASE WHEN import_failures>0 OR verification_failures>0 OR unknown_formats>0 OR source_mismatch>0 THEN 1 ELSE 0 END AS attention FROM stats
)
"""


def _catalog(db):
    row = db.execute('SELECT s.revision,length(CAST(r.document AS BLOB)) FROM run_catalog_state s '
                     'LEFT JOIN run_catalog_revisions r ON r.id=s.revision WHERE s.id=1').fetchone()
    if row is not None:
        if not row[1] or row[1] > MAX_JSON_BYTES:
            raise ValueError('evidence')
        valid = db.execute("SELECT json_valid(document),CASE WHEN json_valid(document) THEN json_type(document,'$.runs') END,"
            "CASE WHEN json_valid(document) THEN json_type(document,'$.inputs') END,"
            "CASE WHEN json_valid(document) THEN json_extract(document,'$.revision') END FROM run_catalog_revisions WHERE id=?",(row[0],)).fetchone()
        if tuple(valid) != (1, 'array', 'array', row[0]):
            raise ValueError('evidence')
        _id(row[0])
        bad_run=db.execute("SELECT 1 FROM run_catalog_revisions d,json_each(d.document,'$.runs') r WHERE d.id=? AND CASE WHEN r.type='object' THEN json_type(r.value,'$.segment_ids') IS NOT 'array' OR json_type(r.value,'$.run_id') IS NOT 'text' OR length(json_extract(r.value,'$.run_id')) NOT BETWEEN 1 AND 128 OR json_extract(r.value,'$.run_id') GLOB '*[^A-Za-z0-9_-]*' ELSE 1 END LIMIT 1",(row[0],)).fetchone()
        bad_input=db.execute("SELECT 1 FROM run_catalog_revisions d,json_each(d.document,'$.inputs') i WHERE d.id=? AND CASE WHEN i.type='array' THEN json_array_length(i.value)!=2 OR json_type(i.value,'$[0]') IS NOT 'text' ELSE 1 END LIMIT 1",(row[0],)).fetchone()
        bad_member=db.execute("SELECT 1 FROM run_catalog_revisions d,json_each(d.document,'$.runs') r,json_each(r.value,'$.segment_ids') s WHERE d.id=? AND s.type!='text' LIMIT 1",(row[0],)).fetchone() if not bad_run else True
        if bad_run or bad_input or bad_member:
            raise ValueError('evidence')
    duplicates=db.execute("SELECT 1 FROM run_catalog_revisions d,json_tree(d.document) t WHERE d.id=? AND t.key IS NOT NULL GROUP BY t.parent,t.key HAVING COUNT(*)>1 LIMIT 1",(row[0],)).fetchone() if row else None
    if duplicates:
        raise ValueError('evidence')
    return row[0] if row else None


def _reports_status(db):
    total=db.execute('SELECT COALESCE(SUM(length(CAST(result_json AS BLOB))),0) FROM analysis_reports').fetchone()[0]
    if total>MAX_REPORT_SCAN_BYTES:
        return False,'report_metadata_scan_limit'
    if db.execute('SELECT 1 FROM analysis_reports WHERE length(CAST(result_json AS BLOB)) NOT BETWEEN 1 AND ? LIMIT 1',(MAX_JSON_BYTES,)).fetchone():
        return False,'report_metadata_unavailable'
    bad = db.execute("SELECT 1 FROM analysis_reports WHERE CASE WHEN json_valid(result_json) THEN "
        "json_type(result_json,'$.checks') IS NOT 'array' OR json_type(result_json,'$.run_id') IS NOT 'text' OR length(json_extract(result_json,'$.run_id')) NOT BETWEEN 1 AND 128 OR json_extract(result_json,'$.run_id') GLOB '*[^A-Za-z0-9_-]*' OR json_extract(result_json,'$.report_id') IS NOT report_id ELSE 1 END LIMIT 1").fetchone()
    if bad:
        return False,'report_metadata_unavailable'
    bad_check=db.execute("SELECT 1 FROM analysis_reports a,json_each(a.result_json,'$.checks') c WHERE CASE WHEN c.type='object' THEN json_type(c.value,'$.provenance') IS NOT 'object' OR json_type(c.value,'$.provenance.source_hashes') IS NOT 'array' ELSE 1 END LIMIT 1").fetchone()
    if bad_check:
        return False,'report_metadata_unavailable'
    bad_hash=db.execute("SELECT 1 FROM analysis_reports a,json_each(a.result_json,'$.checks') c,json_each(c.value,'$.provenance.source_hashes') h WHERE h.type!='text' OR length(h.value)!=64 OR h.value GLOB '*[^a-f0-9]*' LIMIT 1").fetchone()
    if bad_hash:
        return False,'report_metadata_unavailable'
    # Validate the objects that define report membership, without walking each
    # high-rate trace sample. Trace payloads are neither interpreted nor loaded.
    duplicates=db.execute("SELECT 1 FROM analysis_reports a,json_each(a.result_json) t GROUP BY a.report_id,t.key HAVING COUNT(*)>1 LIMIT 1").fetchone()
    if not duplicates:
        duplicates=db.execute("SELECT 1 FROM analysis_reports a,json_each(a.result_json,'$.checks') c,json_each(c.value) t GROUP BY a.report_id,c.key,t.key HAVING COUNT(*)>1 LIMIT 1").fetchone()
    if not duplicates:
        duplicates=db.execute("SELECT 1 FROM analysis_reports a,json_each(a.result_json,'$.checks') c,json_each(c.value,'$.provenance') t GROUP BY a.report_id,c.key,t.key HAVING COUNT(*)>1 LIMIT 1").fetchone()
    if duplicates:
        return False,'report_metadata_unavailable'
    return True,None


def _summary(db, row, reports_available=True, report_reason=None):
    sha = row['sha']
    if type(sha) is not str or not re.fullmatch('[a-f0-9]{64}',sha):
        raise ValueError('evidence')
    sources = [r[0] if r[0] in SOURCES else 'unavailable' for r in db.execute(
        "SELECT DISTINCT CASE WHEN source_type IN ('SYNTHETIC','MANUAL_LOCAL','VERIFIED_TRANSFER','TRANSFER_CHECKSUM_MISMATCH') THEN source_type ELSE 'unavailable' END FROM import_artifacts WHERE sha256=? "
        "UNION SELECT DISTINCT CASE WHEN source_type IN ('SYNTHETIC','MANUAL_LOCAL','VERIFIED_TRANSFER','TRANSFER_CHECKSUM_MISMATCH') THEN source_type ELSE 'unavailable' END FROM import_requests WHERE artifact_sha256=?", (sha,sha))]
    if not sources:
        sources = ['non_wpilog_transfer' if row['wpilog_transfers']==0 and row['unknown_formats']==0 else 'transfer_source_unqualified']
    applicable = row['expected_profiles'] > 0
    if not applicable:
        imports = 'unknown_format' if row['unknown_formats'] else 'not_applicable'
    elif not row['current_count']:
        imports = 'pending'
    elif row['state_count'] > 1 or row['current_count'] < row['expected_profiles']:
        imports = 'mixed'
    else:
        imports = row['one_state'] if row['one_state'] in IMPORT_STATES else 'failed'
    if row['run_count']:
        indexing = 'searchable' if row['current_run_count'] else 'searchable_previous_import'
    elif not applicable:
        indexing = 'not_applicable'
    elif row['indexed_inputs']:
        indexing = 'no_runs'
    else:
        indexing = 'not_indexed'
    reasons = [label for key,label in (('import_failures','import_attention'),('verification_failures','verification_failed'),
               ('unknown_formats','manifest_unavailable'),('source_mismatch','source_checksum_mismatch')) if row[key]]
    return dict(sha256=sha,label='Recording '+sha[:12],source_types=sorted(set(sources)),
        original=dict(state='verified' if row['verified_transfers'] or (row['artifact_count'] and not row['source_mismatch']) else 'unverified' if row['downloaded'] or row['artifact_count'] else 'expected',
            transfer_count=row['transfer_count'],artifact_count=row['artifact_count'],verification_basis='catalog_receipts_not_rehashed'),
        imports=dict(state=imports,current_profile=PROFILE,extractor_version=EXTRACTOR_VERSION,mapping_revision=MAPPING_REVISION,
            expected_profiles=row['expected_profiles'],current_jobs=row['current_count'],total_jobs=row['import_count']),
        indexing=dict(state=indexing,run_count=row['run_count'],current_toolchain_run_count=row['current_run_count']),
        reports=dict(state=('available' if row['report_count'] else 'absent' if applicable else 'not_applicable') if reports_available else 'unavailable',count=row['report_count'] if reports_available else None,reason=report_reason),
        attention=bool(row['attention']),attention_reasons=reasons,detail_url=PREFIX+'/'+sha)


def _section(db, sha, section, limit, after, params):
    params = dict(params, sha=sha, limit=limit+1, after=after)
    if section=='reports' and not params['reports_available']:
        return dict(sha256=sha,section=section,availability='unavailable',reason=params['report_reason'],
                    items=[],total=None,returned=0,limit=limit,next_cursor=None)
    if section == 'transfers':
        where='FROM fm WHERE sha256=:sha'
        sql='SELECT CASE WHEN length(id)<=128 THEN id END AS id,CASE WHEN length(state)<=32 THEN state END AS state,size,offset,CASE WHEN length(verification_state)<=32 THEN verification_state END AS verification_state,CASE WHEN length(verified_digest)<=64 THEN verified_digest END AS verified_digest,CASE WHEN length(format)<=32 THEN format END AS format,CASE WHEN length(profile)<=256 THEN profile END AS profile '+where+' AND id>:after ORDER BY id LIMIT :limit'
    elif section == 'imports':
        where='FROM import_jobs WHERE artifact_sha256=:sha'
        sql=("SELECT CASE WHEN length(id)<=128 THEN id END AS id,CASE WHEN length(profile)<=256 THEN profile END AS profile,CASE WHEN length(extractor_version)<=256 THEN extractor_version END AS extractor_version,CASE WHEN length(mapping_revision)<=128 THEN mapping_revision END AS mapping_revision,CASE WHEN length(state)<=32 THEN state END AS state,CASE WHEN length(error_code)<=64 THEN error_code ELSE 'other_import_error' END AS error_code "+where+' AND id>:after ORDER BY id LIMIT :limit')
    elif section == 'runs':
        where='FROM run_refs WHERE sha=:sha'
        sql='SELECT CASE WHEN length(run_id)<=128 THEN run_id END AS id,current_toolchain '+where+' AND run_id>:after ORDER BY run_id LIMIT :limit'
    else:
        where='FROM report_refs WHERE sha=:sha'
        sql='SELECT CASE WHEN length(report_id)<=128 THEN report_id END AS id,CASE WHEN length(run_id)<=128 THEN run_id END AS run_id '+where+' AND report_id>:after ORDER BY report_id LIMIT :limit'
    if section=='reports':
        # Materialize lightweight hash/run/report identities once, then page and
        # count that projection; do not reparse full trace documents per query.
        selection=sql[:sql.rfind(' AND ')]
        rows=db.execute(COMMON+', selected_reports AS ('+selection+'), page AS '
            '(SELECT *,1 AS present FROM selected_reports WHERE id>:after ORDER BY id LIMIT :limit), '
            'counted AS (SELECT COUNT(*) AS n FROM selected_reports) '
            'SELECT page.*,counted.n AS total FROM counted LEFT JOIN page ON 1',params).fetchall()
        total=rows[0]['total']
        rows=[row for row in rows if row['present'] is not None]
    else:
        total=db.execute(COMMON+'SELECT COUNT(*) '+where,params).fetchone()[0]
        rows=db.execute(COMMON+sql,params).fetchall()
    items=[]
    for row in rows[:limit]:
        identity=_id(row['id'])
        if section=='transfers':
            items.append(dict(id=identity,state=row['state'] if row['state'] in ('queued','downloading','verifying','complete','error','missing','partial') else 'unavailable',
                verified=row['state']=='complete' and type(row['size']) is int and row['size']>=0 and type(row['offset']) is int and row['offset']==row['size'] and row['verification_state']=='complete' and row['verified_digest']==sha,
                verification_state=row['verification_state'] if row['verification_state'] in ('queued','pending','running','complete','failed','error') else 'unavailable',
                format=row['format'] if row['format'] in ('wpilog','opaque') else 'unavailable',format_profile=_token(row['profile']) if row['profile'] else None))
        elif section=='imports':
            items.append(dict(id=identity,profile=_token(row['profile']),extractor_version=_token(row['extractor_version']),
                mapping_revision=_token(row['mapping_revision']),state=row['state'] if row['state'] in IMPORT_STATES else 'unavailable',
                error_code=row['error_code'] if row['error_code'] in ERROR_CODES else 'other_import_error' if row['error_code'] else None,
                supported_current_toolchain=row['extractor_version']==EXTRACTOR_VERSION and row['mapping_revision']==MAPPING_REVISION))
        elif section=='runs':
            items.append(dict(run_id=identity,url='/api/v1/runs/'+identity,current_toolchain=bool(row['current_toolchain'])))
        else:
            run=_id(row['run_id'])
            length=db.execute('SELECT length(CAST(result_json AS BLOB)) FROM analysis_reports WHERE report_id=?',(identity,)).fetchone()[0]
            digest=hashlib.sha256()
            for offset in range(0,length,65536):
                digest.update(db.execute('SELECT substr(CAST(result_json AS BLOB),?,65536) FROM analysis_reports WHERE report_id=?',(offset+1,identity)).fetchone()[0])
            items.append(dict(report_id=identity,run_id=run,url='/api/v1/runs/'+run+'/reports/'+identity,sha256=digest.hexdigest()))
    scope=sha+':'+section
    cursor=_cursor(scope,rows[limit-1]['id']) if len(rows)>limit else None
    return dict(sha256=sha,section=section,availability='available',reason=None,items=items,total=total,returned=len(items),limit=limit,next_cursor=cursor)


def _get(root,path,query,indexer):
    if type(query) is not dict or any(type(k) is not str or type(v) is not str for k,v in query.items()):
        raise ValueError('query')
    listing=path==PREFIX
    if not listing and (not path.startswith(PREFIX+'/') or '/' in path[len(PREFIX)+1:]):
        raise ValueError('query')
    if set(query)-({'view','limit','cursor'} if listing else {'section','limit','cursor'}):
        raise ValueError('query')
    sha=None if listing else _sha(path[len(PREFIX)+1:])
    section=query.get('section')
    if not listing and section is None and query:
        raise ValueError('query')
    if section is not None and section not in SECTIONS:
        raise ValueError('query')
    view=query.get('view','all')
    if view not in ('all','attention'):
        raise ValueError('query')
    limit=_limit(query)
    scope='list:'+view if listing else sha+':'+str(section)
    after=_after(query,scope)
    database=(Path(root)/'catalog.sqlite3').resolve()
    if not database.is_file():
        raise ValueError('evidence')
    with closing(sqlite3.connect(database.as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
        db.row_factory=sqlite3.Row
        db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
        # Fail visibly if a growing/corrupt catalog exceeds this read request's
        # SQL work budget. No schema reverse-index is added by this projection.
        budget=[5000]
        def within_budget():
            budget[0]-=1
            return 1 if budget[0]<0 else 0
        db.set_progress_handler(within_budget,10000)
        revision=_catalog(db)
        budget[0]=500
        try:
            reports_available,report_reason=_reports_status(db)
        except sqlite3.Error:
            reports_available,report_reason=False,'report_metadata_work_limit'
        budget[0]=5000
        params=dict(PARAMS,reports_available=int(reports_available),report_reason=report_reason)
        base=_base(indexer,revision)
        base.update(report_availability='available' if reports_available else 'unavailable',report_reason=report_reason)
        counts=db.execute(COMMON+'SELECT COUNT(*) AS recordings,COALESCE(SUM(attention),0) AS attention FROM flow',params).fetchone()
        base['counts']=dict(counts)
        if listing:
            where='WHERE attention=1' if view=='attention' else 'WHERE 1'
            total=counts['attention'] if view=='attention' else counts['recordings']
            rows=db.execute(COMMON+'SELECT * FROM flow '+where+' AND sha>:after ORDER BY sha LIMIT :limit',dict(params,after=after,limit=limit+1)).fetchall()
            items=[_summary(db,row,reports_available,report_reason) for row in rows[:limit]]
            return dict(base,items=items,total=total,returned=len(items),limit=limit,view=view,
                next_cursor=_cursor(scope,rows[limit-1]['sha']) if len(rows)>limit else None)
        row=db.execute(COMMON+'SELECT * FROM flow WHERE sha=:sha',dict(params,sha=sha)).fetchone()
        if row is None:
            raise ProcessingError(404,'recording_not_found')
        if section is None:
            sections={name:dict(total=row[field],url=PREFIX+'/'+sha+'?'+urlencode(dict(section=name)))
                for name,field in (('transfers','transfer_count'),('imports','import_count'),('runs','run_count'),('reports','report_count'))}
            sections['reports'].update(availability='available' if reports_available else 'unavailable',reason=report_reason,total=row['report_count'] if reports_available else None)
            return dict(base,recording=_summary(db,row,reports_available,report_reason),sections=sections)
        result=_section(db,sha,section,limit,after,params)
        if section=='runs':
            for item in result['items']:item['catalog_revision']=revision
        return dict(base,**result)


def get(root,path,query,indexer=None):
    try:
        value=_get(root,path,query,indexer)
        if len(json.dumps(value,allow_nan=False,separators=(',',':')).encode())>MAX_RESPONSE_BYTES:
            raise ValueError('evidence')
        return value
    except ProcessingError:
        raise
    except (ValueError,TypeError,KeyError,OverflowError,RecursionError) as exc:
        if str(exc)=='query':
            raise ProcessingError(400,'invalid_processing_query') from None
        raise ProcessingError(503,'processing_unavailable') from None
    except (OSError,sqlite3.Error):
        raise ProcessingError(503,'processing_unavailable') from None
