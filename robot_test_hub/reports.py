"""Bounded automatic recording-quality reports, pinned to immutable run evidence."""
import json
import copy
from dataclasses import replace

from .analysis import Analyzer, Declaration, Evidence, Runner, data_quality, result, CONNECTION_FIELDS
from .importer import Importer
from .runs import canonical
from .wpilog import PROFILE
from .analysis_plan import review_snapshot, resolve_plan
from .swerve import (adapt_robot_swerve, swerve_analyzer, swerve_tracking, ROBOT_FIELDS,
                     ROBOT_MAPPING_VERSION, MODULE_POSITIONS)

VERSION = 'automatic-reports-7'
LEGACY_VERSION = 'automatic-reports-5'
from .phoenix_status import FIELDS as PHOENIX_FIELDS, phoenix_status_analyzer

MAX_ROWS = 50000
STREAM_VERSION = 'automatic-reports-8'
CURRENT_STREAM_VERSION = 'automatic-reports-9'  # Fixed historical status policy.
TRACKING_STREAM_VERSION = 'automatic-reports-10'
MOTION_STREAM_VERSION = 'automatic-reports-11'
DEFAULT_STREAM_VERSION = MOTION_STREAM_VERSION
STREAM_SELECTION = 'full_source_scoped_rows_1'
STREAM_FRAMEWORK = 'stream-analysis-1'
STREAM_DISPATCH_SQL_STEP_LIMIT = 5_000_000
STREAM_DISPATCH_SQL_STEP_INTERVAL = 1000


def _quality(evidence, configuration):
    if evidence.context.get('row_limit_reached'):
        return result('insufficient_data', unavailable=['report_resource_limit_reached'])
    output = data_quality(replace(evidence,rows=tuple(r for r in evidence.rows if not r.get('before_run'))), configuration)
    nonadvancing=evidence.context.get('source_nonadvancing_cycles',[])
    if nonadvancing:
        output['outcome']='finding'
        output['findings'].append({'kind':'nonadvancing_source_cycle_timestamps',
            'severity':'warning','observed_behavior':'Source cycles did not advance before chronological merge',
            'count':len(nonadvancing),'source_references':nonadvancing,
            'next_check':'Inspect source clock and logging order before interpreting control metrics'})
    output['unavailable'].extend(['physical_sensor_freshness_not_qualified', 'usb_write_health_not_qualified'])
    return output


def _swerve(evidence,configuration):
    reasons=list(evidence.context.get('analysis_plan_unavailable',[]))
    if evidence.context.get('row_limit_reached'):reasons.append('report_resource_limit_reached')
    if evidence.context.get('source_nonadvancing_cycles'):reasons.append('source_cycle_order_unqualified')
    reasons.extend(evidence.context.get('swerve_source_hazards',[]))
    if reasons:return result('insufficient_data',unavailable=sorted(set(reasons)),coverage={'scope':'not_evaluated'})
    plan=evidence.context['analysis_plan']
    ideal=plan['ideal_simulation_cycle_policy'] and evidence.source_type=='simulation'
    try:
        mapped=adapt_robot_swerve(evidence,{i:evidence.context['component_ids'][position]
            for i,position in enumerate(MODULE_POSITIONS)},ideal_simulation_cycle_policy=ideal)
    except ValueError:
        return result('unsupported',unavailable=['recorded_swerve_schema_or_mapping_unsupported'])
    lo,hi=int(evidence.context['start_monotonic_ns']),int(evidence.context['end_monotonic_ns'])
    for position in MODULE_POSITIONS:
        stamps=[r['timestamp_ns'] for r in mapped.rows if r.get('kind')=='swerve_sample' and r['module_position']==position]
        if not stamps or min(stamps)!=lo or max(stamps)!=hi:
            return result('insufficient_data',unavailable=['whole_run_swerve_state_coverage_required'])
    output=swerve_tracking(mapped,configuration)
    output['unavailable'].extend(mapped.context.get('swerve_mapping_unavailable',[]))
    if plan['ideal_simulation_cycle_policy'] and not ideal:
        output['unavailable'].append('ideal_simulation_policy_not_applicable_to_source')
    if output['coverage'].get('evaluated_modules')!=4:
        output['outcome']='insufficient_data';output['unavailable'].append('all_four_modules_require_qualified_coverage')
    if mapped.context.get('swerve_mapping_unavailable'):
        output['outcome']='insufficient_data'
    if plan.get('approved_baseline_id') is not None and len(output['coverage'].get('baseline_comparisons',[]))!=8:
        output['outcome']='insufficient_data';output['unavailable'].append('explicit_selected_baseline_not_evaluated')
    output['coverage']['physical_acquisition_freshness_qualified']=False
    output['coverage']['ideal_simulation_cycle_policy_applied']=ideal
    output['unavailable']=sorted(set(output['unavailable']))
    return output


