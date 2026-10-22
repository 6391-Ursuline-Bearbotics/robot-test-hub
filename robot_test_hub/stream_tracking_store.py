"""Persistent exact interval weights and bounded percentile work for pipeline 10."""
import hashlib
import json
import math
from .analysis import canonical, identity
from .report_lookup import _normalized

VERSION = 'tracking-work-1'
INITIAL_CHAIN = hashlib.sha256(b'tracking-work-1\0').hexdigest()
SQL = {
 'analysis_tracking_samples': '''CREATE TABLE IF NOT EXISTS analysis_tracking_samples (
 stream_id TEXT NOT NULL,generation INTEGER NOT NULL,sequence INTEGER NOT NULL,
 chunk_ordinal INTEGER NOT NULL,source_ordinal INTEGER NOT NULL,line_ordinal INTEGER NOT NULL,
 module INTEGER NOT NULL,metric TEXT NOT NULL,error REAL NOT NULL,duration_ns INTEGER NOT NULL,
 PRIMARY KEY(stream_id,generation,sequence),
 UNIQUE(stream_id,generation,chunk_ordinal,source_ordinal,line_ordinal,module,metric))''',
 'analysis_tracking_order': '''CREATE INDEX IF NOT EXISTS analysis_tracking_order ON analysis_tracking_samples
 (stream_id,generation,module,metric,error,sequence,duration_ns)''',
 'analysis_tracking_finalization': '''CREATE TABLE IF NOT EXISTS analysis_tracking_finalization (
 stream_id TEXT NOT NULL,generation INTEGER NOT NULL,state_json TEXT NOT NULL,state_sha256 TEXT NOT NULL,
 PRIMARY KEY(stream_id,generation))'''
}
SQL['analysis_tracking_revision'] = '''CREATE TABLE IF NOT EXISTS analysis_tracking_revision (
 stream_id TEXT NOT NULL,generation INTEGER NOT NULL,revision INTEGER NOT NULL,PRIMARY KEY(stream_id,generation))'''
for action,rows in (('insert',('NEW',)),('delete',('OLD',)),('update',('OLD','NEW'))):
    statements=' '.join('INSERT INTO analysis_tracking_revision VALUES('+row+'.stream_id,'+row+'.generation,1) '
                        'ON CONFLICT(stream_id,generation) DO UPDATE SET revision=revision+1;' for row in rows)
    SQL['analysis_tracking_'+action+'_revision']='CREATE TRIGGER IF NOT EXISTS analysis_tracking_'+action+'_revision AFTER '+action.upper()+' ON analysis_tracking_samples BEGIN '+statements+' END'

METRICS = ('drive','steering')

def install_schema(db):
    for statement in SQL.values(): db.execute(statement)

def validate_schema(db):
    for name,expected in SQL.items():
        row=db.execute('SELECT sql FROM sqlite_master WHERE name=?',(name,)).fetchone()
        if row is None or row[0] is None or _normalized(row[0])!=_normalized(expected):
            raise ValueError('tracking_work_schema_invalid')

def chain(previous,record):
    return hashlib.sha256(bytes.fromhex(previous)+canonical(record).encode()).hexdigest()

def initial():
    return dict(version=VERSION,count=0,chain=INITIAL_CHAIN,weights={str(i)+':'+m:'0' for i in range(4) for m in METRICS})

def assert_revision(db,sid,generation,work):
    row=db.execute('SELECT revision FROM analysis_tracking_revision WHERE stream_id=? AND generation=?',(sid,generation)).fetchone()
    if (row[0] if row else 0)!=work['count']:raise ValueError('tracking_work_changed_after_commit')

def contribute(db,sid,generation,state,chunk,row,module,metric,error,duration):
    if not math.isfinite(error) or error<0 or type(duration) is not int or not 0<duration<(1<<63):
        raise ValueError('tracking_nonfinite_or_invalid_weight')
    key=str(module)+':'+metric
    total=int(state['weights'][key])+duration
    if total>=(1<<63):raise ValueError('tracking_duration_overflow')
    record=[state['count'],chunk,row['stream_source_ordinal'],row['stream_line_ordinal'],module,metric,float(error),duration]
    db.execute('INSERT INTO analysis_tracking_samples VALUES(?,?,?,?,?,?,?,?,?,?)',(sid,generation,*record))
    state.update(count=state['count']+1,chain=chain(state['chain'],record))
    state['weights'][key]=str(total)

