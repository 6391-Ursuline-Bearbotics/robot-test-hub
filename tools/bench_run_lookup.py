"""Repeatable SYNTHETIC run lookup scale and optional dense WPILOG profile.

Examples (run from a full source checkout; artifacts stay outside Git):
  python tools/bench_run_lookup.py --sizes 10000,50000
  python tools/bench_run_lookup.py --sizes "" --dense-cycles 300,1500

The normal document publication hook indexes explicitly invented catalog-only
clones of a public synthetic run template. This is not raw run-derivation proof. Query timings
use read-only SQLite with document-column reads denied and a 5M VM-step budget.
These are local software measurements, not school laptop or hardware claims.
"""
from contextlib import closing
import argparse
import copy
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import struct
import sys
import tempfile
import time

REPOSITORY=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(REPOSITORY))
from robot_test_hub.runs import RunCatalog
from robot_test_hub.storage import DataRootOwner,open_catalog
from robot_test_hub import run_lookup

VM_LIMIT=5_000_000
UTC=1800000000000000000

def sizes(text,maximum):
    values=[] if not text else [int(item) for item in text.split(',')]
    if any(not 50<=value<=maximum for value in values):raise ValueError('size outside supported benchmark bounds')
    return values

def load_template(root):
    from robot_test_hub.importer import Importer
    from robot_test_hub.runs import rebuild_from_imports
    root.mkdir()
    with closing(open_catalog(root)) as db:
        imported=Importer(root,db).import_file(REPOSITORY/'tests/fixtures/synthetic/alpha7-main.wpilog',source_type='SYNTHETIC')
        if imported['state']!='succeeded':raise RuntimeError('Public synthetic template import unavailable')
        return rebuild_from_imports(root,db)['runs'][0]

def catalog_document(template,count):
    runs=[]
    for index in range(count):
        run=copy.deepcopy(template)
        run['run_id']=hashlib.sha256(('invented-bench-'+str(index)).encode()).hexdigest()
        run['robot_id']='invented-bench-'+str(index%5)
        if index%10==0:
            run['utc_intervals']=[];run['wall_clock_quality']='unknown'
        else:
            for interval in run['utc_intervals']:
                for key in ('utc_start_ns','utc_end_ns'):interval[key]=str(int(interval[key])+index*10000000000)
        runs.append(run)
    return dict(revision=hashlib.sha256(('invented-catalog-'+str(count)).encode()).hexdigest(),runs=runs,mappings=[])

class MeasuredConnection(sqlite3.Connection):
    bench_active=False
    bench_steps=0
    def set_progress_handler(self,callback,steps):
        # Production replaces the callback to enforce its own query budget;
        # wrap every installation so the benchmark still counts that work.
        if self.bench_active and callback is not None and steps>0:
            original=callback
            def counted():
                self.bench_steps+=steps
                return self.bench_steps>VM_LIMIT or original()
            return super().set_progress_handler(counted,steps)
        return super().set_progress_handler(callback,steps)

def measured(db,operation):
    db.bench_active=True;db.bench_steps=0;forbidden=0
    def authorize(action,table,column,*unused):
        nonlocal forbidden
        if action==sqlite3.SQLITE_READ and table=='run_catalog_revisions' and column=='document':
            forbidden+=1;return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    db.set_authorizer(authorize);db.set_progress_handler(lambda:False,1000)
    start=time.perf_counter()
    try:value=operation()
    finally:
        elapsed=time.perf_counter()-start
        db.bench_active=False
        db.set_authorizer(lambda *unused:sqlite3.SQLITE_OK);db.set_progress_handler(None,0)
    return value,dict(seconds=elapsed,sqlite_vm_steps_approx=db.bench_steps,sqlite_vm_step_budget=VM_LIMIT,forbidden_catalog_body_reads=forbidden)

def lookup_case(root,count,template):
    root.mkdir();doc=catalog_document(template,count)
    with closing(DataRootOwner(root)),closing(open_catalog(root)) as db:
        started=time.perf_counter();run_lookup.publish_document(db,doc);published=time.perf_counter()-started
        target=doc['runs'][count-2];identity=target['run_id'];revision=doc['revision'];interval=target['utc_intervals'][0]
        catalog_bytes=db.execute('SELECT length(CAST(document AS BLOB)) FROM run_catalog_revisions WHERE id=?',(revision,)).fetchone()[0]
        index_rows={table:db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] for table in ('run_lookup_runs','run_lookup_intervals')}
    uri=(root/'catalog.sqlite3').resolve().as_uri()+'?mode=ro'
    with closing(sqlite3.connect(uri,uri=True,factory=MeasuredConnection)) as db:
        db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
        exact,exact_time=measured(db,lambda:RunCatalog(db).search(utc_from_ns=int(interval['utc_start_ns']),utc_to_ns=int(interval['utc_end_ns']),robot=target['robot_id']))
        late,late_time=measured(db,lambda:RunCatalog(db).search(offset=count-50,limit=50))
        selected,lookup_time=measured(db,lambda:run_lookup.select_run(db,identity,revision=revision))
        assert exact['total']==1 and late['total']==count and len(late['items'])==50 and selected[0]['run_id']==identity
    return dict(source='invented_catalog_clones_of_public_synthetic_run',runs=count,catalog_bytes=catalog_bytes,index_rows=index_rows,
        atomic_ready_publish_seconds=published,exact_time_and_robot_search=exact_time,late_page_search=late_time,selected_run_lookup=lookup_time,
        raw_run_derivation_measured=False,operating_system_cache_not_flushed=True)