def _steering(evidence, configuration):
    """Observe recorded logger dynamics without inventing physical sample freshness."""
    from .steering import steering_observation
    reasons=list(evidence.context.get('analysis_plan_unavailable',[]))
    if evidence.context.get('analysis_plan') is None:reasons.append('analysis_plan_missing_or_ambiguous')
    components=evidence.context.get('component_ids',{})
    if set(components)!=set(MODULE_POSITIONS) or len(set(components.values()))!=4:
        reasons.append('four_distinct_physical_components_required')
    if evidence.context.get('row_limit_reached'):reasons.append('report_resource_limit_reached')
    if evidence.context.get('source_nonadvancing_cycles'):reasons.append('source_cycle_order_unqualified')
    reasons.extend(evidence.context.get('swerve_source_hazards',[]))
    if reasons:return result('insufficient_data',unavailable=sorted(set(reasons)),coverage={'scope':'not_evaluated','physical_acquisition_freshness_qualified':False})
    try:
        mapped=adapt_robot_swerve(evidence,{i:components[position] for i,position in enumerate(MODULE_POSITIONS)},ideal_simulation_cycle_policy=False)
    except ValueError:
        return result('unsupported',unavailable=['recorded_swerve_schema_or_mapping_unsupported'])
    lo,hi=int(evidence.context['start_monotonic_ns']),int(evidence.context['end_monotonic_ns'])
    for position in MODULE_POSITIONS:
        stamps=[r['timestamp_ns'] for r in mapped.rows if r.get('kind')=='swerve_sample' and r['module_position']==position]
        if not stamps or min(stamps)!=lo or max(stamps)!=hi:
            return result('insufficient_data',unavailable=['whole_run_swerve_state_coverage_required'],coverage={'scope':'not_evaluated','physical_acquisition_freshness_qualified':False})
    output=steering_observation(mapped,configuration)
    mapping_reasons=mapped.context.get('swerve_mapping_unavailable',[])
    output['unavailable']=sorted(set(output['unavailable']+mapping_reasons))
    if mapping_reasons:output['outcome']='insufficient_data'
    output['coverage']['physical_acquisition_freshness_qualified']=False
    output['coverage']['ideal_simulation_cycle_policy_applied']=False
    return output


ROW_SELECTION_V5 = 'source_order_required_schemas_lifecycle_held_state_phoenix_sdk_observations_and_run_cycles'
ROW_SELECTION = 'source_order_required_schemas_lifecycle_held_state_phoenix_sdk_system_status_observations_and_run_cycles'
SELECTOR_POLICIES = {LEGACY_VERSION: ROW_SELECTION_V5, 'automatic-reports-6': ROW_SELECTION, VERSION: ROW_SELECTION}


def selector_policy(pipeline_version, row_selection):
    """Accept only an exact saved selector/version pair; never upgrade old rows."""
    if type(pipeline_version) is not str or pipeline_version not in SELECTOR_POLICIES or SELECTOR_POLICIES[pipeline_version] != row_selection:
        raise ValueError('unsupported_report_selector')
    fields = set(ROBOT_FIELDS.values()) | CONNECTION_FIELDS | PHOENIX_FIELDS
    if pipeline_version in ('automatic-reports-6', 'automatic-reports-7'):
        from .system_status import FIELDS
        fields |= set(FIELDS)
    return fields


def analyzers(*, pipeline_version=None):
    # Current candidate evaluator versions may deliberately change independently
    # of selectors. Explicit historical generation is limited to known policies.
    if pipeline_version is not None and pipeline_version not in SELECTOR_POLICIES:
        raise ValueError('unsupported_report_selector')
    version = VERSION if pipeline_version is None else pipeline_version
    selected = [Analyzer(Declaration('recording-quality', version, (PROFILE,)), _quality),
                Analyzer(swerve_analyzer((PROFILE,)).declaration,_swerve), phoenix_status_analyzer()]
    if version != LEGACY_VERSION:
        from .system_status import system_status_analyzer
        selected.append(system_status_analyzer())
    if version == 'automatic-reports-7':
        from .steering import steering_analyzer
        selected.append(Analyzer(steering_analyzer().declaration,_steering))
    return selected


