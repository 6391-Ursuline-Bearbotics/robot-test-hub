"""Pipeline-10 motion mapping; mapper-1 and status reducer policy stay untouched."""
import math
from .analysis import identity
from .swerve import ROBOT_FIELDS, MODULE_POSITIONS, SAMPLE_PROFILE
from .stream_quality import _unique
from .system_status import _ns, _record

VERSION='stream-motion-mapper-1'
SCHEMAS={'/.schema/struct:Rotation2d':'double value',
         '/.schema/struct:SwerveModuleVelocity':'double velocity;Rotation2d angle'}
CONNECTIONS=tuple(f'/Drive/Module{i}/'+name for i in range(4) for name in ('DriveConnected','TurnConnected','TurnEncoderConnected'))
MEASUREMENTS=tuple(f'/Drive/Module{i}/'+name for i in range(4) for name in ('DriveVelocityRadPerSec','TurnPosition'))
MEASUREMENT_CONTRACT='alpha7-module-io-wheel-angular-velocity-times-pinned-radius-and-rotation2d-1'
FIELDS=frozenset(MEASUREMENTS)|frozenset(SCHEMAS)|frozenset(ROBOT_FIELDS.values())|frozenset(CONNECTIONS)|frozenset(('/SystemStats/BatteryVoltage',))

def initial():
    return dict(source=None,held={},index=-1,cycle_index=-1,previous_cycle=None,boundary=0,errors=[],after_run=False,pre_run_invalidations=0,measurement_array_seen=False)

def _problem(state,reason,row=None):
    if row is not None and row.get('stream_scope')=='before_run':
        state['pre_run_invalidations']+=1;return
    if reason not in state['errors']:state['errors'].append(reason)

def _clear(state,entry=None,field=None):
    for path,held in list(state['held'].items()):
        if entry is None and field is None or held.get('entry_id')==entry or path==field:
            state['held'].pop(path,None)
    state['boundary']+=1

