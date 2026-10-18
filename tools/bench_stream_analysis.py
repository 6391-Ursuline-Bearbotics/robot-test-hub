"""SYNTHETIC-only S1 qualification; requires this full source tree and test generator.

Example: python tools/bench_stream_analysis.py --prefixes 0,250001 --cycles 300
The controller measures fixture/import/catalog work separately. Each stream runs
in a fresh -I child, with legacy synchronous admission helpers prohibited. Peak
memory therefore excludes importer/fixture allocations. Timing histograms are
fixed-size; the benchmark itself does not retain a per-turn sample list.
"""
from contextlib import closing
import argparse
import ctypes
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

REPOSITORY=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(REPOSITORY),str(REPOSITORY/'tests')]


def memory():
    if os.name=='nt':
        class Counters(ctypes.Structure):
            _fields_=[('cb',ctypes.c_ulong),('PageFaultCount',ctypes.c_ulong)]+[(key,ctypes.c_size_t) for key in (
                'PeakWorkingSetSize','WorkingSetSize','QuotaPeakPagedPoolUsage','QuotaPagedPoolUsage',
                'QuotaPeakNonPagedPoolUsage','QuotaNonPagedPoolUsage','PagefileUsage','PeakPagefileUsage')]
        kernel=ctypes.WinDLL('kernel32',use_last_error=True);kernel.GetCurrentProcess.restype=ctypes.c_void_p
        psapi=ctypes.WinDLL('psapi',use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes=[ctypes.c_void_p,ctypes.POINTER(Counters),ctypes.c_ulong]
        data=Counters();data.cb=ctypes.sizeof(data)
        if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(),ctypes.byref(data),data.cb):raise OSError(ctypes.get_last_error())
        return dict(peak_resident_bytes=data.PeakWorkingSetSize,current_resident_bytes=data.WorkingSetSize,method='Windows GetProcessMemoryInfo')
    import resource
    peak=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1 if sys.platform=='darwin' else 1024)
    return dict(peak_resident_bytes=peak,current_resident_bytes=None,method='getrusage maximum resident set')


def worker(root):
    from unittest.mock import patch
    from robot_test_hub.storage import open_catalog,DataRootOwner
    from robot_test_hub.importer import Importer
    from robot_test_hub.runs import RunCatalog
    from robot_test_hub.reports import schedule_stream_reports,advance_streams
    from robot_test_hub.stream_analysis import StreamRunner
    from robot_test_hub.stream_quality import RecordingQualityReducer
    before=memory();histogram=[0]*64;turns=0;total_ns=0;maximum_ns=0
    started=time.perf_counter()
    with closing(DataRootOwner(root)),closing(open_catalog(root)) as db:
        document=RunCatalog(db).current();runner=StreamRunner(root,db,reducer=RecordingQualityReducer())
        try:
            with patch.object(Importer,'read_manifest',side_effect=AssertionError('legacy manifest admission')),patch.object(Importer,'iter_dataset',side_effect=AssertionError('legacy dataset admission')),patch.object(Importer,'_check_dataset',side_effect=AssertionError('legacy source hashing')):
                admission=time.perf_counter();scheduled=schedule_stream_reports(root,db,document,runner=runner);admission=time.perf_counter()-admission
                if len(scheduled)!=1:raise RuntimeError('Expected one invented run')
                while turns<1000000:
                    tick=time.perf_counter_ns();progress=advance_streams(root,db,runner);elapsed=time.perf_counter_ns()-tick
                    turns+=1;total_ns+=elapsed;maximum_ns=max(maximum_ns,elapsed)
                    bucket=min(63,max(0,int(math.ceil(math.log2(max(1,elapsed/1000))))));histogram[bucket]+=1
                    if progress.get('state')=='failed':raise RuntimeError(progress['error_code'])
                    if progress.get('report') is not None:break
                else:raise RuntimeError('Benchmark work-turn allowance exhausted')
            report=progress['report'];quality=report['checks'][0]
            if report['source_type']!='synthetic' or len(report['checks'])!=5 or quality['provenance']['full_source_verified'] is not True:raise RuntimeError('Invalid complete stream evidence')
            warning=next(check for check in quality['findings'] if check['kind']=='disconnected_observations')
            if warning['count']!=1:raise RuntimeError('Late source warning not evaluated exactly once')
            if any(check['outcome']!='insufficient_data' or check['unavailable']!=['stream_evaluator_not_converted'] for check in report['checks'][1:]):raise RuntimeError('Unconverted checks falsely evaluated')
            def quantile(fraction):
                accumulated=0
                for bucket,count in enumerate(histogram):
                    accumulated+=count
                    if accumulated>=math.ceil(turns*fraction):return (2**bucket)/1000
            result=dict(source='SYNTHETIC',python=sys.version.split()[0],platform=sys.platform,
                work_seconds=time.perf_counter()-started,admission_seconds=admission,work_turns=turns,
                mean_turn_ms=total_ns/turns/1000000,max_turn_ms=maximum_ns/1000000,
                p50_turn_bucket_upper_ms=quantile(.5),p95_turn_bucket_upper_ms=quantile(.95),
                latency_histogram_kind='fixed logarithmic microsecond buckets; quantiles are upper bounds',
                memory_before=before,memory_after=memory(),
                scanned_rows=progress['scanned_rows'],selected_rows=progress['selected_rows'],selected_bytes=progress['selected_bytes'],
                verified_bytes=progress['verified_bytes'],spool_bytes=progress['spool_bytes'],chunk_count=progress['chunk_count'],
                selection_chain=progress['selection_chain'],input_digest=progress['descriptor']['input_digest'],
                quality_outcome=quality['outcome'],cycle_count=quality['coverage']['cycle_count'],
                span_seconds=quality['coverage']['span_seconds'],late_warning_count=warning['count'],
                retained_source_references=len(warning['source_references']),unconverted_checks=4,
                overall_health=report['overall_health'],physical_hardware_qualified=False)
        finally:
            if runner.close() is not True:raise RuntimeError('Streaming cleanup did not complete')
    return result


