"""Fixed policy11: recorded status, tracking, and recorded steering dynamics."""
import hashlib
from .analysis import canonical
from .stream_status import RecordedStatusReducer
from .stream_quality import _unique
from . import stream_tracking as tracking
from . import stream_motion_mapper as motion
from . import stream_tracking_store as work
from . import stream_steering as steering

PIPELINE='automatic-reports-11'
BUILD_POLICY='recorded_build_and_configuration_required_1'
SOFT_MAPPER_ERRORS=frozenset(('swerve_array_type_or_length_invalid','swerve_array_value_invalid',
    'connection_type_invalid','module_io_type_unit_or_value_invalid','battery_type_unit_or_value_invalid'))

def _soft_motion_error(reason):
    if reason in SOFT_MAPPER_ERRORS:return True
    if reason.startswith('invalid_motion_field:'):
        field=reason.partition(':')[2]
        return field in motion.FIELDS and field not in motion.SCHEMAS
    return False


class MotionReducer:
    reducer_id='recorded-status-motion'
    version='stream-recorded-status-motion-1'
    pipeline_version=PIPELINE
    @property
    def context_versions(self):
        return {**tracking.TrackingReducer().context_versions,
            'stream_tracking_work_version':work.MOTION_VERSION,
            'stream_motion_build_policy':BUILD_POLICY,
            'stream_steering_version':steering.VERSION,'stream_steering_numerics':steering.NUMERICS,
            'stream_steering_endpoint_policy':steering.ENDPOINT_POLICY,'stream_steering_availability_policy':steering.AVAILABILITY_POLICY}
    def initial(self,context):
        if any(context.get(k)!=v for k,v in self.context_versions.items()):raise ValueError('stream_motion_version_mismatch')
        state=dict(version=self.version,status=RecordedStatusReducer().initial(context),
            tracking=tracking.initial_tracking(context),steering=steering.initial_steering(context))
        state['tracking']['work']=work.initial(work.MOTION_VERSION)
        return state
    def consume_chunk(self,db,sid,gen,state,row,chunk):
        RecordedStatusReducer().consume(db,sid,gen,state['status'],row)
        track=state['tracking'];mapper=track['mapper'];before=(mapper['source'],mapper['boundary'],len(mapper['errors']))
        snapshot=motion.consume(db,sid,gen,mapper,row)
        after=(mapper['source'],mapper['boundary'],len(mapper['errors']))
        for reason in mapper['errors']:
            target=state['steering']['soft_unavailable'] if _soft_motion_error(reason) else state['steering']['unavailable']
            if reason not in target:target.append(reason)
        if snapshot is None:
            if before!=after and row.get('stream_scope')!='before_run':steering.boundary(state['steering'],'source_lifecycle_or_invalid_record')
            return state
        track['cycles']+=1;stamp=snapshot['cycle']['timestamp_ns']
        track['first']=stamp if track['first'] is None else track['first'];track['last']=stamp
        samples,reasons=motion.samples(snapshot,track['context'],track['context']['stream_source_type'])
        for reason in reasons:
            tracking.add_reason(track,reason)
            target=state['steering']['soft_unavailable'] if reason=='motion_state_missing_while_enabled' else state['steering']['unavailable']
            if reason not in target:target.append(reason)
        for sample in samples:
            tracking.consume_sample(db,sid,gen,track,sample,row,chunk)
            steering.consume_sample(state['steering'],sample,
                lambda module,error,ns:work.contribute(db,sid,gen,track['work'],chunk,row,module,'recorded_steering',error,ns),
                lambda module,key:_unique(db,sid,gen,'steering-record-pair-'+str(module),key))
        return state
    def validation_initial(self):return work.validation_initial(work.MOTION_VERSION)
    def validate_step(self,db,sid,gen,state,validation,limit,deadline,clock):
        return work.validate_step(db,sid,gen,state['tracking']['work'],validation,limit,deadline,clock)
    def finalize_step(self,db,sid,gen,state,limit,deadline,clock):
        if not state['steering']['flushed']:
            steering.flush(state['steering'])
            raw=canonical(state)
            if len(raw.encode())>1024*1024:raise ValueError('stream_steering_checkpoint_resource_limit')
            db.execute('UPDATE analysis_stream_checkpoints SET state_json=?,state_sha256=? WHERE stream_id=? AND generation=? AND reducer_id=?',
                (raw,hashlib.sha256(raw.encode()).hexdigest(),sid,gen,self.reducer_id+'@'+self.version))
        return work.finalize_step(db,sid,gen,state['tracking']['work'],limit,deadline,clock)
    def publication_check(self,db,sid,gen,state):work.assert_revision(db,sid,gen,state['tracking']['work'])
    def finish(self,state,descriptor):
        output=RecordedStatusReducer().finish(state['status'],descriptor)
        output['stream_result_format']='stream-motion-results-1'
        output['checks']['swerve-tracking']=tracking.finish_tracking(state['tracking'],descriptor,state['finalization'])
        output['checks']['recorded-steering-observation']=steering.finish_steering(state['steering'],descriptor,state['finalization']['quantiles'])
        return output
