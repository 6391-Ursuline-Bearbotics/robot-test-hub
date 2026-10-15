"""Explicit durable retries of exact local importer attempts; never source reads."""
import re
import sqlite3
import time

from .wpilog import PROFILE,EXTRACTOR_VERSION,MAPPING_REVISION
from .report_lookup import atomic,_normalized

FIELDS=('id','job_id','source_sha256','extractor_version','profile','mapping_revision',
 'expected_attempts','expected_updated_utc_ns','state','error_code','created_utc_ns','updated_utc_ns',
 'attempt_updated_utc_ns','result_state','result_attempts','result_updated_utc_ns')
PIN_FIELDS=('job_id','source_sha256','extractor_version','profile','mapping_revision','expected_attempts','expected_updated_utc_ns')
REQUEST_COLUMNS=','.join((f"CASE WHEN {key} IS NULL OR length(CAST({key} AS BLOB))<={256 if key in ('extractor_version','profile','mapping_revision') else 128} THEN {key} ELSE X'00' END AS {key}" if key not in ('expected_attempts','result_attempts') else key) for key in FIELDS)
RETRY_CODES=('import_io_or_integrity','extractor_failure')
STATES=('pending','running','completed','failed','superseded')
SCHEMA_SQL={
'import_retry_requests':"""CREATE TABLE IF NOT EXISTS import_retry_requests (
 id TEXT PRIMARY KEY NOT NULL,job_id TEXT NOT NULL,source_sha256 TEXT NOT NULL,
 extractor_version TEXT NOT NULL,profile TEXT NOT NULL,mapping_revision TEXT NOT NULL,
 expected_attempts INTEGER NOT NULL CHECK(expected_attempts>=0),expected_updated_utc_ns TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('pending','running','completed','failed','superseded')),
 error_code TEXT,created_utc_ns TEXT NOT NULL,updated_utc_ns TEXT NOT NULL,
 attempt_updated_utc_ns TEXT,result_state TEXT,result_attempts INTEGER,result_updated_utc_ns TEXT,
 UNIQUE(job_id,expected_attempts,expected_updated_utc_ns))""",
'import_retry_pending':"CREATE INDEX IF NOT EXISTS import_retry_pending ON import_retry_requests(state,created_utc_ns,id)",
'import_retry_job':"CREATE INDEX IF NOT EXISTS import_retry_job ON import_retry_requests(job_id)",
}


class RetryError(ValueError):
    def __init__(self,status,code):
        super().__init__(code);self.status=status;self.code=code


def install_schema(db):
    for sql in SCHEMA_SQL.values():db.execute(sql)


def validate_schema(db):
    for name,sql in SCHEMA_SQL.items():
        row=db.execute('SELECT sql FROM sqlite_master WHERE name=?',(name,)).fetchone()
        if row is None or row[0] is None or _normalized(row[0])!=_normalized(sql):
            raise ValueError('import_retry_schema_invalid')


def _id(value):
    return type(value) is str and re.fullmatch('[A-Za-z0-9_-]{1,128}',value) is not None


def _ns(value):
    return type(value) is str and re.fullmatch('0|[1-9][0-9]{0,18}',value) is not None and int(value)<1<<63


def eligibility(job):
    """Pure metadata policy for both the read-only UI and enqueue CAS."""
    try:
        attempts=job['attempts'];updated=job['updated_utc_ns']
        if type(updated) is int:updated=str(updated)
        pins=dict(job_id=job['id'],source_sha256=job['artifact_sha256'],extractor_version=job['extractor_version'],
            profile=job['profile'],mapping_revision=job['mapping_revision'],expected_attempts=attempts,expected_updated_utc_ns=updated)
        if (not _id(pins['job_id']) or type(pins['source_sha256']) is not str or not re.fullmatch('[a-f0-9]{64}',pins['source_sha256'])
            or type(attempts) is not int or not 0<=attempts<1<<31 or not _ns(updated)
            or any(type(pins[key]) is not str or not re.fullmatch('[A-Za-z0-9_.-]{1,256}',pins[key]) for key in ('profile','extractor_version','mapping_revision'))):
            raise ValueError()
        if (pins['profile'],pins['extractor_version'],pins['mapping_revision'])!=(PROFILE,EXTRACTOR_VERSION,MAPPING_REVISION):reason='unsupported_version'
        elif job['state']!='failed':reason='state_not_retryable'
        elif job['error_code'] not in RETRY_CODES:reason='error_not_retryable'
        else:reason='eligible'
        return dict(eligible=reason=='eligible',reason=reason,pins=pins)
    except (KeyError,TypeError,ValueError,OverflowError):
        return dict(eligible=False,reason='invalid_job_metadata',pins=None)


