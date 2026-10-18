"""Bounded source/lifecycle mapper shared by streamed recorded-state reducers."""
from . import phoenix_status as phoenix
from . import system_status as system
from .analysis import CONNECTION_FIELDS
from .stream_quality import _unique

VERSION='stream-cycle-mapper-1'
FIELDS=phoenix.FIELDS | system.FIELDS | CONNECTION_FIELDS | frozenset(('/DriverStation/Enabled',))
MAX_GAP_NS=250000000


def initial(context):
    return dict(version=VERSION,source=None,held={},epochs={},index=-1,cycle_index=-1,
        previous_cycle=None,global_cycle=None,after_run=False,errors=[],boundary=0)


def _problem(state,reason):
    # Reasons belong to a fixed vocabulary plus the fixed required-field set.
    if reason not in state['errors']:state['errors'].append(reason)


def _clear(state,entry=None,field=None):
    for path,old in list(state['held'].items()):
        if entry is None and field is None or old.get('entry_id')==entry or path==field:
            state['held'].pop(path,None)
            state['epochs'][path]=state['epochs'].get(path,0)+1
    if field in FIELDS and field not in state['held']:
        # Even an invalid/missing field lifecycle has a bounded epoch scalar.
        if field not in state['epochs']:state['epochs'][field]=1


def consume(db,stream_id,generation,state,row):
    """Return one eligible cycle snapshot, or None. No history-sized Python state."""
    source=row.get('source_sha256')
    if type(source) is not str or len(source)!=64 or any(c not in '0123456789abcdef' for c in source):
        _problem(state,'source_reference_unavailable');_clear(state);state['boundary']+=1;return None
    if source!=state['source']:
        if not _unique(db,stream_id,generation,'mapper-source',source):_problem(state,'noncontiguous_source_order_unqualified')
        _clear(state);state.update(source=source,index=-1,cycle_index=-1,previous_cycle=None,after_run=False)
        state['boundary']+=1
    kind=row.get('kind');field=row.get('field')
    if kind in ('observation','control'):
        # Irrelevant controls can invalidate a required live entry, but cannot grow maps.
        if field not in FIELDS and not any(v.get('entry_id')==row.get('entry_id') for v in state['held'].values()):return None
        error=system._record(row)
        index=row.get('record_index')
        if error or index<=state['index']:
            _problem(state,error or 'source_record_order_unqualified');_clear(state);state['boundary']+=1;return None
        state['index']=index
        entry,entry_generation=row['entry_id'],row['entry_generation']
        key=f'{source}:{entry:019d}:'
        retired=db.execute('SELECT 1 FROM analysis_stream_unique WHERE stream_id=? AND generation=? AND namespace=? AND key>=? AND key<? LIMIT 1',
            (stream_id,generation,'mapper-retired',key+f'{entry_generation:019d}',key+'z')).fetchone() is not None
        conflict=any(v['entry_id']==entry and (entry_generation<v['entry_generation'] or entry_generation==v['entry_generation'] and path!=field)
            for path,v in state['held'].items())
        if kind=='control':
            if row.get('control') not in ('start','finish','metadata'):
                _problem(state,'entry_lifecycle_unavailable');_clear(state);state['boundary']+=1;return None
            if conflict:_problem(state,'entry_generation_or_identity_conflict')
            _clear(state,entry,field)
            if row['control']=='finish':
                db.execute('INSERT OR IGNORE INTO analysis_stream_unique VALUES(?,?,?,?)',
                    (stream_id,generation,'mapper-retired',key+f'{entry_generation:019d}'))
            return None
        if field not in FIELDS:return None
        if retired or conflict:
            _problem(state,'entry_generation_or_identity_conflict');_clear(state,entry,field);return None
        try:
            system._ns(row['record_timestamp_ns'])
            if row.get('cycle_timestamp_ns') is not None:system._ns(row['cycle_timestamp_ns'])
        except ValueError:
            _problem(state,'record_cycle_timestamp_invalid');_clear(state,entry,field);return None
        if state['after_run'] or row.get('stream_scope')=='after_run':return None
        previous=state['held'].get(field)
        if field in system.FIELDS and previous and (previous['entry_id'],previous['entry_generation'])==(entry,entry_generation) and system._ns(row['record_timestamp_ns'])<=system._ns(previous['record_timestamp_ns']):
            _problem(state,'source_observation_clock_nonadvancing:'+field);_clear(state,entry,field);return None
        if previous and (previous['entry_id'],previous['entry_generation'])!=(entry,entry_generation):_clear(state,field=field)
        for path,value in list(state['held'].items()):
            if path!=field and value['entry_id']==entry:_clear(state,field=path)
        copied={key:row[key] for key in ('kind','source_sha256','field','type','unit','category','validity','value','record_index','entry_id','entry_generation','record_timestamp_ns','cycle_timestamp_ns') if key in row}
        copied['before_run']=row.get('stream_scope')!='in_run'
        copied['stream_scope']=row.get('stream_scope','unscoped')
        if field in phoenix.FIELDS and copied.get('type')=='double' and type(copied.get('value')) is int and abs(copied['value'])>=10**19:copied['validity']='invalid'
        if field in system.FIELDS:
            _,error=system._value(copied)
            if error:_problem(state,error+':'+field);_clear(state,field=field);return None
        if field in phoenix.FIELDS:
            name=field.split('/Phoenix',1)[1]
            specification=phoenix.COMMON.get(name)
            if specification is None:
                signal=next(signal for signal in phoenix.SIGNALS if name.startswith(signal))
                suffix=name[len(signal):];specification=phoenix.SIGNAL_FIELDS[suffix]
                if suffix=='RawValue':specification=(specification[0],phoenix.SIGNALS[signal])
            _,_,error=phoenix._read({field:copied},field,specification)
            if error:_clear(state,field=field)
        state['held'][field]=copied
        return None
    if kind!='cycle':return None
    try:stamp=system._ns(row['timestamp_ns'])
    except (ValueError,KeyError):
        _problem(state,'cycle_timestamp_invalid');state['boundary']+=1;return None
    if row.get('stream_scope')=='after_run':state['after_run']=True
    if row.get('stream_scope')!='in_run' or state['after_run']:return None
    index=row.get('timestamp_record_index')
    if type(index) is not int or not 0<=index<(1<<63) or index<=state['cycle_index']:
        _problem(state,'cycle_record_reference_invalid');state['boundary']+=1;return None
    state['cycle_index']=index
    previous=state['previous_cycle']
    if previous is not None and stamp<=int(previous) or not _unique(db,stream_id,generation,'mapper-cycle',str(stamp)):
        _problem(state,'source_cycle_order_or_overlap_unqualified');state['boundary']+=1;return None
    if state['global_cycle'] is not None and stamp<=int(state['global_cycle']):
        _problem(state,'overlapping_source_clock_intervals');state['boundary']+=1
    if previous is not None and stamp-int(previous)>MAX_GAP_NS:
        _problem(state,'logger_cycle_gap');state['boundary']+=1
    state['global_cycle']=state['previous_cycle']=str(stamp)
    return dict(cycle=row,held=state['held'],epochs=state['epochs'],boundary=state['boundary'])