def consume(db,sid,gen,state,row):
    source=row.get('source_sha256')
    if type(source) is not str or len(source)!=64:
        _problem(state,'source_reference_invalid');_clear(state);return None
    if source!=state['source']:
        if not _unique(db,sid,gen,'motion-source',source):_problem(state,'noncontiguous_source_order')
        _clear(state);state.update(source=source,index=-1,cycle_index=-1,previous_cycle=None,after_run=False,measurement_array_seen=False)
    field=row.get('field');kind=row.get('kind')
    if kind in ('observation','control'):
        if field not in FIELDS and not any(v['entry_id']==row.get('entry_id') for v in state['held'].values()):return None
        error=_record(row)
        if error or row['record_index']<=state['index']:
            _problem(state,'source_record_or_lifecycle_invalid');_clear(state);return None
        state['index']=row['record_index']
        entry,generation=row['entry_id'],row['entry_generation']
        retired_prefix=f'{source}:{entry:019d}:'
        retired=db.execute('SELECT 1 FROM analysis_stream_unique WHERE stream_id=? AND generation=? AND namespace=? '
                           'AND key>=? AND key<? LIMIT 1',(sid,gen,'motion-retired',retired_prefix+f'{generation:019d}',retired_prefix+'z')).fetchone()
        conflict=any(v['entry_id']==entry and (generation<v['entry_generation'] or generation==v['entry_generation'] and path!=field)
                     for path,v in state['held'].items())
        if kind=='control':
            if row.get('control') not in ('start','finish','metadata'):
                _problem(state,'entry_lifecycle_invalid');_clear(state);return None
            _clear(state,entry,field)
            if row['control']=='finish':
                db.execute('INSERT OR IGNORE INTO analysis_stream_unique VALUES(?,?,?,?)',(sid,gen,'motion-retired',retired_prefix+f'{generation:019d}'))
            if conflict:_problem(state,'entry_generation_conflict')
            return None
        if field not in FIELDS:return None
        if field==ROBOT_FIELDS['measured']:state['measurement_array_seen']=True
        if retired or conflict:
            _problem(state,'entry_generation_conflict');_clear(state,entry,field);return None
        old=state['held'].get(field)
        if old and ((old['entry_id'],old['entry_generation'])!=(entry,generation) or _ns(row['record_timestamp_ns'])<_ns(old['record_timestamp_ns'])):
            _clear(state,field=field)
        if state['after_run'] or row.get('stream_scope')=='after_run':return None
        if row.get('validity')!='valid':
            _problem(state,'invalid_motion_field:'+field,row);_clear(state,field=field);return None
        # Fixed field vocabulary and at most four struct elements bound held state.
        if field in ROBOT_FIELDS.values():
            value=row.get('value')
            if row.get('type')=='struct:SwerveModuleVelocity[]' and value==[] and field==ROBOT_FIELDS['commands']:
                # The source robot deliberately records [] while disabled. It
                # invalidates held demand; the cycle's enabled authority decides
                # whether missing motion is an availability error.
                if row.get('stream_scope')=='before_run':state['pre_run_invalidations']+=1
                _clear(state,field=field);return None
            if row.get('type')!='struct:SwerveModuleVelocity[]' or not isinstance(value,list) or len(value)!=4:
                _problem(state,'swerve_array_type_or_length_invalid',row);_clear(state,field=field);return None
            for item in value:
                if not isinstance(item,dict) or not isinstance(item.get('angle'),dict) or any(type(v) not in (int,float) or not math.isfinite(v) for v in (item.get('velocity'),item['angle'].get('value'))):
                    _problem(state,'swerve_array_value_invalid',row);_clear(state,field=field);return None
        elif field in SCHEMAS:
            value=row.get('value')
            if row.get('type')!='structschema' or not isinstance(value,str) or ''.join(value.split()).rstrip(';')!=''.join(SCHEMAS[field].split()).rstrip(';'):
                _problem(state,'recorded_swerve_schema_unsupported',row);_clear(state,field=field);return None
        elif field in CONNECTIONS and (row.get('type')!='boolean' or type(row.get('value')) is not bool):
            _problem(state,'connection_type_invalid',row);_clear(state,field=field);return None
        elif field in MEASUREMENTS:
            value=row.get('value')
            valid=(row.get('type')=='double' and type(value) in (int,float) and math.isfinite(value)) if field.endswith('DriveVelocityRadPerSec') else (row.get('type')=='struct:Rotation2d' and isinstance(value,dict) and type(value.get('value')) in (int,float) and math.isfinite(value['value']))
            expected_unit='radians per second' if field.endswith('DriveVelocityRadPerSec') else 'radians'
            if not valid or row.get('category')!='input' or row.get('unit') not in (None,expected_unit):
                _problem(state,'module_io_type_unit_or_value_invalid',row);_clear(state,field=field);return None
        elif field=='/SystemStats/BatteryVoltage':
            if row.get('type')!='double' or row.get('unit')!='volts' or row.get('category')!='input' or type(row.get('value')) not in (int,float) or not math.isfinite(row['value']):
                _problem(state,'battery_type_unit_or_value_invalid',row);_clear(state,field=field);return None
        state['held'][field]={k:row[k] for k in ('field','type','validity','value','record_index','entry_id','entry_generation','record_timestamp_ns','cycle_timestamp_ns','source_sha256') if k in row}
        return None
    if kind!='cycle':return None
    if row.get('stream_scope')=='after_run':state['after_run']=True
    if row.get('stream_scope')!='in_run' or state['after_run']:return None
    stamp=_ns(row['timestamp_ns']);index=row.get('timestamp_record_index')
    if type(index) is not int or index<=state['cycle_index'] or state['previous_cycle'] is not None and stamp<=int(state['previous_cycle']) or not _unique(db,sid,gen,'motion-cycle',[source,stamp]):
        _problem(state,'source_cycle_order_invalid');_clear(state);return None
    state['cycle_index']=index;state['previous_cycle']=str(stamp)
    return dict(cycle=row,held=state['held'],boundary=state['boundary'],measurement_array_seen=state['measurement_array_seen'])