DERIVED_CONTEXT_FIELDS = ('row_limit_reached', 'source_nonadvancing_cycles',
    'recorded_configuration_sha256', 'recorded_build_sha256', 'build_context_basis',
    'swerve_source_hazards')


class SelectionLimit(ValueError):
    pass


def select_rows(run, jobs, manifests, datasets, *, max_rows=MAX_ROWS, plan=None, max_bytes=None,
                pipeline_version=None, row_selection=None):
    """Pure versioned selector; datasets follow the supplied job order."""
    version = VERSION if pipeline_version is None else pipeline_version
    selection = SELECTOR_POLICIES.get(version) if row_selection is None else row_selection
    fields = selector_policy(version, selection)
    # Derived evidence checks are part of the final job identity. Runner
    # reuses immutable results only after the bounded source scan completes.
    context = {}
    normalized_bytes = 2
    rows, cycles, overflow = [], {}, False
    nonadvancing=[]
    hazards=[];scanned=0;logged_builds=set();logged_configs=set();config_missing=False
    lo, hi = int(run['start_monotonic_ns']), int(run['end_monotonic_ns'])
    ranges=[]
    for job in jobs:
        manifest=manifests[job['id']]
        if manifest.get('unsupported_types'):hazards.append('unsupported_source_types_present')
        first,last=int(manifest['first_cycle_timestamp_ns']),int(manifest['last_cycle_timestamp_ns'])
        a,b=max(lo,first),min(hi,last)
        if a<=b:
            if any(max(a,c)<=min(b,d) for c,d in ranges):hazards.append('overlapping_source_cycle_intervals')
            ranges.append((a,b))
    for job, dataset in zip(jobs, datasets):
        previous_cycle=None
        for row in dataset:
            scanned+=1
            if scanned>max_rows*5:overflow=True;break
            if row.get('kind') not in ('cycle','observation','sample','control'):
                continue
            timestamp = row.get('timestamp_ns', row.get('cycle_timestamp_ns'))
            stamp = int(timestamp) if timestamp is not None else None
            # Inspect the complete bounded source order, including records
            # after the requested run; a later regression must stay visible.
            if row['kind']=='cycle':
                if previous_cycle is not None and stamp<=previous_cycle:
                    nonadvancing.append({'source_sha256':job['artifact_sha256'],
                        'record_index':row.get('record_index'),'timestamp_ns':str(stamp),
                        'previous_timestamp_ns':str(previous_cycle)})
                previous_cycle=stamp
            if stamp is not None and stamp>hi:continue
            before=stamp is None or stamp<lo
            if row['kind'] in ('cycle','sample') and before:continue
            if row['kind']=='observation':
                relevant=(row.get('field') in fields or row.get('type')=='structschema')
                if not relevant and (before or row.get('validity')=='valid'):continue
            if row['kind'] == 'cycle':
                aliases = {k:v.get('value') for k,v in row.get('aliases',{}).items()}
                if stamp in cycles:
                    hazards.append('duplicate_source_cycle_timestamp')
                    if cycles[stamp] != aliases:
                        overflow = True  # Conflicting overlap is not assessable.
                    continue
                cycles[stamp] = aliases
                refs=row.get('aliases',{})
                config=refs.get('configuration_sha256',{})
                if config.get('validity')=='valid' and isinstance(config.get('value'),str):logged_configs.add(config['value'])
                else:config_missing=True
                build=refs.get('source_sha256',{})
                if build.get('validity')=='valid' and isinstance(build.get('value'),str):logged_builds.add(build['value'])
                runtime=refs.get('runtime_mode',{})
                if runtime.get('validity')!='valid' or runtime.get('value')!=run['runtime_mode']:
                    hazards.append('recorded_runtime_mode_unknown_or_changed')
                known=refs.get('state_known',{})
                if known.get('validity')=='valid' and known.get('value') is True:
                    authoritative=refs.get('status_enabled',{})
                    ordinary=refs.get('enabled',{})
                    if (ordinary.get('validity')=='valid' and authoritative.get('validity')=='valid'
                            and ordinary.get('value')!=authoritative.get('value')):hazards.append('enabled_status_disagreement')
                    row={**row,'aliases':{**refs,'enabled':authoritative}}
            if len(rows) >= max_rows:
                overflow = True
                break
            selected = {**row, 'source_sha256':job['artifact_sha256'],'before_run':before}
            if max_bytes is not None:
                normalized_bytes += len(canonical(selected).encode('utf-8')) + 1
                if normalized_bytes > max_bytes:
                    raise SelectionLimit('selected_input_resource_limit')
            rows.append(selected)
        if overflow:
            break
    # Keep source lifecycle/observation order; presentation sorting must not
    # turn overlapping or nonadvancing control evidence into healthy samples.
    if overflow:
        context['row_limit_reached'] = True
    if nonadvancing:
        context['source_nonadvancing_cycles']=nonadvancing
    context['recorded_configuration_sha256']=sorted(logged_configs)
    context['recorded_build_sha256']=sorted(logged_builds)
    context['build_context_basis']='recorded_alias_and_human_plan' if logged_builds else 'human_declared_build_unverified'
    if plan:
        if config_missing or not logged_configs:hazards.append('recorded_configuration_hash_unavailable')
        elif logged_configs!={plan['config_hash']}:hazards.append('recorded_configuration_hash_mismatch_or_changed')
        if logged_builds and logged_builds!={plan['build_hash']}:hazards.append('recorded_build_hash_mismatch_or_changed')
    context['swerve_source_hazards']=sorted(set(hazards))
    return rows, context