def case(root,prefix,cycles):
    from test_stream_review import write_recording
    from robot_test_hub.importer import Importer
    from robot_test_hub.runs import rebuild_from_imports
    from robot_test_hub.storage import open_catalog
    root.mkdir();start=time.perf_counter()
    source=write_recording(root/'invented-benchmark.wpilog',prefix_records=prefix,cycles=cycles,connections_per_cycle=180)
    generated=time.perf_counter()-start
    with closing(open_catalog(root)) as db:
        start=time.perf_counter();job=Importer(root,db).import_file(source,source_type='SYNTHETIC');imported=time.perf_counter()-start
        if job['state']!='succeeded':raise RuntimeError('Synthetic import did not succeed')
        start=time.perf_counter();document=rebuild_from_imports(root,db);indexed=time.perf_counter()-start
        if len(document['runs'])!=1:raise RuntimeError('Unexpected synthetic run count')
    started=time.perf_counter()
    child=subprocess.run([sys.executable,'-I',str(Path(__file__).resolve()),'--worker',str(root)],capture_output=True,text=True,encoding='utf-8',timeout=600)
    if child.returncode or len(child.stdout.encode())>65536:raise RuntimeError('Stream benchmark child failed: '+child.stderr[-4000:])
    result=json.loads(child.stdout)
    return dict(prefix_records=prefix,robot_cycles=cycles,connections_per_cycle=180,wpilog_bytes=source.stat().st_size,
        generated_seconds=generated,import_seconds=imported,run_catalog_seconds=indexed,isolated_child_seconds=time.perf_counter()-started,stream=result)


def main():
    parser=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--prefixes',default='0,250001');parser.add_argument('--cycles',type=int,default=300)
    parser.add_argument('--work-dir',type=Path);parser.add_argument('--worker',type=Path,help=argparse.SUPPRESS)
    args=parser.parse_args()
    if args.worker:
        print(json.dumps(worker(args.worker.resolve()),allow_nan=False));return
    try:prefixes=[int(value) for value in args.prefixes.split(',')]
    except ValueError:parser.error('prefixes must be comma-separated integers')
    if not prefixes or len(prefixes)>10 or any(not 0<=value<=1000000 for value in prefixes) or not 2<=args.cycles<=10000:parser.error('Unsupported benchmark size')
    temporary=None
    if args.work_dir:
        root=args.work_dir.resolve()
        if root.is_relative_to(REPOSITORY):parser.error('Synthetic benchmark artifacts must be outside the checkout')
        root.mkdir(parents=True,exist_ok=False)
    else:
        temporary=tempfile.TemporaryDirectory(prefix='rth stream benchmark ');root=Path(temporary.name)
    try:
        results=[case(root/('case-'+str(index)),prefix,args.cycles) for index,prefix in enumerate(prefixes)]
        receipt=dict(schema_version=1,source='SYNTHETIC',cases=results,hardware_qualified=False)
        (root/'benchmark.json').write_text(json.dumps(receipt,indent=2,allow_nan=False),encoding='utf-8')
        print(json.dumps(receipt,indent=2,allow_nan=False))
    finally:
        if temporary is not None:temporary.cleanup()

if __name__=='__main__':main()
