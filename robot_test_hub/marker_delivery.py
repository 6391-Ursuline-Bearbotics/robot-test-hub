"""Durable explicit contextual marker jobs; saved annotations remain hub-only."""
from __future__ import annotations
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
from .notebook import Notebook, NotebookError, validate, ID

PROFILE = 'testhub-note-marker-1'
MAX_WIRE = 16384
SHA = re.compile(r'[a-f0-9]{64}')
TERMINAL = {'robot_acknowledged','rejected','historical_boot_ended','unavailable'}

class MarkerError(ValueError):
    def __init__(self, code, status=400):
        super().__init__(code)
        self.code,self.status=code,status

def canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)

def strict_json(encoded, maximum=MAX_WIRE):
    if isinstance(encoded,bytes):
        if len(encoded)>maximum:raise MarkerError('marker_record_oversized')
        try:encoded=encoded.decode('utf-8',errors='strict')
        except UnicodeError:raise MarkerError('invalid_marker_record') from None
    try:
        if not isinstance(encoded,str) or len(encoded.encode('utf-8'))>maximum:raise MarkerError('marker_record_oversized')
    except UnicodeError:raise MarkerError('invalid_marker_record') from None
    # Bound nesting before the recursive JSON decoder; strings/escapes do not count.
    depth=0; quoted=False; escaped=False
    for ch in encoded:
        if quoted:
            if escaped:escaped=False
            elif ch=='\\':escaped=True
            elif ch=='"':quoted=False
        elif ch=='"':quoted=True
        elif ch in '[{':
            depth+=1
            if depth>8:raise MarkerError('marker_record_depth')
        elif ch in ']}':
            depth-=1
            if depth<0:raise MarkerError('invalid_marker_record')
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:raise MarkerError('duplicate_marker_field')
            result[key]=value
        return result
    try:
        return json.loads(encoded,object_pairs_hook=pairs,parse_constant=lambda _: (_ for _ in ()).throw(MarkerError('nonfinite_marker_field')))
    except (ValueError,UnicodeError,RecursionError) as exc:
        if isinstance(exc,MarkerError):raise
        raise MarkerError('invalid_marker_record') from None

def identity(value):
    if not isinstance(value,str) or not ID.fullmatch(value):raise MarkerError('invalid_marker_identity')
    return value

def revision(value):
    if type(value) is not int or not 1<=value<=1000000:raise MarkerError('invalid_marker_revision')
    return value

def digest(value):
    if not isinstance(value,str) or not SHA.fullmatch(value):raise MarkerError('invalid_marker_digest')
    return value

