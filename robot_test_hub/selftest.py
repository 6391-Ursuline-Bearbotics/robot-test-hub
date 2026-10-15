"""Offline synthetic installation exercise; never opens a configured archive."""
import hashlib
from importlib import resources
import json
from pathlib import Path
import shutil
import tempfile

SAMPLE_SHA256='df79f80165f2a4577913d86a503e11069fa89037ae6ee2931d8dcbe7480cb3d5'
SAMPLE_SIZE=4530
CYCLE_TIMESTAMPS=('1000000000','1020000123','1040000000','1060000000','1080000000','1500000000','1520000000')
# Failed close retains ownership instead of releasing a root with live users.
_RETAINED_CLEANUP=[]
STAGES=('fixture','transfer_gating','transfer_resume','import_index','search','notebook','backup_restore','analytics')
GUIDANCE={
 'fixture':'Reinstall the hub package with its unchanged bundled synthetic sample.',
 'transfer_gating':'Check temporary local storage and the collector permission and idle-delay checks.',
 'transfer_resume':'Inspect local storage and the collector checkpoint recovery checks.',
 'import_index':'Inspect the pinned importer, run catalog and report lookup checks.',
 'search':'Inspect the supported fixture clock mapping and run search checks.',
 'notebook':'Inspect local notebook persistence; incident time must survive reopening.',
 'backup_restore':'Inspect local backup verification and restore integrity checks.',
 'analytics':'Install the pinned analytics extra explicitly; if installed, inspect conversion, query and summary checks.'}
PASS_GUIDANCE={
 'fixture':'Bundled invented sample and locked digest match.',
 'transfer_gating':'Enabled, stale and insufficient-idle status blocked source reads.',
 'transfer_resume':'Interrupted transfer resumed its checkpoint and verified the original bytes.',
 'import_index':'Expected two runs, fixture timestamps and exact report receipts match.',
 'search':'Known fixture UTC search returned the expected run.',
 'notebook':'Reopened delayed note retained its original incident time and revision.',
 'backup_restore':'Verified restore retained original, run, report and note identities.',
 'analytics':'Real Parquet conversion, known query and portable summary match.'}


def _require(condition):
    if not condition:raise ValueError('synthetic_exercise_expectation_failed')


def _sample():
    folder=resources.files('robot_test_hub').joinpath('sample_data')
    with folder.joinpath('manifest.json').open('rb') as stream:encoded=stream.read(4097)
    _require(len(encoded)<=4096)
    manifest=json.loads(encoded)
    expected=dict(schema_version=1,source_type='SYNTHETIC',file='alpha7-main.wpilog',size_bytes=SAMPLE_SIZE,
                  sha256=SAMPLE_SHA256,physical_records=143,cycle_timestamp_ns=list(CYCLE_TIMESTAMPS))
    # Git may check out this text resource with CRLF on Windows. Only its
    # line-ending representation is flexible; manifest values and raw log bytes
    # remain exact. No normalization is applied to the original WPILOG.
    canonical=(json.dumps(expected,sort_keys=True,indent=2)+'\n').encode('utf-8')
    _require(manifest==expected and encoded.replace(b'\r\n',b'\n')==canonical)
    with folder.joinpath('alpha7-main.wpilog').open('rb') as stream:data=stream.read(SAMPLE_SIZE+1)
    _require(len(data)==SAMPLE_SIZE and hashlib.sha256(data).hexdigest()==SAMPLE_SHA256)
    return data


class _Clock:
    def __init__(self):self.now=100.0
    def __call__(self):return self.now


class _SyntheticSource:
    """Only an in-memory invented sample; deliberately no adapter configuration."""
    def __init__(self,data,clock):
        self.data,self.clock=data,clock
        self.enabled=True;self.stale=False;self.reads=[];self.discoveries=0
    def status(self):
        from .collector import RobotStatus
        return RobotStatus(self.enabled,self.clock()-10 if self.stale else self.clock(),'synthetic-selftest-boot',0)
    def list_closed_files(self):
        from .collector import LogFile
        from .wpilog import PROFILE
        self.discoveries+=1
        return [LogFile('synthetic-selftest-log','synthetic-selftest.wpilog',len(self.data),SAMPLE_SHA256,1,
                        format='wpilog',format_profile=PROFILE)]
    def read(self,file_id,offset,length):
        _require(not self.enabled and not self.stale and file_id=='synthetic-selftest-log')
        self.reads.append((offset,length))
        return self.data[offset:offset+length]


