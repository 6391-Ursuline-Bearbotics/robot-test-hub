"""Maintained report provenance lookup; writers/backfill only, never HTTP repair.

Schema installation queues discovery without parsing legacy report bodies. Exact
stored bytes are hashed once per generation. A pending/failed generation never
supplies references, including after direct SQL replacement with foreign keys off.
"""
from contextlib import contextmanager
import hashlib
import itertools
import re
import sqlite3

MAX_REPORT_BYTES = 64 * 1024 * 1024
MAX_VM_STEPS = 5_000_000
MAX_SOURCES = 1024
MAX_CHECKS = 16
_NAMES = itertools.count()

TABLE_SQL = {
'report_lookup_state': """CREATE TABLE IF NOT EXISTS report_lookup_state (
 id INTEGER PRIMARY KEY CHECK(id=1), generation INTEGER NOT NULL DEFAULT 0,
 discovery_after TEXT NOT NULL DEFAULT '', discovery_complete INTEGER NOT NULL DEFAULT 0 CHECK(discovery_complete IN (0,1)),
 error_code TEXT)""",
'report_lookup_jobs': """CREATE TABLE IF NOT EXISTS report_lookup_jobs (
 report_id TEXT PRIMARY KEY REFERENCES analysis_reports(report_id) ON DELETE CASCADE,
 generation INTEGER NOT NULL, state TEXT NOT NULL CHECK(state IN ('pending','running','ready','failed')),
 body_sha256 TEXT, run_id TEXT, error_code TEXT)""",
'report_source_refs': """CREATE TABLE IF NOT EXISTS report_source_refs (
 report_id TEXT NOT NULL, source_sha256 TEXT NOT NULL, generation INTEGER NOT NULL,
 PRIMARY KEY(report_id,source_sha256),
 FOREIGN KEY(report_id) REFERENCES analysis_reports(report_id) ON DELETE CASCADE)""",
}
INDEX_SQL = {
 'report_source_sha': 'CREATE INDEX IF NOT EXISTS report_source_sha ON report_source_refs(source_sha256,report_id)',
 'report_lookup_pending': 'CREATE INDEX IF NOT EXISTS report_lookup_pending ON report_lookup_jobs(state,generation,report_id)',
}
TRIGGER_SQL = {
'report_lookup_insert': """CREATE TRIGGER IF NOT EXISTS report_lookup_insert AFTER INSERT ON analysis_reports BEGIN
 UPDATE report_lookup_state SET generation=generation+1 WHERE id=1;
 DELETE FROM report_source_refs WHERE report_id IS NEW.report_id;
 DELETE FROM report_lookup_jobs WHERE report_id IS NEW.report_id;
 INSERT INTO report_lookup_jobs(report_id,generation,state)
 SELECT NEW.report_id,generation,'pending' FROM report_lookup_state WHERE id=1;
 END""",
'report_lookup_update': """CREATE TRIGGER IF NOT EXISTS report_lookup_update AFTER UPDATE ON analysis_reports BEGIN
 UPDATE report_lookup_state SET generation=generation+1 WHERE id=1;
 DELETE FROM report_source_refs WHERE report_id IS OLD.report_id OR report_id IS NEW.report_id;
 DELETE FROM report_lookup_jobs WHERE report_id IS OLD.report_id OR report_id IS NEW.report_id;
 INSERT INTO report_lookup_jobs(report_id,generation,state)
 SELECT NEW.report_id,generation,'pending' FROM report_lookup_state WHERE id=1;
 END""",
'report_lookup_delete': """CREATE TRIGGER IF NOT EXISTS report_lookup_delete AFTER DELETE ON analysis_reports BEGIN
 UPDATE report_lookup_state SET generation=generation+1 WHERE id=1;
 DELETE FROM report_source_refs WHERE report_id IS OLD.report_id;
 DELETE FROM report_lookup_jobs WHERE report_id IS OLD.report_id;
 END""",
}
SCHEMA_SQL = {**TABLE_SQL, **INDEX_SQL, **TRIGGER_SQL}
REFERENCES_SQL = """SELECT r.source_sha256 AS sha,r.report_id AS report_id,j.run_id AS run_id,j.body_sha256 AS sha256
 FROM report_source_refs r JOIN report_lookup_jobs j
 ON j.report_id=r.report_id AND j.generation=r.generation
 JOIN analysis_reports a ON a.report_id=r.report_id
 WHERE j.state='ready'"""


