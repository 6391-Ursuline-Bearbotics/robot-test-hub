"""Bounded automatic recording-quality reports, pinned to immutable run evidence."""
import json

from .analysis import Analyzer, Declaration, Evidence, Runner, data_quality, result, CONNECTION_FIELDS
from .importer import Importer
from .runs import canonical
from .wpilog import PROFILE

VERSION = 'automatic-reports-3'
MAX_ROWS = 50000


def _quality(evidence, configuration):
    if evidence.context.get('row_limit_reached'):
        return result('insufficient_data', unavailable=['report_resource_limit_reached'])
    output = data_quality(evidence, configuration)
    nonadvancing=evidence.context.get('source_nonadvancing_cycles',[])
    if nonadvancing:
        output['outcome']='finding'
        output['findings'].append({'kind':'nonadvancing_source_cycle_timestamps',
            'severity':'warning','observed_behavior':'Source cycles did not advance before chronological merge',
            'count':len(nonadvancing),'source_references':nonadvancing,
            'next_check':'Inspect source clock and logging order before interpreting control metrics'})
    output['unavailable'].extend(['physical_sensor_freshness_not_qualified', 'usb_write_health_not_qualified'])
    return output


def _swerve_unavailable(evidence, configuration):
    return result('insufficient_data', unavailable=['physical_component_assignments_and_acquisition_freshness_required',
        'approved_comparable_baseline_not_selected'], coverage={'scope':'not_evaluated'})


def analyzers():
    return [Analyzer(Declaration('recording-quality', VERSION, (PROFILE,)), _quality),
            Analyzer(Declaration('swerve-readiness', VERSION, (PROFILE,)), _swerve_unavailable)]


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
    reports = []
    for run in document['runs']:
        jobs = [importer.get_job(j) for j in run['segment_ids']]
        profiles = sorted({j['profile'] for j in jobs})
        context = {k:run[k] for k in ('run_id','robot_id','boot_id','mapping_revision','runtime_mode',
                                     'start_monotonic_ns','end_monotonic_ns','completeness')}
        context['pipeline_version'] = VERSION
        context['max_rows'] = max_rows
        context['import_job_ids'] = run['segment_ids']
        context['row_selection'] = 'run_cycles_invalid_observations_connections_and_samples'
        if run['wall_clock_quality'] == 'anchored':
            context['start_utc_ns'] = run['utc_intervals'][0]['utc_start_ns']
            context['end_utc_ns'] = run['utc_intervals'][-1]['utc_end_ns']
        # A prior report with this exact immutable context already has checked
        # source/dataset hashes. Rebuild on version, policy, or run revision change.
        hashes = tuple(sorted({j['artifact_sha256'] for j in jobs}))
        source_types = {importer.read_manifest(j['id'])['source_type'] for j in jobs}
        source_type = ('synthetic' if source_types == {'SYNTHETIC'} else
                       'simulation' if run['runtime_mode'] == 'SIM' else 'historical')
        context['dataset_hashes'] = sorted(j['dataset_sha256'] for j in jobs)
        key = canonical({'source_hashes':hashes, 'context':context})
        previous = db.execute('SELECT result_json FROM analysis_reports WHERE provenance_json=?', (key,)).fetchone()
        if previous:
            reports.append(json.loads(previous[0]))
            continue
        rows, cycles, overflow = [], {}, False
        nonadvancing=[]
        lo, hi = int(run['start_monotonic_ns']), int(run['end_monotonic_ns'])
        for job in jobs:
            previous_cycle=None
            for row in importer.iter_dataset(job['id']):
                if row.get('kind') not in ('cycle','observation','sample'):
                    continue
                timestamp = row.get('timestamp_ns', row.get('cycle_timestamp_ns'))
                if timestamp is None:
                    continue
                stamp = int(timestamp)
                if not lo <= stamp <= hi:
                    continue
                if row['kind'] == 'observation' and row.get('validity') == 'valid' and row.get('field') not in CONNECTION_FIELDS:
                    continue
                if row['kind'] == 'cycle':
                    if previous_cycle is not None and stamp<=previous_cycle:
                        nonadvancing.append({'source_sha256':job['artifact_sha256'],
                            'record_index':row.get('record_index'),'timestamp_ns':str(stamp),
                            'previous_timestamp_ns':str(previous_cycle)})
                    previous_cycle=stamp
                    aliases = {k:v.get('value') for k,v in row.get('aliases',{}).items()}
                    if stamp in cycles:
                        if cycles[stamp] != aliases:
                            overflow = True  # Conflicting overlap is not assessable.
                        continue
                    cycles[stamp] = aliases
                if len(rows) >= max_rows:
                    overflow = True
                    break
                rows.append({**row, 'source_sha256':job['artifact_sha256']})
            if overflow:
                break
        rows.sort(key=lambda r:(int(r.get('timestamp_ns', r.get('cycle_timestamp_ns'))),r.get('record_index',0)))
        if overflow:
            context['row_limit_reached'] = True
        if nonadvancing:
            context['source_nonadvancing_cycles']=nonadvancing
        evidence = Evidence(profiles[0] if len(profiles)==1 else 'mixed-unqualified', hashes, tuple(rows), context,
            run['mapping_revision'], '+'.join(sorted({j['extractor_version'] for j in jobs})), source_type)
        reports.append(runner.report(analyzers(), evidence))
    return reports


def list_for_run(db, run_id):
    reports = []
    for row in db.execute('SELECT result_json FROM analysis_reports ORDER BY created_utc_ns DESC'):
        report = json.loads(row[0])
        if report.get('run_id') == run_id:
            reports.append(report)
    return reports