def project_request(row):
    value=dict(row)
    result={key:value.get(key) for key in FIELDS}
    if (not _id(result['job_id']) or type(result['id']) is not str or not (re.fullmatch('[a-f0-9]{32}',result['id']) or re.fullmatch('[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}',result['id']))
        or type(result['source_sha256']) is not str or not re.fullmatch('[a-f0-9]{64}',result['source_sha256'])
        or any(type(result[key]) is not str or not re.fullmatch('[A-Za-z0-9_.-]{1,256}',result[key]) for key in ('profile','extractor_version','mapping_revision'))
        or type(result['expected_attempts']) is not int or not 0<=result['expected_attempts']<1<<31
        or result['state'] not in STATES
        or any(not _ns(result[key]) for key in ('created_utc_ns','updated_utc_ns','expected_updated_utc_ns'))
        or any(result[key] is not None and not _ns(result[key]) for key in ('attempt_updated_utc_ns','result_updated_utc_ns'))
        or result['result_state'] not in (None,'succeeded','succeeded_with_unsupported','invalid','unsupported','checksum_mismatch','failed')
        or (result['result_attempts'] is not None and (type(result['result_attempts']) is not int or not 0<=result['result_attempts']<1<<31))):
        raise RetryError(503,'import_retry_storage_unavailable')
    # Every persisted code is fixed by this module/the importer; never project
    # arbitrary exception text even after unsupported manual catalog corruption.
    codes=set(RETRY_CODES)|{'import_retry_attempt_changed','invalid_format','unsupported_profile','unsupported_schema','resource_limit','checksum_mismatch','import_retry_storage_failed'}
    if result['error_code'] is not None and result['error_code'] not in codes:result['error_code']='import_retry_storage_failed'
    return result


def _payload(job_id,value):
    if type(value) is not dict or set(value)!={'request_id',*PIN_FIELDS} or value.get('job_id')!=job_id:
        raise RetryError(400,'invalid_import_retry')
    request=value['request_id']
    if type(request) is not str or not (re.fullmatch('[a-f0-9]{32}',request) or re.fullmatch('[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}',request)):
        raise RetryError(400,'invalid_import_retry')
    if (not _id(job_id) or type(value['source_sha256']) is not str or not re.fullmatch('[a-f0-9]{64}',value['source_sha256'])
        or type(value['expected_attempts']) is not int or not 0<=value['expected_attempts']<1<<31 or not _ns(value['expected_updated_utc_ns'])
        or any(type(value[key]) is not str or not re.fullmatch('[A-Za-z0-9_.-]{1,256}',value[key]) for key in ('profile','extractor_version','mapping_revision'))):
        raise RetryError(400,'invalid_import_retry')
    return value