@contextmanager
def atomic(db):
    """Savepoint isolation never commits a caller-owned transaction."""
    nested = db.in_transaction
    name = 'report_lookup_' + str(next(_NAMES))
    db.execute('SAVEPOINT '+name if nested else 'BEGIN IMMEDIATE')
    try:
        yield
    except BaseException:
        if nested:
            db.execute('ROLLBACK TO '+name)
            db.execute('RELEASE '+name)
        else:
            db.rollback()
        raise
    else:
        db.execute('RELEASE '+name) if nested else db.commit()


def has_schema(db):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='report_lookup_state'").fetchone() is not None


def install_schema(db):
    """Schema only; caller owns migration transaction. Never use executescript."""
    for statement in SCHEMA_SQL.values():
        db.execute(statement)
    db.execute("INSERT OR IGNORE INTO report_lookup_state(id,generation,discovery_after,discovery_complete) "
               "SELECT 1,0,'',CASE WHEN EXISTS(SELECT 1 FROM analysis_reports) THEN 0 ELSE 1 END")


def _normalized(sql):
    parts=re.split("('(?:''|[^'])*')",sql)
    for index in range(0,len(parts),2):
        parts[index]=re.sub(r'\s+',' ',re.sub(r'\bIF\s+NOT\s+EXISTS\b','',parts[index],flags=re.I)).lower()
    return ''.join(parts).strip().rstrip(';')


def validate_schema(db):
    """Reject altered/missing definitions without installing or repairing them."""
    for name, expected in SCHEMA_SQL.items():
        row = db.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone()
        if row is None or row[0] is None or _normalized(row[0]) != _normalized(expected):
            raise ValueError('report_lookup_schema_invalid')
    row = db.execute("SELECT generation,typeof(discovery_after),discovery_complete,CASE WHEN error_code IS NULL OR length(error_code)<=64 THEN error_code ELSE 'report_lookup_state_invalid' END FROM report_lookup_state WHERE id=1").fetchone()
    if (row is None or type(row[0]) is not int or not 0 <= row[0] < (1 << 63)
            or row[1]!='text' or row[2] not in (0,1)
            or row[3] not in (None,'report_discovery_failed')):
        raise ValueError('report_lookup_state_invalid')


def coverage(db):
    """Read-only metadata; incomplete coverage never establishes an empty result."""
    state = db.execute("SELECT discovery_complete,CASE WHEN error_code IS NULL OR error_code='report_discovery_failed' THEN error_code ELSE 'report_lookup_state_invalid' END FROM report_lookup_state WHERE id=1").fetchone()
    if state is None:
        raise ValueError('report_lookup_state_invalid')
    counts = dict(db.execute('SELECT state,COUNT(*) FROM report_lookup_jobs GROUP BY state').fetchall())
    pending = counts.get('pending',0)+counts.get('running',0)
    failed = counts.get('failed',0)
    known = db.execute('SELECT COUNT(*) FROM analysis_reports').fetchone()[0]
    missing = db.execute('SELECT EXISTS(SELECT 1 FROM analysis_reports a LEFT JOIN report_lookup_jobs j ON j.report_id IS a.report_id WHERE j.rowid IS NULL)').fetchone()[0]
    complete = bool(state[0]) and not pending and not failed and not missing and state[1] is None
    return dict(complete=complete,state='complete' if complete else 'failed' if failed or state[1] else 'incomplete',
        pending=pending,failed=failed,ready=counts.get('ready',0),discovery_complete=bool(state[0]),
        known_reports=known,error_code=state[1])