def samples(snapshot,context,source_type):
    row=snapshot['cycle'];held=snapshot['held'];stamp=_ns(row['timestamp_ns']);aliases=row.get('aliases',{})
    reasons=[];plan=context.get('analysis_plan') or {}
    config=aliases.get('configuration_sha256',{})
    if config.get('validity')!='valid' or config.get('value')!=plan.get('config_hash'):
        reasons.append('recorded_configuration_hash_unavailable_or_changed')
    build=aliases.get('source_sha256',{})
    if build.get('validity')=='valid' and build.get('value')!=plan.get('build_hash'):
        reasons.append('recorded_build_hash_mismatch_or_changed')
    runtime=aliases.get('runtime_mode',{})
    if runtime.get('validity')!='valid' or runtime.get('value')!=context.get('runtime_mode'):
        reasons.append('recorded_runtime_mode_unknown_or_changed')
    enabled=aliases.get('enabled',{})
    known=aliases.get('state_known',{});authoritative=aliases.get('status_enabled',{})
    if known.get('validity')=='valid' and known.get('value') is True:
        if enabled.get('validity')=='valid' and authoritative.get('validity')=='valid' and enabled.get('value')!=authoritative.get('value'):
            reasons.append('enabled_status_disagreement')
        enabled=authoritative
    enabled_value=enabled.get('value') if enabled.get('validity')=='valid' and type(enabled.get('value')) is bool else None
    if enabled_value is None:reasons.append('enabled_status_unknown_or_invalid')
    ideal=plan.get('ideal_simulation_cycle_policy') is True and source_type=='simulation'
    outputs=[]
    for module,position in enumerate(MODULE_POSITIONS):
        command=held.get(ROBOT_FIELDS['commands']);measured=held.get(ROBOT_FIELDS['measured'])
        velocity=held.get(f'/Drive/Module{module}/DriveVelocityRadPerSec')
        turn=held.get(f'/Drive/Module{module}/TurnPosition')
        array_measurement=snapshot['measurement_array_seen']
        valid=all(field in held for field in SCHEMAS) and command is not None
        a=command['value'][module] if command else None
        radius=context.get('wheel_radius_m')
        if array_measurement:
            valid=valid and measured is not None
            b=measured['value'][module] if measured else None
            measurement_rows=[measured] if measured else []
            measurement_kind='recorded_measured_velocity_array'
        else:
            source_selected=context.get('stream_motion_measurement_source')=='module_io_if_array_absent'
            if not source_selected and (velocity is not None or turn is not None):
                reasons.append('explicit_module_io_measurement_policy_required')
            valid=valid and source_selected and velocity is not None and turn is not None and type(radius) in (int,float) and math.isfinite(radius) and 0<radius<=1
            b=dict(velocity=velocity['value']*radius,angle=turn['value']) if valid else None
            if valid and not math.isfinite(b['velocity']):valid=False
            measurement_rows=[value for value in (velocity,turn) if value]
            measured=velocity
            measurement_kind=MEASUREMENT_CONTRACT
        if valid:valid=all(_ns(value['record_timestamp_ns'])<=stamp for value in [command,*measurement_rows])
        if enabled_value is True and not valid:reasons.append('motion_state_missing_while_enabled')
        refs=[held.get(f'/Drive/Module{module}/'+name) for name in ('DriveConnected','TurnConnected','TurnEncoderConnected')]
        connected=all(value and value.get('value') is True and _ns(value['record_timestamp_ns'])<=stamp for value in refs)
        reference=dict(source_hash=row['source_sha256'],cycle_record_index=row['timestamp_record_index'],
                       command_record_index=command.get('record_index') if command else None,
                       command_entry_id=command.get('entry_id') if command else None,
                       command_entry_generation=command.get('entry_generation') if command else None,
                       measured_entry_id=measured.get('entry_id') if measured else None,
                       measured_entry_generation=measured.get('entry_generation') if measured else None,
                       measured_record_index=measured.get('record_index') if measured else None,
                       measurement_contract=measurement_kind,wheel_radius_m=radius if not array_measurement else None,
                       module_measurement_references=[{key:value.get(key) for key in ('field','record_index','entry_id','entry_generation','record_timestamp_ns','cycle_timestamp_ns')} for value in measurement_rows],
                       command_record_timestamp_ns=command.get('record_timestamp_ns') if command else None,
                       measured_record_timestamp_ns=measured.get('record_timestamp_ns') if measured else None,
                       connection_record_indices=[value.get('record_index') if value else None for value in refs],
                       connection_entry_identities=[[value.get('entry_id'),value.get('entry_generation')] if value else None for value in refs],mapping_version=VERSION)
        battery=held.get('/SystemStats/BatteryVoltage',{})
        outputs.append(dict(kind='swerve_sample',module_index=module,module_id=context.get('component_ids',{}).get(position),module_position=position,
            timestamp_ns=stamp,enabled=enabled_value,connected=connected,fresh=ideal,validity='valid' if valid else 'missing_or_invalid',
            command_speed_mps=a['velocity'] if valid else None,measured_speed_mps=b['velocity'] if valid else None,
            command_angle_rad=a['angle']['value'] if valid else None,measured_angle_rad=b['angle']['value'] if valid else None,
            command_timestamp_ns=stamp if ideal else None,measurement_timestamp_ns=stamp if ideal else None,
            command_stage='final_io_optimized_cosine_desaturated',sample_profile=SAMPLE_PROFILE,
            speed_unit='meters per second',angle_unit='radians',battery_voltage_v=battery.get('value'),
            source_reference=reference,boundary=[row['source_sha256'],snapshot['boundary']]))
    return outputs,reasons