def enqueue(db,job_id,payload):
    """Only a small catalog transaction. No importer, filesystem or source IO."""
    payload=_payload(job_id,payload)
    with atomic(db):
        existing=db.execute('SELECT '+REQUEST_COLUMNS+' FROM import_retry_requests WHERE id=?',(payload['request_id'],)).fetchone()
        if existing:
            value=project_request(dict(existing))
            if any(value[key]!=payload[key] for key in PIN_FIELDS):raise RetryError(409,'import_retry_conflict')
            return dict(schema_version=1,request=project_request(value),idempotent=True,deduplicated=False)
        existing=db.execute('SELECT '+REQUEST_COLUMNS+' FROM import_retry_requests WHERE job_id=? AND expected_attempts=? AND expected_updated_utc_ns=?',
            (job_id,payload['expected_attempts'],payload['expected_updated_utc_ns'])).fetchone()
        if existing:
            value=project_request(dict(existing))
            if any(value[key]!=payload[key] for key in PIN_FIELDS):raise RetryError(409,'import_retry_conflict')
            return dict(schema_version=1,request=project_request(value),idempotent=True,deduplicated=True)
        job=db.execute('SELECT * FROM import_jobs WHERE id=?',(job_id,)).fetchone()
        if job is None:raise RetryError(404,'import_job_not_found')
        policy=eligibility(dict(job))
        if not policy['eligible'] or policy['pins']!={key:payload[key] for key in PIN_FIELDS}:
            raise RetryError(409,'import_retry_stale_or_ineligible')
        now=str(time.time_ns())
        db.execute("INSERT INTO import_retry_requests(id,job_id,source_sha256,extractor_version,profile,mapping_revision,expected_attempts,expected_updated_utc_ns,state,created_utc_ns,updated_utc_ns) VALUES(?,?,?,?,?,?,?,?,'pending',?,?)",
            (payload['request_id'],*(payload[key] for key in PIN_FIELDS),now,now))
        row=db.execute('SELECT '+REQUEST_COLUMNS+' FROM import_retry_requests WHERE id=?',(payload['request_id'],)).fetchone()
        return dict(schema_version=1,request=project_request(dict(row)),idempotent=False,deduplicated=False)


def supersede(db,request_id):
    db.execute("UPDATE import_retry_requests SET state='superseded',error_code='import_retry_attempt_changed',updated_utc_ns=? WHERE id=? AND state IN ('pending','running')",(str(time.time_ns()),request_id))


def complete(db,request_id,job):
    """Called within the same transaction that publishes the job's outcome."""
    db.execute('UPDATE import_retry_requests SET state=?,error_code=?,updated_utc_ns=?,result_state=?,result_attempts=?,result_updated_utc_ns=? WHERE id=? AND state=?',
        ('completed' if job['state'] in ('succeeded','succeeded_with_unsupported') else 'failed',job['error_code'],str(time.time_ns()),job['state'],job['attempts'],str(job['updated_utc_ns']),request_id,'running'))


def step(root,db):
    """Claim/reconcile one exact request, then do local work outside SQL locks."""
    with atomic(db):
        row=db.execute("SELECT * FROM import_retry_requests WHERE state IN ('running','pending') ORDER BY CASE state WHEN 'running' THEN 0 ELSE 1 END,created_utc_ns,id LIMIT 1").fetchone()
        if row is None:return None
        request=dict(row)
        jobrow=db.execute('SELECT * FROM import_jobs WHERE id=?',(request['job_id'],)).fetchone()
        job=dict(jobrow) if jobrow else None
        if request['state']=='pending':
            policy=eligibility(job or {})
            pins={key:request[key] for key in PIN_FIELDS}
            if not policy['eligible'] or policy['pins']!=pins:
                supersede(db,request['id']);return dict(state='superseded')
            stamp=str(max(time.time_ns(),int(request['expected_updated_utc_ns'])+1))
            db.execute("UPDATE import_jobs SET state='running',attempts=attempts+1,error_code=NULL,error=NULL,updated_utc_ns=? WHERE id=?",(int(stamp),job['id']))
            db.execute("UPDATE import_retry_requests SET state='running',attempt_updated_utc_ns=?,updated_utc_ns=? WHERE id=?",(stamp,stamp,request['id']))
            request['attempt_updated_utc_ns']=stamp
        else:
            if (job is None or job['state']!='running' or job['attempts']!=request['expected_attempts']+1
                or str(job['updated_utc_ns'])!=request['attempt_updated_utc_ns']
                or any(job[key]!=request[pin] for key,pin in (('artifact_sha256','source_sha256'),('extractor_version','extractor_version'),('profile','profile'),('mapping_revision','mapping_revision')))):
                supersede(db,request['id']);return dict(state='superseded')
    from .importer import Importer
    importer=Importer(root,db)
    importer.retry_existing(request['job_id'],request_id=request['id'],expected_attempts=request['expected_attempts']+1,
                            expected_updated_utc_ns=request['attempt_updated_utc_ns'])
    return dict(state='processed',request_id=request['id'])