def _discover(db, limit):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('invalid_report_lookup_limit')
    with atomic(db):
        state = db.execute("SELECT discovery_complete,CASE WHEN error_code IS NULL OR error_code='report_discovery_failed' THEN error_code ELSE 'report_lookup_state_invalid' END FROM report_lookup_state WHERE id=1").fetchone()
        # SQLite TEXT PRIMARY KEY historically permits NULL. These values
        # cannot enter the ordinary >'' keyset, so fail one ambiguous legacy
        # identity per tick without exposing or parsing a report body.
        db.execute("INSERT INTO report_lookup_jobs(report_id,generation,state,error_code) SELECT a.report_id,s.generation,'failed','report_metadata_invalid' FROM analysis_reports a CROSS JOIN report_lookup_state s WHERE s.id=1 AND (a.report_id IS NULL OR a.report_id='') AND NOT EXISTS(SELECT 1 FROM report_lookup_jobs j WHERE j.report_id IS a.report_id) LIMIT 1")
        if state[0] or state[1]:
            return
        # Keep opaque/private IDs in SQL during discovery, including malformed
        # legacy identifiers. Their jobs fail safely without exporting the ID.
        selector = ('SELECT report_id FROM analysis_reports WHERE report_id>'
                    '(SELECT discovery_after FROM report_lookup_state WHERE id=1) ORDER BY report_id LIMIT ?')
        count = db.execute('SELECT COUNT(*) FROM ('+selector+')', (limit,)).fetchone()[0]
        db.execute('INSERT OR IGNORE INTO report_lookup_jobs(report_id,generation,state) '
            "SELECT a.report_id,s.generation,'pending' FROM ("+selector+') a CROSS JOIN report_lookup_state s WHERE s.id=1', (limit,))
        db.execute('UPDATE report_lookup_state SET discovery_after=COALESCE((SELECT MAX(report_id) FROM ('+selector+')),discovery_after),discovery_complete=? WHERE id=1', (limit,int(count < limit)))


@contextmanager
def _snapshot(db):
    # A caller-owned transaction already has a consistent snapshot. Worker
    # reads release theirs before publishing, so generation CAS sees replacements.
    owned = not db.in_transaction
    if owned:
        db.execute('BEGIN')
    try:
        yield
    finally:
        if owned:
            db.rollback()