def generate(root, db, document, *, max_rows=MAX_ROWS, pipeline_version=None):
    """Read one run at a time. Never infer hardware origin from transfer transport.

    Valid observations are streamed past: the default check only counts invalid
    observations and checks cycle coverage. It does not assess sensor health.
    Large/problematic runs produce explicit unavailable reports, never truncated
    successful health reports. Raw originals and complete imported datasets stay.
    """
    if pipeline_version in (STREAM_VERSION,CURRENT_STREAM_VERSION,TRACKING_STREAM_VERSION,MOTION_STREAM_VERSION):
        return schedule_stream_reports(root,db,document,pipeline_version=pipeline_version)
    if type(max_rows) is not int or max_rows < 2:
        raise ValueError('max_rows must be at least two')
    version = VERSION if pipeline_version is None else pipeline_version
    selection = SELECTOR_POLICIES.get(version)
    selector_policy(version, selection)
    importer, runner = Importer(root, db), Runner(db)
    snapshot=review_snapshot(db)
    reports = []
    for run in document['runs']:
        jobs = [importer.get_job(j) for j in run['segment_ids']]
        manifests={j['id']:importer.read_manifest(j['id']) for j in jobs}
        jobs.sort(key=lambda j:(int(manifests[j['id']].get('first_cycle_timestamp_ns') or 0),j['id']))
        profiles = sorted({j['profile'] for j in jobs})
        context = {k:run[k] for k in ('run_id','robot_id','boot_id','mapping_revision','runtime_mode',
                                     'start_monotonic_ns','end_monotonic_ns','completeness')}
        context['pipeline_version'] = version
        context['run_catalog_revision']=document['revision']
        context['clock_mapping_revision']=run['mapping_revision']
        mappings=sorted({j['mapping_revision'] for j in jobs})
        context['mapping_revision']=mappings[0] if len(mappings)==1 else 'mixed-unqualified'
        context['max_rows'] = max_rows
        context['import_job_ids'] = run['segment_ids']
        context['row_selection'] = selection
        selected,plan=resolve_plan(run,snapshot);context.update(selected)
        hashes = tuple(sorted({j['artifact_sha256'] for j in jobs}))
        source_types = {manifests[j['id']]['source_type'] for j in jobs}
        source_type = ('synthetic' if source_types == {'SYNTHETIC'} else
                       'simulation' if run['runtime_mode'] == 'SIM' and source_types<={'MANUAL_LOCAL','VERIFIED_TRANSFER'} else
                       'real' if run['runtime_mode']=='REAL' and 'SYNTHETIC' not in source_types else 'historical')
        if 'SYNTHETIC' in source_types:source_type='synthetic'
        context['original_import_source_types']=sorted(source_types)
        context['swerve_mapping_version']=ROBOT_MAPPING_VERSION
        context['swerve_freshness_policy']='ideal_simulation_cycle' if plan and plan['ideal_simulation_cycle_policy'] and source_type=='simulation' else 'physical_acquisition_time_unknown'
        context['dataset_hashes'] = sorted(j['dataset_sha256'] for j in jobs)
        rows, derived = select_rows(run, jobs, manifests,
            (importer.iter_dataset(j['id']) for j in jobs), max_rows=max_rows, plan=plan,
            pipeline_version=version, row_selection=selection)
        context.update(derived)
        evidence = Evidence(profiles[0] if len(profiles)==1 else 'mixed-unqualified', hashes, tuple(rows), context,
            context['mapping_revision'], '+'.join(sorted({j['extractor_version'] for j in jobs})), source_type)
        report=runner.report(analyzers(pipeline_version=version), evidence,
            {'swerve-tracking':plan['swerve_configuration'] if plan else {},
             'recorded-steering-observation':plan.get('steering_configuration',{}) if plan else {}})
        reports.append(report)
        # Streaming analysis is pinned to the Alpha 7 profile; other profiles keep
        # the explicit row-limited report rather than failing the whole pipeline.
        if pipeline_version is None and context.get('row_limit_reached') and profiles==[STREAM_PROFILE]:
            _schedule_stream(root,db,run,document,snapshot,legacy_report=report,pipeline_version=DEFAULT_STREAM_VERSION)
    return reports