def validation_initial():
    return dict(cursor=-1,count=0,chain=INITIAL_CHAIN,weights={str(i)+':'+m:0 for i in range(4) for m in METRICS},contributions_done=False,final_replay=None)

def validate_step(db,sid,generation,expected,state,limit,deadline,clock):
    assert_revision(db,sid,generation,expected)
    if state['contributions_done']:
        return validate_finalization_step(db,sid,generation,expected,state,limit,deadline,clock)
    rows=db.execute('SELECT sequence,chunk_ordinal,source_ordinal,line_ordinal,module,metric,error,duration_ns '
                    'FROM analysis_tracking_samples WHERE stream_id=? AND generation=? AND sequence>? '
                    'ORDER BY sequence LIMIT ?',(sid,generation,state['cursor'],limit)).fetchall()
    used=0
    for row in rows:
        if clock()>=deadline:break
        record=list(row)
        if record[0]!=state['count'] or record[4] not in range(4) or record[5] not in METRICS or not math.isfinite(record[6]) or record[6]<0 or type(record[7]) is not int or record[7]<=0:
            raise ValueError('tracking_work_invalid')
        state['chain']=chain(state['chain'],record)
        state['weights'][str(record[4])+':'+record[5]]+=record[7]
        state['cursor']=record[0];state['count']+=1;used+=1
    if used==len(rows) and len(rows)<limit:
        if state['count']!=expected['count'] or state['chain']!=expected['chain'] or any(str(value)!=expected['weights'][key] for key,value in state['weights'].items()):
            raise ValueError('tracking_work_checkpoint_mismatch')
        state['contributions_done']=True
        return False
    return False

def _empty_final(work):
    return dict(version=VERSION,sample_chain=work['chain'],sample_count=work['count'],metric_index=0,cursor=None,cumulative='0',quantiles={},complete=False)