def install_schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS marker_deliveries (
        ordinal INTEGER PRIMARY KEY AUTOINCREMENT,delivery_id TEXT NOT NULL UNIQUE,
        event_id TEXT NOT NULL,note_revision INTEGER NOT NULL,annotation_sha256 TEXT NOT NULL,
        destination_robot_id TEXT NOT NULL,destination_boot_id TEXT NOT NULL,
        payload_sha256 TEXT NOT NULL,wire_json TEXT NOT NULL,state TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,error_code TEXT,receipt_robot_ns TEXT,
        ack_json TEXT,created_utc_ns TEXT NOT NULL,updated_utc_ns TEXT NOT NULL,
        UNIQUE(event_id,note_revision,destination_robot_id,destination_boot_id))""")

class MarkerStore:
    def __init__(self,path,*,clock_ns=time.time_ns,maximum_pending=128):
        self.path=Path(path);self.clock_ns=clock_ns;self.maximum_pending=maximum_pending
        with self.connection() as db:
            with db:install_schema(db)
    @contextmanager
    def connection(self):
        db=sqlite3.connect(self.path,timeout=2);db.row_factory=sqlite3.Row
        try:
            db.execute('PRAGMA synchronous=FULL');yield db
        finally:db.close()
    @staticmethod
    def public(row):
        keys=('delivery_id','event_id','note_revision','annotation_sha256','destination_robot_id',
              'destination_boot_id','payload_sha256','state','attempts','error_code','receipt_robot_ns','created_utc_ns','updated_utc_ns')
        result={key:row[key] for key in keys}
        ack=json.loads(row['ack_json']) if row['ack_json'] else None
        for key in ('logger_queue_fault','duplicate','runtime_mode'):
            result[key]=ack[key] if ack else None
        result.update(ack_scope='contextual_receipt_only',usb_durability='unavailable',robot_receipt_is_event_time=False,
                      annotation_source_digest_verified_by_robot=False)
        return result
    def schedule(self,request,*,robot_id):
        fields={'delivery_id','event_id','note_revision','annotation_sha256','destination_robot_id','destination_boot_id'}
        if not isinstance(request,dict) or set(request)!=fields:raise MarkerError('invalid_marker_request')
        for key in ('delivery_id','event_id','destination_robot_id','destination_boot_id'):identity(request[key])
        revision(request['note_revision']);digest(request['annotation_sha256'])
        if request['destination_robot_id']!=robot_id:raise MarkerError('marker_destination_mismatch',409)
        # Exact stored bytes, read only after Notebook.save's FULL commit.
        original=Notebook(self.path).exact_revision(request['event_id'],request['note_revision'])
        if original['sha256']!=request['annotation_sha256']:raise MarkerError('marker_annotation_conflict',409)
        payload={k:v for k,v in strict_json(original['payload_json']).items() if k not in ('author','device_id','run_id')}
        projected=canonical(payload)
        envelope={'schema_version':1,'profile':PROFILE,'delivery_id':request['delivery_id'],
            'event_id':request['event_id'],'revision':request['note_revision'],
            'destination_robot_id':request['destination_robot_id'],'destination_boot_id':request['destination_boot_id'],
            'annotation_sha256':request['annotation_sha256'],'payload_sha256':hashlib.sha256(projected.encode('utf-8')).hexdigest(),
            'payload_json':projected}
        wire=canonical(envelope)
        state='queued' if len(wire.encode('utf-8'))<=MAX_WIRE else 'unavailable'
        now=str(self.clock_ns())
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            with db:
                old=db.execute('SELECT * FROM marker_deliveries WHERE delivery_id=? OR (event_id=? AND note_revision=? AND destination_robot_id=? AND destination_boot_id=?)',
                    (request['delivery_id'],request['event_id'],request['note_revision'],request['destination_robot_id'],request['destination_boot_id'])).fetchone()
                if old:
                    if (any(old[key]!=request[key] for key in ('delivery_id','event_id','note_revision','destination_robot_id','destination_boot_id','annotation_sha256'))
                            or old['payload_sha256']!=envelope['payload_sha256']
                            or (old['wire_json'] and old['wire_json']!=wire)):
                        raise MarkerError('marker_delivery_conflict',409)
                    return {'schema_version':1,'delivery':self.public(old),'idempotent':True}
                count=db.execute("SELECT COUNT(*) FROM marker_deliveries WHERE state NOT IN ('robot_acknowledged','rejected','historical_boot_ended','unavailable')").fetchone()[0]
                if count>=self.maximum_pending:raise MarkerError('marker_outbox_full',409)
                # Oversized requests are saved as unavailable without retaining unbounded wire bytes.
                db.execute('INSERT INTO marker_deliveries(delivery_id,event_id,note_revision,annotation_sha256,destination_robot_id,destination_boot_id,payload_sha256,wire_json,state,error_code,created_utc_ns,updated_utc_ns) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    (request['delivery_id'],request['event_id'],request['note_revision'],request['annotation_sha256'],request['destination_robot_id'],request['destination_boot_id'],envelope['payload_sha256'],wire if state=='queued' else '',state,'marker_payload_oversized' if state=='unavailable' else None,now,now))
                row=db.execute('SELECT * FROM marker_deliveries WHERE delivery_id=?',(request['delivery_id'],)).fetchone()
            return {'schema_version':1,'delivery':self.public(row),'idempotent':False}
    def get(self,delivery_id,*,private=False):
        identity(delivery_id)
        with self.connection() as db:
            row=db.execute('SELECT * FROM marker_deliveries WHERE delivery_id=?',(delivery_id,)).fetchone()
            if row is None:raise MarkerError('marker_delivery_not_found',404)
            return dict(row) if private else {'schema_version':1,'delivery':self.public(row)}
    def page(self,*,limit=20,cursor=None):
        if type(limit) is not int or not 1<=limit<=100:raise MarkerError('invalid_marker_query')
        if cursor is not None and (not isinstance(cursor,str) or not re.fullmatch(r'[1-9][0-9]{0,18}',cursor) or int(cursor)>=(1<<63)):raise MarkerError('invalid_marker_cursor')
        with self.connection() as db:
            rows=db.execute('SELECT * FROM marker_deliveries WHERE ordinal>? ORDER BY ordinal LIMIT ?',(int(cursor or 0),limit+1)).fetchall()
            return {'schema_version':1,'items':[self.public(row) for row in rows[:limit]],'next_cursor':str(rows[limit-1]['ordinal']) if len(rows)>limit else None}
    def pending(self):
        with self.connection() as db:
            row=db.execute("SELECT * FROM marker_deliveries WHERE state NOT IN ('robot_acknowledged','rejected','historical_boot_ended','unavailable') ORDER BY ordinal LIMIT 1").fetchone()
            return dict(row) if row else None
    def update(self,delivery_id,state,*,error=None,attempt=False,ack=None):
        states={'queued','waiting_fresh_status','awaiting_ack','retry_wait'}|TERMINAL
        if state not in states:raise MarkerError('invalid_marker_state')
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            with db:
                row=db.execute('SELECT * FROM marker_deliveries WHERE delivery_id=?',(delivery_id,)).fetchone()
                if row is None:raise MarkerError('marker_delivery_not_found',404)
                if row['state'] in TERMINAL:return
                db.execute('UPDATE marker_deliveries SET state=?,attempts=attempts+?,error_code=?,receipt_robot_ns=?,ack_json=?,updated_utc_ns=? WHERE delivery_id=?',
                    (state,int(attempt),error,ack.get('receipt_robot_ns') if ack else row['receipt_robot_ns'],canonical(ack) if ack else row['ack_json'],str(self.clock_ns()),delivery_id))
    def retry(self,delivery_id):
        row=self.get(delivery_id,private=True)
        if row['state'] in TERMINAL:raise MarkerError('marker_retry_not_allowed',409)
        self.update(delivery_id,'queued')
        return self.get(delivery_id)

def validate_ack(value,job,runtime_mode):
    pins=('delivery_id','event_id','destination_robot_id','destination_boot_id','annotation_sha256','payload_sha256')
    fields=set(pins)|{'schema_version','profile','revision','state','duplicate','receipt_robot_ns','reason','ack_scope','usb_durability','logger_queue_fault','runtime_mode'}
    if not isinstance(value,dict) or set(value)!=fields:raise MarkerError('invalid_marker_ack')
    if type(value['schema_version']) is not int or value['schema_version']!=1 or value['profile']!=PROFILE:raise MarkerError('invalid_marker_ack')
    if any(value[k]!=job[k] for k in pins) or type(value['revision']) is not int or value['revision']!=job['note_revision']:raise MarkerError('marker_ack_pin_mismatch')
    if value['runtime_mode']!=runtime_mode or value['ack_scope']!='contextual_receipt_only' or value['usb_durability']!='unavailable':raise MarkerError('invalid_marker_ack')
    if type(value['duplicate']) is not bool or type(value['logger_queue_fault']) is not bool:raise MarkerError('invalid_marker_ack')
    if value['state']=='accepted_into_log_input':
        ns=value['receipt_robot_ns']
        if (not isinstance(ns,str) or not re.fullmatch(r'(?:0|[1-9][0-9]{0,18})',ns) or int(ns)>=(1<<63) or value['reason'] is not None):raise MarkerError('invalid_marker_ack')
    elif value['state']=='rejected':
        if value['duplicate'] is not False or value['receipt_robot_ns'] is not None or value['reason'] not in {'wrong_robot','wrong_boot','payload_conflict','capacity','log_input_unavailable'}:raise MarkerError('invalid_marker_ack')
    else:raise MarkerError('invalid_marker_ack')
    if job.get('receipt_robot_ns') is not None and value['receipt_robot_ns']!=job['receipt_robot_ns']:raise MarkerError('marker_ack_receipt_conflict')
    return value