def list_for_run(db, run_id):
    reports = []
    for row in db.execute('SELECT result_json FROM analysis_reports ORDER BY created_utc_ns DESC'):
        report = json.loads(row[0])
        if report.get('run_id') == run_id:
            reports.append(report)
    return reports


def public_check(check, *, trace_limit=500):
    """Bound display evidence only; persisted analysis inputs/results stay full."""
    check=copy.deepcopy(check)
    context=check.get('provenance',{}).get('context',{})
    baseline=context.get('approved_baseline')
    if isinstance(baseline,dict):
        context['approved_baseline']={k:baseline[k] for k in ('id','revision','source_type','reviewer','rationale','context','independent_runs') if k in baseline}
        context['approved_baseline']['display_projection']=True
    for key in ('review_comparison_history','maintenance_references'):
        value=context.pop(key,None)
        if value is not None:context[key+'_omitted_from_display']=True
    coverage=check.get('coverage',{})
    for module in coverage.get('modules',[]):
        trace=module.get('evidence_trace',[])
        module.update(evidence_trace=trace[:trace_limit],
                      evidence_trace_total=max(len(trace),module.get('evidence_trace_total',len(trace))),
                      evidence_trace_truncated=module.get('evidence_trace_truncated',False) or len(trace)>trace_limit)
    for finding in check.get('findings',[]):
        for key in ('intervals_ns','source_references'):
            if isinstance(finding.get(key),list):
                values=finding[key];finding[key]=values[:500]
                available=max(len(values),finding.get(key+'_available',len(values)))
                total=max(available,finding.get(key+'_total',available))
                finding[key+'_available']=available
                finding[key+'_total']=total
                finding[key+'_omitted']=total-len(finding[key])
                finding[key+'_truncated']=finding.get(key+'_truncated',False) or total>len(finding[key])
    return check


def public_for_run(db,run_id,*,limit=5):
    from .report_api import detail,RUN_FILTER
    # Extract identity in SQL so unrelated complete report JSON is not loaded.
    count=db.execute('SELECT COUNT(*) FROM analysis_reports WHERE '+RUN_FILTER,(run_id,)).fetchone()[0]
    rows=db.execute('SELECT report_id FROM analysis_reports WHERE '+RUN_FILTER+' ORDER BY CAST(created_utc_ns AS INTEGER) DESC,report_id DESC LIMIT ?',(run_id,limit)).fetchall()
    items=[]
    for row in rows:
        items.append(detail(db,run_id,row[0])['report'])
    return items,{'total':count,'returned':len(items),'limit':limit,'truncated':count>len(items),
                  'basis':'latest_immutable_report_versions','trace_limit_per_module':500}


def _stream_reducer(pipeline_version):
    if pipeline_version==STREAM_VERSION:
        from .stream_quality import RecordingQualityReducer
        return RecordingQualityReducer()
    if pipeline_version==CURRENT_STREAM_VERSION:
        from .stream_status import RecordedStatusReducer
        return RecordedStatusReducer()
    if pipeline_version==TRACKING_STREAM_VERSION:
        from .stream_tracking import TrackingReducer
        return TrackingReducer()
    if pipeline_version==MOTION_STREAM_VERSION:
        from .stream_motion import MotionReducer
        return MotionReducer()
    raise ValueError('stream_report_policy_invalid')


def _stream_reducer_identity(pipeline_version):
    reducer=_stream_reducer(pipeline_version)
    return reducer.reducer_id+'@'+reducer.version


