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

VERSION = 'automatic-reports-4'
MAX_ROWS = 50000


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


def analyzers():
    return [Analyzer(Declaration('recording-quality', VERSION, (PROFILE,)), _quality),
            Analyzer(swerve_analyzer((PROFILE,)).declaration,_swerve)]


def generate(root, db, document, *, max_rows=MAX_ROWS):
    """Read one run at a time. Never infer hardware origin from transfer transport.

    Valid observations are streamed past: the default check only counts invalid
    observations and checks cycle coverage. It does not assess sensor health.
    Large/problematic runs produce explicit unavailable reports, never truncated
    successful health reports. Raw originals and complete imported datasets stay.
    """
    if type(max_rows) is not int or max_rows < 2:
        raise ValueError('max_rows must be at least two')
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
        context['pipeline_version'] = VERSION
        context['run_catalog_revision']=document['revision']
        context['clock_mapping_revision']=run['mapping_revision']
        mappings=sorted({j['mapping_revision'] for j in jobs})
        context['mapping_revision']=mappings[0] if len(mappings)==1 else 'mixed-unqualified'
        context['max_rows'] = max_rows
        context['import_job_ids'] = run['segment_ids']
        context['row_selection'] = 'source_order_required_schemas_lifecycle_held_state_and_run_cycles'
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
        # Derived evidence checks are part of the final job identity. Runner
        # reuses immutable results only after the bounded source scan completes.
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
        for job in jobs:
            previous_cycle=None
            for row in importer.iter_dataset(job['id']):
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
                    relevant=(row.get('field') in set(ROBOT_FIELDS.values())|CONNECTION_FIELDS or row.get('type')=='structschema')
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
                rows.append({**row, 'source_sha256':job['artifact_sha256'],'before_run':before})
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
        evidence = Evidence(profiles[0] if len(profiles)==1 else 'mixed-unqualified', hashes, tuple(rows), context,
            context['mapping_revision'], '+'.join(sorted({j['extractor_version'] for j in jobs})), source_type)
        reports.append(runner.report(analyzers(), evidence,
            {'swerve-tracking':plan['swerve_configuration'] if plan else {}}))
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
        module.update(evidence_trace=trace[:trace_limit],evidence_trace_total=len(trace),
                      evidence_trace_truncated=len(trace)>trace_limit)
    for finding in check.get('findings',[]):
        for key in ('intervals_ns','source_references'):
            if isinstance(finding.get(key),list):
                values=finding[key];finding[key]=values[:500]
                finding[key+'_total']=len(values);finding[key+'_truncated']=len(values)>500
    return check


def public_for_run(db,run_id,*,limit=5):
    # Extract identity in SQL so unrelated complete report JSON is not loaded.
    count=db.execute("SELECT COUNT(*) FROM analysis_reports WHERE json_extract(result_json,'$.run_id')=?",(run_id,)).fetchone()[0]
    rows=db.execute("SELECT result_json FROM analysis_reports WHERE json_extract(result_json,'$.run_id')=? ORDER BY created_utc_ns DESC,report_id DESC LIMIT ?",(run_id,limit)).fetchall()
    items=[]
    for row in rows:
        report=json.loads(row[0]);report['checks']=[public_check(c) for c in report['checks']];items.append(report)
    return items,{'total':count,'returned':len(items),'limit':limit,'truncated':count>len(items),
                  'basis':'latest_immutable_report_versions','trace_limit_per_module':500}
