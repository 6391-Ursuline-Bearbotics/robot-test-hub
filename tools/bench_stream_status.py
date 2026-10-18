"""SYNTHETIC S2 qualification using the full source checkout's test generator.

Each stream runs in a fresh -I child, excluding fixture/import allocations.
Use --work-dir outside this checkout to retain invented evidence and receipts.
"""
import argparse
from contextlib import closing
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import time

REPOSITORY=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(REPOSITORY),str(REPOSITORY/'tests'),str(REPOSITORY/'tools')]
from bench_stream_analysis import memory


def worker(root):
    from unittest.mock import patch
    from robot_test_hub.storage import open_catalog,DataRootOwner
    from robot_test_hub.importer import Importer
    from robot_test_hub.runs import RunCatalog
    from robot_test_hub.reports import generate,advance_streams,StreamReportRunner
    before=memory();histogram=[0]*64;turns=total_ns=maximum_ns=0
    started=time.perf_counter()
    with closing(DataRootOwner(root)),closing(open_catalog(root)) as db:
        runner=StreamReportRunner(root,db)
        try:
            with patch.object(Importer,'read_manifest',side_effect=AssertionError('legacy admission')),patch.object(Importer,'iter_dataset',side_effect=AssertionError('legacy iteration')),patch.object(Importer,'_check_dataset',side_effect=AssertionError('legacy hashing')):
                tick=time.perf_counter();scheduled=generate(root,db,RunCatalog(db).current(),pipeline_version='automatic-reports-9');admission=time.perf_counter()-tick
                if len(scheduled)!=1:raise RuntimeError('Expected one invented run')
                while turns<1000000:
                    tick=time.perf_counter_ns();progress=advance_streams(root,db,runner);elapsed=time.perf_counter_ns()-tick
                    turns+=1;total_ns+=elapsed;maximum_ns=max(maximum_ns,elapsed)
                    bucket=min(63,max(0,int(math.ceil(math.log2(max(1,elapsed/1000))))));histogram[bucket]+=1
                    if progress.get('state')=='failed':raise RuntimeError(progress['error_code'])
                    if progress.get('report') is not None:break
                else:raise RuntimeError('Benchmark work-turn allowance exhausted')
            report=progress['report'];checks={check['analyzer_id']:check for check in report['checks']}
            sdk=checks['phoenix-status-observation'];system=checks['system-status-observation']
            sdk_warning=next(f for f in sdk['findings'] if f['signal']=='DriveVelocity')
            flag=next(f for f in system['findings'] if f['kind']=='recorded_system_fault_true')
            counter=next(f for f in system['findings'] if f['kind']=='recorded_system_fault_counter_increased')
            if (sdk_warning['observed_false_status_records'],flag['observations'],counter['observations'])!=(1,1,1):raise RuntimeError('Late warnings not counted exactly once')
            if report['source_type']!='synthetic' or report['overall_health']!='not_assessed':raise RuntimeError('Source or health claim changed')
            if any(c['provenance']['full_source_verified'] is not True for c in checks.values()):raise RuntimeError('Unverified source')
            for name in ('swerve-tracking','recorded-steering-observation'):
                if checks[name]['unavailable']!=['stream_evaluator_not_converted']:raise RuntimeError('Unconverted check falsely evaluated')
            def quantile(fraction):
                accumulated=0
                for bucket,count in enumerate(histogram):
                    accumulated+=count
                    if accumulated>=math.ceil(turns*fraction):return 2**bucket/1000
            return dict(source='SYNTHETIC',python=sys.version.split()[0],platform=sys.platform,
                active_seconds=time.perf_counter()-started,admission_seconds=admission,turns=turns,
                mean_turn_ms=total_ns/turns/1000000,max_turn_ms=maximum_ns/1000000,
                p95_bucket_upper_ms=quantile(.95),latency_basis='fixed logarithmic microsecond histogram',
                memory_before=before,memory_after=memory(),scanned_rows=progress['scanned_rows'],
                selected_bytes=progress['selected_bytes'],spool_bytes=progress['spool_bytes'],chunk_count=progress['chunk_count'],
                selection_chain=progress['selection_chain'],input_digest=progress['descriptor']['input_digest'],
                sdk_expected_cycles=sdk['coverage']['expected_cycles'],system_expected_cycles=system['coverage']['expected_cycles'],
                sdk_false_records=1,system_true_records=1,system_counter_changes=1,unconverted_checks=2,
                voltage_metrics=system['metrics'],physical_hardware_qualified=False)
        finally:
            if runner.close() is not True:raise RuntimeError('Streaming cleanup did not complete')


def case(root,cycles,prefix):
    from test_stream_status_review import status_recording,FLAG,COUNT,SDK_FLAG,SDK_CODE
    from robot_test_hub.storage import open_catalog
    from robot_test_hub.importer import Importer
    from robot_test_hub.runs import rebuild_from_imports
    root.mkdir();started=time.perf_counter()
    source=status_recording(root/'invented-status-benchmark.wpilog',cycles=cycles,prefix_records=prefix,
        updates={cycles:{FLAG:True,COUNT:3,SDK_FLAG:False,SDK_CODE:-7},cycles+1:{COUNT:3}})
    generated=time.perf_counter()-started
    with closing(open_catalog(root)) as db:
        started=time.perf_counter();job=Importer(root,db).import_file(source,source_type='SYNTHETIC');imported=time.perf_counter()-started
        if job['state']!='succeeded':raise RuntimeError('Synthetic import failed')
        started=time.perf_counter();document=rebuild_from_imports(root,db);indexed=time.perf_counter()-started
        if len(document['runs'])!=1:raise RuntimeError('Unexpected run count')
    started=time.perf_counter()
    child=subprocess.run([sys.executable,'-I',str(Path(__file__).resolve()),'--worker',str(root)],capture_output=True,text=True,encoding='utf-8',timeout=600)
    if child.returncode or len(child.stdout.encode())>65536:raise RuntimeError('Benchmark child failed: '+child.stderr[-4000:])
    return dict(cycles=cycles,prefix_records=prefix,wpilog_bytes=source.stat().st_size,generated_seconds=generated,
        import_seconds=imported,run_catalog_seconds=indexed,isolated_child_seconds=time.perf_counter()-started,stream=json.loads(child.stdout))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-dir',type=Path);parser.add_argument('--worker',type=Path,help=argparse.SUPPRESS)
    args=parser.parse_args()
    if args.worker:print(json.dumps(worker(args.worker.resolve()),allow_nan=False));return
    temporary=None
    if args.work_dir:
        root=args.work_dir.resolve()
        if root.is_relative_to(REPOSITORY):parser.error('Invented artifacts must be outside the checkout')
        root.mkdir(parents=True,exist_ok=False)
    else:temporary=tempfile.TemporaryDirectory(prefix='S2 benchmark ');root=Path(temporary.name)
    try:
        receipt=dict(schema_version=1,source='SYNTHETIC',hardware_qualified=False,
            cases=[case(root/'small',60,0),case(root/'dense',300,250001)])
        (root/'benchmark.json').write_text(json.dumps(receipt,indent=2,allow_nan=False),encoding='utf-8')
        print(json.dumps(receipt,indent=2,allow_nan=False))
    finally:
        if temporary is not None:temporary.cleanup()


if __name__=='__main__':main()