def dense_recording(count):
    # Reuse the existing typed fixture writer and source-qualified Phoenix field
    # fixture, with an explicit update for every field on every cycle.
    sys.path.insert(0,str(REPOSITORY/'tests'))
    from test_steering_integration import recording
    from test_phoenix_status import values
    from test_wpilog import start,wire_record
    from robot_test_hub.wpilog import records
    from robot_test_hub.phoenix_status import COMMON,SIGNALS,SIGNAL_FIELDS
    raw=recording(samples=count);header=raw[:12+int.from_bytes(raw[8:12],'little')]
    fields=[];declarations=[];entry=10000
    def packed(kind,value):return value.encode() if kind=='string' else bytes([value]) if kind=='boolean' else struct.pack('<q' if kind=='int64' else '<d',value)
    for module in range(4):
        for name,value in values(1,1100000000).items():
            spec=COMMON.get(name)
            if spec is None:
                signal=next(signal for signal in SIGNALS if name.startswith(signal));spec=SIGNAL_FIELDS[name[len(signal):]]
            metadata=json.dumps({'unit':spec[1]}) if spec[1] else '{}'
            declarations.extend((start(entry,f'/Drive/Module{module}/Phoenix'+name,spec[0],metadata),wire_record(entry,packed(spec[0],value))))
            fields.append((entry,name,spec[0]));entry+=1
    events=list(declarations);sequence=0
    for record in records(io.BytesIO(raw)):
        events.append(wire_record(record.entry,record.payload,record.timestamp_us))
        if record.entry==1:
            sequence+=1;current=values(sequence,struct.unpack('<q',record.payload)[0])
            for entry,name,kind in fields:events.append(wire_record(entry,packed(kind,current[name]),record.timestamp_us))
    return header+b''.join(events),len(fields)

def dense_case(root,count):
    from robot_test_hub.importer import Importer
    from robot_test_hub.pipeline import Pipeline
    from robot_test_hub.review import ReviewStore
    from robot_test_hub.swerve import MODULE_POSITIONS
    from robot_test_hub.reports import MAX_ROWS
    root.mkdir();start=time.perf_counter();raw,fields=dense_recording(count);generation=time.perf_counter()-start
    path=root/'synthetic.wpilog';path.write_bytes(raw)
    with closing(DataRootOwner(root)),closing(open_catalog(root)) as db:
        review=ReviewStore(db)
        review.record_analysis_plan(dict(id='synthetic-plan',robot_id='steering-robot',start_utc_ns=str(UTC-1000000),end_utc_ns=None,
            reviewer='Synthetic benchmark',rationale='Software-only dense source profile',build_hash='a'*64,config_hash='b'*64,
            test_id='synthetic-dense',surface='invented',battery_id='invented',wheel_radius_m=.05,swerve_configuration={}))
        for index,position in enumerate(MODULE_POSITIONS):review.assign_component(dict(id='synthetic-assignment-'+str(index),robot_id='steering-robot',component_id='synthetic-module-'+str(index),
            location=position,start_utc_ns=str(UTC-1000000),end_utc_ns=None,reviewer='Synthetic benchmark',rationale='Invented identity'))
        start=time.perf_counter();job=Importer(root,db).import_file(path,source_type='SYNTHETIC');import_seconds=time.perf_counter()-start
        if job['state']!='succeeded':raise RuntimeError('Synthetic dense import unavailable')
        start=time.perf_counter();result=Pipeline(root,db).tick();pipeline_seconds=time.perf_counter()-start
        reports=[json.loads(row[0]) for row in db.execute('SELECT result_json FROM analysis_reports')]
        checks=[dict(analyzer_id=check['analyzer_id'],outcome=check['outcome'],row_limit_reached=check['provenance']['context'].get('row_limit_reached',False),
            unavailable=check['unavailable']) for report in reports for check in report['checks']]
    return dict(source='invented_typed_wpilog_with_phoenix_updates',robot_cycles=count,phoenix_fields_per_cycle=fields,wpilog_bytes=len(raw),
        synthetic_generation_seconds=generation,import_seconds=import_seconds,index_and_automatic_reports_seconds=pipeline_seconds,
        automatic_report_selected_row_limit=MAX_ROWS,automatic_report_scan_limit=MAX_ROWS*5,report_count=len(reports),checks=checks,
        bounded_resource_failure_is_not_health_pass=True,physical_hardware_qualified=False)

def main():
    parser=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--sizes',default='10000,50000');parser.add_argument('--dense-cycles',default='')
    parser.add_argument('--work-dir',type=Path,help='Fresh output directory outside the source checkout; retained when supplied')
    args=parser.parse_args()
    try:run_sizes=sizes(args.sizes,100000);dense_sizes=sizes(args.dense_cycles,20000)
    except ValueError as exc:parser.error(str(exc))
    if not run_sizes and not dense_sizes:parser.error('Select at least one benchmark size')
    temporary=None
    if args.work_dir:
        root=args.work_dir.resolve()
        if root.is_relative_to(REPOSITORY):parser.error('Benchmark artifacts must be outside the Git checkout')
        root.mkdir(parents=True,exist_ok=False)
    else:
        temporary=tempfile.TemporaryDirectory(prefix='rth-synthetic-run-lookup-');root=Path(temporary.name)
    try:
        template=load_template(root/'template') if run_sizes else None
        result=dict(schema_version=1,source='synthetic_only',python_version=sys.version.split()[0],sqlite_version=sqlite3.sqlite_version,
            catalog_benchmarks=[lookup_case(root/('catalog-'+str(count)),count,template) for count in run_sizes],
            dense_wpilog_profiles=[dense_case(root/('dense-'+str(count)),count) for count in dense_sizes],
            physical_performance_qualified=False)
        print(json.dumps(result,indent=2,allow_nan=False))
    finally:
        if temporary is not None:temporary.cleanup()

if __name__=='__main__':main()