def _receipts(db):
    from .report_lookup import REFERENCES_SQL
    rows=db.execute('SELECT report_id,result_json FROM analysis_reports ORDER BY report_id').fetchall()
    _require(len(rows)==2)
    exact={row['report_id']:hashlib.sha256(row['result_json'].encode('utf-8')).hexdigest() for row in rows}
    refs=db.execute('SELECT * FROM ('+REFERENCES_SQL+') WHERE sha=? ORDER BY report_id',(SAMPLE_SHA256,)).fetchall()
    _require({row['report_id']:row['sha256'] for row in refs}==exact)
    run_refs={row['report_id']:row['run_id'] for row in refs}
    for row in rows:
        doc=json.loads(row['result_json'])
        _require(run_refs[row['report_id']]==doc['run_id'])
        _require(doc['overall_health']=='not_assessed' and doc['source_type']=='historical' and doc['source_hashes']==[SAMPLE_SHA256])
    return exact


def _analytics(root,db,job):
    from .columnar import Converter,connection,dependency
    from .summaries import Summarizer,FILES
    dependency()  # Required only by this explicitly requested stage.
    converter=Converter(root,db,threads=1,memory_mb=256)
    converted=converter.convert(job['id'])
    _require(converted['state']=='succeeded')
    row=db.execute("SELECT path FROM columnar_artifacts WHERE job_id=? AND path LIKE '%/records.parquet'",(converted['id'],)).fetchone()
    _require(row is not None)
    with connection(root,threads=1,memory_mb=256) as duck:
        count,first,last=duck.execute("SELECT count(*),min(timestamp_ns),max(timestamp_ns) FROM read_parquet(?) WHERE kind='cycle'",[str(root/row['path'])]).fetchone()
    _require((count,first,last)==(7,1000000000,1520000000))
    report,queried,cached=Summarizer(root,db,converter).generate()
    _require(report['state']=='succeeded' and queried==2)
    items=db.execute('SELECT path,sha256,size_bytes FROM summary_artifacts WHERE report_id=?',(report['id'],)).fetchall()
    _require({Path(row['path']).name for row in items}==set(FILES))
    for item in items:
        data=(root/item['path']).read_bytes()
        _require(len(data)==item['size_bytes'] and hashlib.sha256(data).hexdigest()==item['sha256'])
    summary=json.loads((root/next(row['path'] for row in items if row['path'].endswith('/summary.json'))).read_text(encoding='utf-8'))
    _require(summary['full_summary_run_count']==2 and summary['hardware_thresholds_qualified'] is False)