def _read_report(db, report_id, generation, *, max_bytes=MAX_REPORT_BYTES, max_vm_steps=MAX_VM_STEPS):
    """Return small metadata/hash from one consistent stored-body snapshot."""
    result = dict(report_id=report_id,generation=generation,error_code=None,body_sha256=None,run_id=None,sources=[])
    if type(report_id) is not str or not re.fullmatch('[A-Za-z0-9_-]{1,128}',report_id):
        result['error_code']='report_metadata_invalid'
        return result
    granularity=min(1000,max(1,max_vm_steps))
    budget=[max(1,max_vm_steps//granularity)]
    def progress():
        budget[0]-=1
        return int(budget[0]<0)
    db.set_progress_handler(progress,granularity)
    try:
        with _snapshot(db):
            row = db.execute('SELECT length(CAST(result_json AS BLOB)) FROM analysis_reports WHERE report_id=?',(report_id,)).fetchone()
            if row is None:
                result['error_code']='report_missing'
                return result
            size=row[0]
            if type(size) is not int or not 0 < size <= max_bytes:
                result['error_code']='report_resource_limit'
                return result
            digest=hashlib.sha256()
            for offset in range(0,size,65536):
                digest.update(db.execute('SELECT substr(CAST(result_json AS BLOB),?,65536) FROM analysis_reports WHERE report_id=?',(offset+1,report_id)).fetchone()[0])
            result['body_sha256']=digest.hexdigest()
            valid=db.execute('SELECT json_valid(result_json) FROM analysis_reports WHERE report_id=?',(report_id,)).fetchone()[0]
            if valid != 1:
                result['error_code']='report_metadata_invalid'
                return result
            metadata=db.execute("SELECT CASE WHEN json_type(result_json,'$.report_id')='text' AND length(json_extract(result_json,'$.report_id'))<=128 THEN json_extract(result_json,'$.report_id') END,CASE WHEN json_type(result_json,'$.run_id')='text' AND length(json_extract(result_json,'$.run_id'))<=128 THEN json_extract(result_json,'$.run_id') END,json_type(result_json,'$.checks'),json_array_length(result_json,'$.checks') FROM analysis_reports WHERE report_id=?",(report_id,)).fetchone()
            if metadata[0]!=report_id:
                result['error_code']='report_metadata_invalid'
                return result
            if metadata[1] is None:
                result['error_code']='report_not_run_scoped'
                return result
            if not re.fullmatch('[A-Za-z0-9_-]{1,128}',metadata[1]) or metadata[2]!='array' or not 1 <= metadata[3] <= MAX_CHECKS:
                result['error_code']='report_metadata_invalid'
                return result
            bad=db.execute("SELECT 1 FROM analysis_reports a,json_each(a.result_json,'$.checks') c WHERE a.report_id=? AND CASE WHEN c.type='object' THEN json_type(c.value,'$.provenance') IS NOT 'object' OR json_type(c.value,'$.provenance.source_hashes') IS NOT 'array' ELSE 1 END LIMIT 1",(report_id,)).fetchone()
            if bad:
                result['error_code']='report_metadata_invalid'
                return result
            for suffix, groups in (("json_each(a.result_json) t",'t.key'),("json_each(a.result_json,'$.checks') c,json_each(c.value) t",'c.key,t.key'),("json_each(a.result_json,'$.checks') c,json_each(c.value,'$.provenance') t",'c.key,t.key')):
                duplicate=db.execute('SELECT 1 FROM analysis_reports a,'+suffix+' WHERE a.report_id=? GROUP BY '+groups+' HAVING COUNT(*)>1 LIMIT 1',(report_id,)).fetchone()
                if duplicate:
                    result['error_code']='report_metadata_invalid'
                    return result
            sources=db.execute("SELECT DISTINCT CASE WHEN h.type='text' AND length(h.value)=64 AND h.value NOT GLOB '*[^a-f0-9]*' THEN h.value END FROM analysis_reports a,json_each(a.result_json,'$.checks') c,json_each(c.value,'$.provenance.source_hashes') h WHERE a.report_id=? LIMIT ?",(report_id,MAX_SOURCES+1)).fetchall()
            if not sources or len(sources)>MAX_SOURCES or any(row[0] is None for row in sources):
                result['error_code']='report_metadata_invalid'
                return result
            result.update(run_id=metadata[1],sources=sorted(row[0] for row in sources))
            return result
    except sqlite3.Error as exc:
        if str(exc) not in ('interrupted','malformed JSON'):
            # Disk/schema/connection failures belong to worker diagnostics;
            # they are not a malformed report or a successful lookup.
            raise
        result.update(error_code='report_resource_limit' if str(exc)=='interrupted' else 'report_metadata_invalid',sources=[],run_id=None)
        return result
    finally:
        db.set_progress_handler(None,0)


def _publish(db, result):
    with atomic(db):
        row=db.execute('SELECT generation FROM report_lookup_jobs WHERE report_id=?',(result['report_id'],)).fetchone()
        if row is None or row[0]!=result['generation']:
            return False
        db.execute('DELETE FROM report_source_refs WHERE report_id=?',(result['report_id'],))
        if result['error_code'] is None:
            db.executemany('INSERT INTO report_source_refs VALUES(?,?,?)',
                [(result['report_id'],source,result['generation']) for source in result['sources']])
        db.execute('UPDATE report_lookup_jobs SET state=?,body_sha256=?,run_id=?,error_code=? WHERE report_id=? AND generation=?',
            ('failed' if result['error_code'] else 'ready',result['body_sha256'],result['run_id'],result['error_code'],result['report_id'],result['generation']))
        return result['error_code'] is None


def index_report(db, report_id, *, max_bytes=MAX_REPORT_BYTES, max_vm_steps=MAX_VM_STEPS):
    """Normalize the actual stored row without committing a caller transaction."""
    row=db.execute('SELECT generation,state FROM report_lookup_jobs WHERE report_id=?',(report_id,)).fetchone()
    if row is None:
        return False
    if row[1] in ('ready','failed'):
        return row[1]=='ready'
    result=_read_report(db,report_id,row[0],max_bytes=max_bytes,max_vm_steps=max_vm_steps)
    return _publish(db,result)


def step(db, *, discovery_limit=50, max_bytes=MAX_REPORT_BYTES, max_vm_steps=MAX_VM_STEPS):
    """Bounded legacy discovery and one resumable job; no sleep or source access."""
    _discover(db,discovery_limit)
    with atomic(db):
        row=db.execute("SELECT rowid,CASE WHEN length(report_id)<=128 THEN report_id END FROM report_lookup_jobs WHERE state IN ('pending','running') ORDER BY CASE state WHEN 'running' THEN 0 ELSE 1 END,generation,report_id LIMIT 1").fetchone()
        if row:
            if row[1] is None:
                db.execute("UPDATE report_lookup_jobs SET state='failed',error_code='report_metadata_invalid' WHERE rowid=?",(row[0],))
            else:
                db.execute("UPDATE report_lookup_jobs SET state='running' WHERE rowid=?",(row[0],))
    if row and row[1] is not None:
        index_report(db,row[1],max_bytes=max_bytes,max_vm_steps=max_vm_steps)
    return coverage(db)