class StreamReportRunner:
    """Dispatch persisted policies to fixed reducers without reinterpreting jobs."""
    def __init__(self,root,db,*,limits=None):
        from .stream_analysis import StreamRunner
        self.db=db
        self.runners={policy:StreamRunner(root,db,reducer=_stream_reducer(policy),limits=limits)
                      for policy in (STREAM_VERSION,CURRENT_STREAM_VERSION,TRACKING_STREAM_VERSION,MOTION_STREAM_VERSION)}
    def schedule(self,run,context):
        policy=context.get('pipeline_version',DEFAULT_STREAM_VERSION)
        if policy not in self.runners:raise ValueError('stream_report_policy_invalid')
        reducer=_stream_reducer(policy)
        if any(key in context and context[key]!=value for key,value in getattr(reducer,'context_versions',{}).items()):
            raise ValueError('stream_report_policy_invalid')
        return self.runners[policy].schedule(run,context)
    def step(self,stream_id=None,**budget):
        from .stream_analysis import StreamError
        if self.db.in_transaction:raise ValueError('stream_worker_requires_owned_transaction')
        import sqlite3
        steps=0;exhausted=False
        def progress():
            nonlocal steps,exhausted
            if exhausted:return 0
            steps+=STREAM_DISPATCH_SQL_STEP_INTERVAL
            exhausted=steps>STREAM_DISPATCH_SQL_STEP_LIMIT
            return int(exhausted)
        self.db.set_progress_handler(progress,STREAM_DISPATCH_SQL_STEP_INTERVAL)
        try:
            if stream_id is None:
                # Exact state/report prefixes use the existing covering index.
                # At most three active candidates are retained, never history.
                candidate=self.db.execute("SELECT stream_id,created_utc_ns FROM analysis_stream_jobs WHERE state='ready' AND report_id IS NULL ORDER BY created_utc_ns,stream_id LIMIT 1").fetchone()
                if candidate is None:
                    candidates=[]
                    for state in ('pending','verifying','running'):
                        found=self.db.execute('SELECT stream_id,created_utc_ns FROM analysis_stream_jobs WHERE state=? AND report_id IS NULL ORDER BY created_utc_ns,stream_id LIMIT 1',(state,)).fetchone()
                        if found is not None:candidates.append(found)
                    candidate=min(candidates,key=lambda value:(value['created_utc_ns'],value['stream_id'])) if candidates else None
                stream_id=candidate['stream_id'] if candidate is not None else None
            row=(self.db.execute('SELECT stream_id,context_json FROM analysis_stream_jobs WHERE stream_id=?',(stream_id,)).fetchone() if stream_id is not None else None)
        except sqlite3.DatabaseError:
            if not exhausted:raise
            # No complete candidate was selected: preserve every job and
            # surface the global metadata failure to the owning worker.
            raise StreamError('stream_sql_resource_limit') from None
        finally:self.db.set_progress_handler(None,0)
        if row is None:
            if stream_id is not None:raise KeyError('stream_not_found')
            return {'state':'idle','phase':'complete'}
        try:
            context=json.loads(row['context_json']);policy=context['pipeline_version']
            if policy not in self.runners or context.get('stream_reducer_id')!=_stream_reducer_identity(policy):
                raise ValueError('incompatible reducer')
            reducer=_stream_reducer(policy)
            if any(context.get(key)!=value for key,value in getattr(reducer,'context_versions',{}).items()):
                raise ValueError('incompatible reducer versions')
        except (ValueError,TypeError,KeyError):raise StreamError('stream_checkpoint_invalid') from None
        return self.runners[policy].step(row['stream_id'],**budget)
    def close(self):
        complete=True
        for runner in self.runners.values():
            if runner.close() is not True:complete=False
        return complete


def _stream_context(run,document,snapshot,legacy_report=None,*,pipeline_version=STREAM_VERSION):
    from .wpilog import MAPPING_REVISION
    context={key:run[key] for key in ('run_id','robot_id','boot_id','runtime_mode',
        'start_monotonic_ns','end_monotonic_ns','completeness')}
    context.update(pipeline_version=pipeline_version,row_selection=STREAM_SELECTION,
        run_catalog_revision=document['revision'],clock_mapping_revision=run['mapping_revision'],
        mapping_revision=MAPPING_REVISION,import_job_ids=list(run['segment_ids']),
        swerve_mapping_version=ROBOT_MAPPING_VERSION,swerve_freshness_policy='physical_acquisition_time_unknown')
    selected,plan=resolve_plan(run,snapshot);context.update(selected)
    if pipeline_version==CURRENT_STREAM_VERSION:
        from .stream_mapper import VERSION as mapper_version
        from .stream_status import PHOENIX_VERSION,SYSTEM_VERSION
        context.update(stream_mapper_version=mapper_version,stream_phoenix_status_version=PHOENIX_VERSION,
                       stream_system_status_version=SYSTEM_VERSION)
    if pipeline_version in (TRACKING_STREAM_VERSION,MOTION_STREAM_VERSION):
        context.update(_stream_reducer(pipeline_version).context_versions)
        context['stream_motion_measurement_source']=(plan or {}).get('stream_tracking_measurement_source','measured_array')
    if legacy_report is not None:
        context['legacy_fallback_report_id']=legacy_report['report_id']
        context['legacy_fallback_reason']='report_resource_limit_reached'
        context['legacy_fallback_pipeline_version']='automatic-reports-7'
    return context