def run_selftest(*,analytics=False):
    """Run only in newly allocated storage and return bounded, path-free receipts.

    A passed stage is an installation exercise using invented evidence. It is
    never a physical-health verdict or qualification of a configured system.
    """
    if type(analytics) is not bool:raise ValueError('analytics_must_be_boolean')
    report=dict(schema_version=1,success=False,source_type='SYNTHETIC',hardware_qualified=False,
        network_contacted=False,active_archive_opened=False,overall_health='not_assessed',analytics_requested=analytics,
        cleanup={'state':'passed'},stages=[])
    workspace=None;collector=None;db=None;owner=None;recovered=None;restored_owner=None;stage='fixture'
    def passed(identity):
        report['stages'].append(dict(id=identity,state='passed',code='selftest_'+identity+'_passed',guidance=PASS_GUIDANCE[identity]))
    try:
        data=_sample();passed('fixture')
        stage='transfer_gating'
        workspace=Path(tempfile.mkdtemp(prefix='robot-test-hub-selftest-')).resolve()
        root=workspace/'archive'
        from .collector import Collector
        from .storage import DataRootOwner,open_catalog
        from .pipeline import Pipeline
        from .importer import Importer
        from .runs import RunCatalog
        from .notebook import Notebook
        from .report_lookup import coverage,step
        from .backup import Backup,verify,restore
        clock=_Clock();source=_SyntheticSource(data,clock)
        stage='transfer_gating'
        collector=Collector(root,source,idle_delay=2,freshness=1,chunk_size=512,clock=clock,jitter=lambda:1)
        collector.tick()
        _require(source.reads==[] and source.discoveries==0)
        source.enabled=False;source.stale=True;collector.tick()
        _require(source.reads==[] and source.discoveries==0)
        source.stale=False;collector.tick();clock.now+=1.5;collector.tick()
        _require(source.reads==[] and source.discoveries==0)
        passed(stage)
        stage='transfer_resume'
        clock.now+=.5;collector.tick()
        before=collector.db.execute("SELECT offset,state FROM files WHERE id='synthetic-selftest-log'").fetchone()
        _require(before is not None and before['offset']==512 and before['state']!='complete')
        offset=before['offset'];calls=len(source.reads)
        source.enabled=True;collector.tick()
        _require(len(source.reads)==calls)
        collector.close();collector=None
        collector=Collector(root,source,idle_delay=2,freshness=1,chunk_size=512,clock=clock,jitter=lambda:1)
        _require(collector.db.execute("SELECT offset FROM files WHERE id='synthetic-selftest-log'").fetchone()[0]==offset)
        collector.tick();_require(len(source.reads)==calls)
        source.enabled=False;collector.tick();clock.now+=1.5;collector.tick();_require(len(source.reads)==calls)
        clock.now+=.5
        for _ in range(20):
            collector.tick()
            if collector.snapshot()['completed_files']==1:break
            clock.now+=.1
        _require(collector.snapshot()['completed_files']==1 and source.reads[calls][0]==offset)
        _require((root/'archive/synthetic-selftest-log.logdata').read_bytes()==data)
        collector.close();collector=None;passed(stage)
        stage='import_index'
        owner=DataRootOwner(root);db=open_catalog(root)
        result=Pipeline(root,db).tick()
        _require(result['runs']==2 and result['reports']==2)
        jobs=Importer(root,db).list_jobs();_require(len(jobs)==1 and jobs[0]['state']=='succeeded')
        job=jobs[0];manifest=Importer(root,db).read_manifest(job['id'])
        _require(manifest['counts']['cycle']==7 and manifest['first_cycle_timestamp_ns']==CYCLE_TIMESTAMPS[0] and manifest['last_cycle_timestamp_ns']==CYCLE_TIMESTAMPS[-1])
        physical=[row for row in Importer(root,db).iter_dataset(job['id']) if 'record_index' in row and row['kind'] in ('control','observation')]
        _require(len(physical)==143)
        _require(tuple(row['timestamp_ns'] for row in Importer(root,db).iter_dataset(job['id'],kind='cycle'))==CYCLE_TIMESTAMPS)
        document=RunCatalog(db).current()
        _require([run['start_monotonic_ns'] for run in document['runs']]==['1020000123','1500000000'])
        _require([run['end_monotonic_ns'] for run in document['runs']]==['1080000000','1520000000'])
        for _ in range(5):
            if coverage(db)['complete']:break
            step(db)
        _require(coverage(db)['complete'])
        receipts=_receipts(db);passed(stage)
        stage='search'
        found=RunCatalog(db).search(utc_from_ns=1791003600020000000,utc_to_ns=1791003600020000000)
        _require(found['total']==1 and found['items'][0]['start_monotonic_ns']=='1020000123')
        passed(stage)
        stage='notebook'
        incident='1791003600020000000';submitted='1791003630020000000'
        note=dict(schema_version=1,event_id='synthetic-selftest-note',revision=1,submitted_utc_ns=submitted,
            client_monotonic_ns='90000000000',event_utc_start_ns=incident,event_utc_end_ns=incident,
            when={'kind':'seconds_ago','seconds':30},clock_domain='synthetic-selftest',clock_quality='unverified_client',
            uncertainty_ms=1000,text='Invented delayed practice observation',tags=['synthetic'],source='practice-notebook',run_id=None)
        Notebook(root/'catalog.sqlite3',clock_ns=lambda:1791003640020000000).save(note)
        db.close();db=None
        saved=Notebook(root/'catalog.sqlite3').history(note['event_id'])['annotation']
        _require(saved['event_utc_start_ns']==incident and saved['submitted_utc_ns']==submitted and saved['revision']==1)
        note_pin=Notebook(root/'catalog.sqlite3').exact_revision(note['event_id'],1)
        db=open_catalog(root);passed(stage)
        stage='backup_restore'
        destination=workspace/'backup';restored=workspace/'restored'
        _require(Backup(root,destination).create('synthetic-selftest')['state']=='complete')
        verify(destination/'synthetic-selftest');restore(destination/'synthetic-selftest',restored)
        restored_owner=DataRootOwner(restored)
        recovered=open_catalog(restored)
        _require(RunCatalog(recovered).current()==document and _receipts(recovered)==receipts)
        restored_job=Importer(restored,recovered).get_job(job['id'])
        _require(restored_job==job)
        _require(Importer(restored,recovered).read_manifest(job['id'])==manifest)
        _require((restored/manifest['raw_path']).read_bytes()==data)
        _require((restored/'archive/synthetic-selftest-log.logdata').read_bytes()==data)
        recovered.close();recovered=None
        _require(Notebook(restored/'catalog.sqlite3').exact_revision(note['event_id'],1)==note_pin)
        _require(Notebook(restored/'catalog.sqlite3').history(note['event_id'])['annotation']==saved)
        restored_owner.close();restored_owner=None
        passed(stage)
        stage='analytics'
        if analytics:_analytics(root,db,job);passed(stage)
        else:report['stages'].append(dict(id=stage,state='skipped',code='analytics_not_requested',guidance='Optional analytics was not exercised; use --analytics to require its pinned dependency and real conversion.'))
    except BaseException:
        if stage not in [row['id'] for row in report['stages']]:
            report['stages'].append(dict(id=stage,state='failed',code='selftest_'+stage+'_failed',guidance=GUIDANCE[stage]))
    finally:
        released=True
        for resource in (collector,recovered,db):
            if resource is not None:
                try:resource.close()
                except BaseException:released=False
        if released:
            for resource in (restored_owner,owner):
                if resource is not None:
                    try:resource.close()
                    except BaseException:released=False
        if not released:
            _RETAINED_CLEANUP.append((workspace,collector,db,owner))
            if recovered is not None or restored_owner is not None:
                _RETAINED_CLEANUP.append((workspace,None,recovered,restored_owner))
        if workspace is not None and released:
            try:shutil.rmtree(workspace)
            except BaseException:released=False
        if not released:report['cleanup']={'state':'failed'}
    recorded={row['id'] for row in report['stages']}
    for identity in STAGES:
        if identity not in recorded:report['stages'].append(dict(id=identity,state='skipped',code='dependency_failed',guidance='Resolve the earlier failed exercise stage and rerun.'))
    report['success']=report['cleanup']['state']=='passed' and all(row['state']=='passed' or (row['id']=='analytics' and row['code']=='analytics_not_requested') for row in report['stages'])
    return report


def main(argv=None):
    import argparse
    parser=argparse.ArgumentParser(description='Exercise invented sample data in fresh temporary storage; no hardware qualification.')
    parser.add_argument('--json',action='store_true')
    parser.add_argument('--analytics',action='store_true')
    args=parser.parse_args(argv)
    report=run_selftest(analytics=args.analytics)
    if args.json:print(json.dumps(report,sort_keys=True,separators=(',',':')))
    else:
        print('SYNTHETIC offline installation exercise: '+('passed' if report['success'] else 'failed'))
        for row in report['stages']:print(row['id']+': '+row['state']+' — '+row['guidance'])
        print('Cleanup: '+report['cleanup']['state']+'. Hardware health is not assessed; no live configuration was opened.')
    return 0 if report['success'] else 1


if __name__=='__main__':raise SystemExit(main())