def _load(db,sid,generation,work):
    row=db.execute('SELECT state_json,state_sha256 FROM analysis_tracking_finalization WHERE stream_id=? AND generation=?',(sid,generation)).fetchone()
    if row is None:
        return _empty_final(work)
    if len(row[0].encode())>65536 or hashlib.sha256(row[0].encode()).hexdigest()!=row[1]:
        raise ValueError('tracking_finalization_checkpoint_invalid')
    state=json.loads(row[0])
    if state.get('version')!=VERSION or state.get('sample_chain')!=work['chain'] or state.get('sample_count')!=work['count']:
        raise ValueError('tracking_finalization_input_changed')
    if type(state.get('metric_index')) is not int or not 0<=state['metric_index']<=8 or state.get('complete') is not (state['metric_index']==8):
        raise ValueError('tracking_finalization_cursor_invalid')
    if not isinstance(state.get('quantiles'),dict) or set(state['quantiles'])!={str(i//2)+':'+METRICS[i%2] for i in range(state['metric_index'])}:
        raise ValueError('tracking_finalization_quantiles_invalid')
    return state

def _target_reached(state,target):
    if target is None:return False
    if state['metric_index']==target['metric_index'] and state['cursor']==target['cursor']:
        if state!=target:raise ValueError('tracking_finalization_semantic_mismatch')
        return True
    if state['metric_index']>target['metric_index']:raise ValueError('tracking_finalization_cursor_unreachable')
    return False

def _advance(db,sid,generation,work,state,limit,deadline,clock,target=None):
    if _target_reached(state,target):return True
    consumed=0
    while state['metric_index']<8 and consumed<limit and clock()<deadline:
        module,offset=divmod(state['metric_index'],2);metric=METRICS[offset];key=str(module)+':'+metric
        total=int(work['weights'][key])
        if not total:
            state['quantiles'][key]=None
            state.update(metric_index=state['metric_index']+1,cursor=None,cumulative='0')
            consumed+=1
            state['complete']=state['metric_index']==8
            if _target_reached(state,target):return True
            continue
        cursor=state['cursor']
        args=[sid,generation,module,metric]
        where=''
        if cursor is not None:
            where=' AND (error,sequence)>(?,?)';args+=cursor
        args.append(limit-consumed)
        rows=db.execute('SELECT error,sequence,duration_ns FROM analysis_tracking_samples INDEXED BY analysis_tracking_order '
                        'WHERE stream_id=? AND generation=? AND module=? AND metric=?'+where+
                        ' ORDER BY error,sequence LIMIT ?',args).fetchall()
        if not rows:raise ValueError('tracking_quantile_weight_missing')
        for error,sequence,weight in rows:
            if consumed>=limit or clock()>=deadline:break
            consumed+=1
            cumulative=int(state['cumulative'])+weight
            if cumulative>total:raise ValueError('tracking_quantile_weight_invalid')
            state['cumulative']=str(cumulative);state['cursor']=[error,sequence]
            if 100*cumulative>=95*total:
                state['quantiles'][key]=error
                state.update(metric_index=state['metric_index']+1,cursor=None,cumulative='0')
                state['complete']=state['metric_index']==8
                if _target_reached(state,target):return True
                break
            if _target_reached(state,target):return True
    state['complete']=state['metric_index']==8
    if target is not None and state['complete'] and not _target_reached(state,target):
        raise ValueError('tracking_finalization_cursor_unreachable')
    return state['complete'] if target is None else False

def validate_finalization_step(db,sid,generation,work,validation,limit,deadline,clock):
    if validation['final_replay'] is None:
        validation['final_target']=_load(db,sid,generation,work)
        validation['final_replay']=_empty_final(work)
    return _advance(db,sid,generation,work,validation['final_replay'],limit,deadline,clock,validation['final_target'])

def finalize_step(db,sid,generation,work,limit,deadline,clock):
    assert_revision(db,sid,generation,work)
    state=_load(db,sid,generation,work)
    _advance(db,sid,generation,work,state,limit,deadline,clock)
    raw=canonical(state)
    db.execute('INSERT INTO analysis_tracking_finalization VALUES(?,?,?,?) ON CONFLICT(stream_id,generation) '
               'DO UPDATE SET state_json=excluded.state_json,state_sha256=excluded.state_sha256',
               (sid,generation,raw,hashlib.sha256(raw.encode()).hexdigest()))
    return state

def finalized_digest(work,state):
    if not state['complete']:raise ValueError('tracking_quantiles_incomplete')
    return identity([VERSION,work,state])


def validate_catalog_work(db):
    """Coherent backup verification: bounded Python pages, never materialize history."""
    checkpoints=db.execute("SELECT stream_id,generation,state_json,state_sha256 FROM analysis_stream_checkpoints WHERE reducer_id=? ORDER BY stream_id,generation",
                           ('recorded-status-tracking@stream-recorded-status-tracking-1',))
    for sid,generation,raw,digest in checkpoints:
        if len(raw.encode())>1024*1024 or hashlib.sha256(raw.encode()).hexdigest()!=digest:
            raise ValueError('tracking_checkpoint_invalid')
        expected=json.loads(raw)['tracking']['work'];cursor=validation_initial()
        while not validate_step(db,sid,generation,expected,cursor,256,1.,lambda:0.):pass
        # A finalization record can be absent for a still-scanning stream.
        _load(db,sid,generation,expected)
    orphan=db.execute("SELECT 1 FROM analysis_tracking_samples s WHERE NOT EXISTS (SELECT 1 FROM analysis_stream_checkpoints c "
                      "WHERE c.stream_id=s.stream_id AND c.generation=s.generation AND c.reducer_id=?) LIMIT 1",
                      ('recorded-status-tracking@stream-recorded-status-tracking-1',)).fetchone()
    if orphan:raise ValueError('tracking_orphan_work')
    for table in ('analysis_tracking_finalization','analysis_tracking_revision'):
        orphan=db.execute('SELECT 1 FROM '+table+' s WHERE NOT EXISTS (SELECT 1 FROM analysis_stream_checkpoints c '
                          'WHERE c.stream_id=s.stream_id AND c.generation=s.generation AND c.reducer_id=?) LIMIT 1',
                          ('recorded-status-tracking@stream-recorded-status-tracking-1',)).fetchone()
        if orphan:raise ValueError('tracking_orphan_work')