from .wpilog import PROFILE as STREAM_PROFILE


def _schedule_stream(root,db,run,document,snapshot,legacy_report=None,runner=None,*,pipeline_version=STREAM_VERSION):
    from .stream_analysis import StreamRunner
    owned=runner is None
    if isinstance(runner,StreamRunner) and runner._reducer_id()!=_stream_reducer_identity(pipeline_version):
        raise ValueError('stream_report_reducer_invalid')
    if owned:runner=StreamRunner(root,db,reducer=_stream_reducer(pipeline_version))
    try:return runner.schedule(run,_stream_context(run,document,snapshot,legacy_report,pipeline_version=pipeline_version))
    finally:
        if owned:runner.close()


def schedule_stream_reports(root,db,document,*,runner=None,pipeline_version=STREAM_VERSION):
    """Explicit stream admission never invokes tuple manifest/dataset helpers."""
    snapshot=review_snapshot(db)
    return [_schedule_stream(root,db,run,document,snapshot,runner=runner,pipeline_version=pipeline_version) for run in document['runs']]


def publish_stream_report(db,stream_id,descriptor):
    """Publish a distinct fully verified input contract, never tuple Evidence."""
    import time
    from dataclasses import asdict
    from .analysis import identity,canonical as encode,OUTCOMES
    from .stream_quality import VERSION as quality_version
    from .report_lookup import atomic,has_schema,index_report
    if (descriptor.get('input_format')!='stream-evidence-1' or descriptor.get('framework')!=STREAM_FRAMEWORK
            or descriptor.get('full_source_verified') is not True or descriptor.get('profile')!=PROFILE):
        raise ValueError('stream_report_input_not_verified')
    provenance={key:copy.deepcopy(value) for key,value in descriptor.items() if key!='result'}
    policy=provenance.get('context',{}).get('pipeline_version')
    if policy not in (STREAM_VERSION,CURRENT_STREAM_VERSION,TRACKING_STREAM_VERSION,MOTION_STREAM_VERSION):raise ValueError('stream_report_policy_invalid')
    if provenance['context'].get('stream_reducer_id')!=_stream_reducer_identity(policy):
        raise ValueError('stream_report_reducer_invalid')
    if any(provenance['context'].get(key)!=value for key,value in getattr(_stream_reducer(policy),'context_versions',{}).items()):
        raise ValueError('stream_report_policy_invalid')
    status_checks=None
    if policy in (CURRENT_STREAM_VERSION,TRACKING_STREAM_VERSION,MOTION_STREAM_VERSION):
        payload=descriptor['result']
        expected_format='stream-motion-results-1' if policy==MOTION_STREAM_VERSION else 'stream-tracking-results-1' if policy==TRACKING_STREAM_VERSION else 'stream-status-results-1'
        if payload.get('stream_result_format')!=expected_format:raise ValueError('stream_report_result_invalid')
        status_checks=payload.get('checks')
        expected_checks={'recording-quality','phoenix-status-observation','system-status-observation'}
        if policy in (TRACKING_STREAM_VERSION,MOTION_STREAM_VERSION):expected_checks.add('swerve-tracking')
        if policy==MOTION_STREAM_VERSION:expected_checks.add('recorded-steering-observation')
        if type(status_checks) is not dict or set(status_checks)!=expected_checks:
            raise ValueError('stream_report_result_invalid')
        if any(value.get('outcome') not in OUTCOMES for value in status_checks.values()):raise ValueError('stream_report_result_invalid')
    quality=copy.deepcopy(descriptor['result'] if status_checks is None else status_checks['recording-quality'])
    if quality.get('outcome') not in OUTCOMES:raise ValueError('stream_report_result_invalid')
    definitions=[('recording-quality',quality_version),('swerve-tracking','stream-unconverted-1'),
        ('phoenix-status-observation','stream-unconverted-1'),('system-status-observation','stream-unconverted-1'),
        ('recorded-steering-observation','stream-unconverted-1')]
    if status_checks is not None:
        from .stream_status import PHOENIX_VERSION,SYSTEM_VERSION
        definitions[2]=('phoenix-status-observation',PHOENIX_VERSION)
        definitions[3]=('system-status-observation',SYSTEM_VERSION)
    if policy in (TRACKING_STREAM_VERSION,MOTION_STREAM_VERSION):
        from .stream_tracking import VERSION as tracking_version
        definitions[1]=('swerve-tracking',tracking_version)
    if policy==MOTION_STREAM_VERSION:
        from .stream_steering import VERSION as steering_version
        definitions[4]=('recorded-steering-observation',steering_version)
    checks=[]
    plan=provenance['context'].get('analysis_plan') or {}
    for analyzer_id,version in definitions:
        declaration=asdict(Declaration(analyzer_id,version,(PROFILE,),
            eligible_windows=('full verified source traversal; quality counters scoped to selected run' if analyzer_id=='recording-quality' else
                              'full verified source traversal; recorded status scoped to selected run' if status_checks is not None and analyzer_id in status_checks else
                              'stream evaluator not converted')))
        configuration=(plan.get('swerve_configuration',{}) if analyzer_id=='swerve-tracking' else
            plan.get('steering_configuration',{}) if analyzer_id=='recorded-steering-observation' else {})
        output=copy.deepcopy(quality) if analyzer_id=='recording-quality' else result('insufficient_data',
            unavailable=['stream_evaluator_not_converted'],coverage={'scope':'not_evaluated','input_format':'stream-evidence-1','physical_acquisition_time_qualified':False})
        if status_checks is not None and analyzer_id in status_checks:output=copy.deepcopy(status_checks[analyzer_id])
        job_id=identity([STREAM_FRAMEWORK,declaration,provenance,configuration])
        output.update(job_id=job_id,analyzer_id=analyzer_id,analyzer_version=version,provenance=provenance,
            configuration=configuration,declaration=declaration)
        checks.append(output)
    report_id=identity([STREAM_FRAMEWORK,[check['job_id'] for check in checks]])
    document=dict(report_id=report_id,framework_version=STREAM_FRAMEWORK,source_type=provenance['source_type'],
        source_hashes=provenance['source_hashes'],run_id=provenance['context']['run_id'],checks=checks,
        evaluated_checks=sum(check['outcome'] in ('finding','evaluated_no_finding') for check in checks),
        unavailable_checks=sum(check['outcome'] in ('insufficient_data','unsupported','failed') for check in checks),
        finding_count=sum(len(check['findings']) for check in checks),overall_health='not_assessed')
    with atomic(db):
        ready=db.execute('SELECT state,report_id,descriptor_json FROM analysis_stream_jobs WHERE stream_id=?',(stream_id,)).fetchone()
        if ready is None or ready[0]!='ready':raise ValueError('stream_report_job_not_ready')
        if ready[2] is None or encode(json.loads(ready[2]))!=encode(descriptor):
            raise ValueError('stream_report_ready_descriptor_mismatch')
        if ready[1] is not None and ready[1]!=report_id:raise ValueError('stream_report_publication_conflict')
        for check in checks:
            db.execute('INSERT OR IGNORE INTO analyzer_jobs VALUES(?,?,?,?,?,?,?,?,?,?)',
                (check['job_id'],STREAM_FRAMEWORK,check['analyzer_id'],check['analyzer_version'],encode(check['declaration']),
                 encode(provenance),encode(check['configuration']),check['outcome'],encode(check),str(time.time_ns())))
        db.execute('INSERT OR IGNORE INTO analysis_reports VALUES(?,?,?,?)',
            (report_id,encode({'source_hashes':provenance['source_hashes'],'context':provenance['context']}),encode(document),str(time.time_ns())))
        if has_schema(db):index_report(db,report_id)
        db.execute('UPDATE analysis_stream_jobs SET report_id=? WHERE stream_id=? AND state=?',(report_id,stream_id,'ready'))
    return document


def advance_streams(root,db,runner,**step_budget):
    """One bounded worker turn; no pending result is published as a report."""
    progress=runner.step(**step_budget)
    if progress.get('state')=='ready' and progress.get('descriptor') is not None:
        document=publish_stream_report(db,progress['stream_id'],progress['descriptor'])
        progress={**progress,'report_id':document['report_id'],'report':document}
    return progress
