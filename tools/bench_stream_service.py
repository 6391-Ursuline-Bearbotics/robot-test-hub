"""Actual local service benchmark; generated SYNTHETIC evidence, loopback only.

Run --baseline <verified source root> --output <private output directory>.
Each isolated child imports the same digest-pinned recording, then starts the
actual service workers, HTTP listener and waits. No scheduling path is bypassed.
Wall-work duty is not CPU or Driver Station qualification. Import is separate;
peak RSS includes import. Concurrent host load is uncontrolled.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

REPOSITORY=Path(__file__).resolve().parents[1]

def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()

class Stats:
    def __init__(self):
        self.count=0;self.seconds=0.;self.maximum=0.;self.buckets=[0]*64;self.lock=threading.Lock()
    def add(self,value):
        with self.lock:
            self.count+=1;self.seconds+=value;self.maximum=max(self.maximum,value)
            self.buckets[min(63,max(0,math.ceil(math.log2(max(1,value*1e6)))))]+=1
    def result(self):
        with self.lock:
            return dict(count=self.count,seconds=self.seconds,max_seconds=self.maximum,log2_microsecond_buckets=self.buckets.copy())

def fixture(path,cycles,fields):
    sys.path[:0]=[str(REPOSITORY),str(REPOSITORY/'tests')]
    from test_wpilog import log_bytes,start,wire_record
    entries={};initial=[]
    def declare(name,kind,value,metadata='{"source":"AdvantageKit"}'):
        entry=len(entries)+2;entries[name]=entry
        initial.extend((start(entry,name,kind,metadata),wire_record(entry,value)))
    for name,value in (('/Metadata/RobotId','service-bench-robot'),('/Metadata/BootId','service-bench-boot'),('/RealOutputs/TestHub/RobotId','service-bench-robot'),('/RealOutputs/TestHub/BootId','service-bench-boot'),('/RealOutputs/TestHub/RuntimeMode','SIM'),('/RealOutputs/TestHub/Mode','TELEOPERATED'),('/RealOutputs/TestHub/ConfigurationSHA256','c'*64)):
        declare(name,'string',value.encode())
    declare('/RealOutputs/TestHub/StateKnown','boolean',b'\1')
    for name in ('/DriverStation/Enabled','/RealOutputs/TestHub/Enabled'):declare(name,'boolean',b'\0')
    declare('/SystemStats/EpochTimeValid','boolean',b'\1')
    declare('/SystemStats/EpochTime','double',struct.pack('<d',1800000000000000),'{"unit":"microseconds"}')
    declare('/Drive/Module0/DriveConnected','boolean',b'\1')
    with path.open('wb') as output:
        output.write(log_bytes(*initial))
        for cycle in range(1,2*cycles+4):
            enabled=cycle<=cycles or cycles+2<=cycle<=2*cycles+1
            stamp=1000000000+cycle*20000000;ts=stamp//1000
            output.write(wire_record(1,struct.pack('<q',stamp),ts))
            for name in ('/DriverStation/Enabled','/RealOutputs/TestHub/Enabled'):output.write(wire_record(entries[name],bytes([enabled]),ts))
            output.write(wire_record(entries['/SystemStats/EpochTime'],struct.pack('<d',1800000000000000+cycle*20000),ts))
            output.write(wire_record(entries['/Drive/Module0/DriveConnected'],bytes([cycle not in (cycles,2*cycles+1)]),ts))
            # Repeated observations of an allowlisted connection field are retained
            # individually, as in the existing dense stream qualification fixture.
            output.write(wire_record(entries['/Drive/Module0/DriveConnected'],b'\1',ts)*fields)

def worker(args):
    source=Path(args.source).resolve();sys.path[:0]=[str(source),str(source/'tools')]
    from bench_stream_analysis import memory
    from robot_test_hub import reports,pipeline,service,stream_analysis
    from robot_test_hub.config import Config
    from robot_test_hub.demo import OfflineSource
    from robot_test_hub.importer import Importer
    from robot_test_hub.storage import open_catalog,DataRootOwner
    from robot_test_hub.server import create_http_server
    if Path(service.__file__).resolve()!=source/'robot_test_hub/service.py':raise RuntimeError('Wrong service source')
    recording=Path(args.fixture);original_hash=digest(recording)
    if original_hash!=args.sha256:raise RuntimeError('Fixture identity changed')
    stats={name:Stats() for name in ('stream','maintenance','catalog','indexer_wait','http','status')};read_bytes={};last_status=[None]
    root=Path(args.archive);root.mkdir()
    started=time.perf_counter();db=open_catalog(root)
    try:
        imported=Importer(root,db).import_file(recording,source_type='SYNTHETIC')
        if imported['state']!='succeeded':raise RuntimeError(imported)
    finally:db.close()
    import_seconds=time.perf_counter()-started
    advance=reports.advance_streams;tick=pipeline.Pipeline.tick;rebuild=pipeline.rebuild_from_imports;read=stream_analysis.StreamRunner._read
    nested=[0.]
    def measured_advance(*a,**kw):
        begin=time.perf_counter()
        try:return advance(*a,**kw)
        finally:
            elapsed=time.perf_counter()-begin;stats['stream'].add(elapsed);nested[0]+=elapsed
    def measured_tick(*a,**kw):
        before=nested[0];begin=time.perf_counter()
        try:return tick(*a,**kw)
        finally:stats['maintenance'].add(max(0,time.perf_counter()-begin-(nested[0]-before)))
    def measured_rebuild(*a,**kw):
        begin=time.perf_counter()
        try:return rebuild(*a,**kw)
        finally:stats['catalog'].add(time.perf_counter()-begin)
    def measured_read(self,rt,item,kind,*a,**kw):
        result=read(self,rt,item,kind,*a,**kw);read_bytes[kind]=read_bytes.get(kind,0)+result[0];return result
    reports.advance_streams=measured_advance;pipeline.Pipeline.tick=measured_tick;pipeline.rebuild_from_imports=measured_rebuild;stream_analysis.StreamRunner._read=measured_read
    class ObservedOffline(OfflineSource):
        def status(self):
            now=time.perf_counter()
            if last_status[0] is not None:stats['status'].add(now-last_status[0])
            last_status[0]=now;return super().status()
        def read(self,*a):raise AssertionError('No source reads authorized')
    class ObservedEvent:
        def __init__(self):self.event=threading.Event()
        def set(self):return self.event.set()
        def is_set(self):return self.event.is_set()
        def wait(self,timeout=None):
            begin=time.perf_counter()
            try:return self.event.wait(timeout)
            finally:
                if threading.current_thread().name=='hub-indexer':stats['indexer_wait'].add(time.perf_counter()-begin)
    adapter=ObservedOffline();hub=service.HubService(Config(data_dir=str(root),stream_min_free_bytes=0),adapter);hub.stop=ObservedEvent()
    httpd=create_http_server(hub,adapter,0);http_thread=threading.Thread(target=httpd.serve_forever,name='bench-http');http_thread.start()
    wall=time.perf_counter();cpu=time.process_time();closed=False
    try:
        hub.start();db=sqlite3.connect(root/'catalog.sqlite3',timeout=2)
        try:
            while True:
                if time.perf_counter()-wall>args.timeout:raise RuntimeError('Actual-service benchmark timed out')
                begin=time.perf_counter()
                with urllib.request.urlopen('http://127.0.0.1:'+str(httpd.server_port)+'/api/status',timeout=5) as response:
                    if len(response.read(1024*1024+1))>1024*1024:raise RuntimeError('Oversize status')
                stats['http'].add(time.perf_counter()-begin)
                if hub.health['indexer']['state']=='failed':raise RuntimeError('Indexer failed')
                jobs=db.execute('SELECT state,report_id FROM analysis_stream_jobs').fetchall()
                if len(jobs)==2 and all(state=='ready' and report for state,report in jobs):break
                time.sleep(.1)
            report_rows=db.execute('SELECT report_id,result_json FROM analysis_reports ORDER BY report_id').fetchall()
            canonical=json.dumps(report_rows,separators=(',',':'),ensure_ascii=False).encode()
            Path(args.output+'.reports.json').write_bytes(canonical)
            storage={name:(root/name).stat().st_size if (root/name).exists() else 0 for name in ('catalog.sqlite3','catalog.sqlite3-wal','catalog.sqlite3-shm')}
            spool=db.execute('SELECT COALESCE(SUM(spool_bytes),0) FROM analysis_stream_jobs').fetchone()[0]
        finally:db.close()
        result=dict(source_root=str(source),module_sha256={name:digest(source/'robot_test_hub'/name) for name in ('service.py','pipeline.py')},fixture_sha256=original_hash,fixture_bytes=recording.stat().st_size,import_seconds=import_seconds,service_wall_seconds=time.perf_counter()-wall,service_process_cpu_seconds=time.process_time()-cpu,statistics={name:value.result() for name,value in stats.items()},stream_read_bytes=read_bytes,spool_bytes=spool,storage_bytes=storage,memory=memory(),report_count=len(report_rows),report_ids=[row[0] for row in report_rows],full_ordered_reports_sha256=hashlib.sha256(canonical).hexdigest(),limitations=['SYNTHETIC recording; no physical or DS qualification','Host concurrency uncontrolled; wall-work pacing is not CPU duty','Importer/catalog/legacy maintenance remain synchronous','Read counters are logical stream reads, not physical device IO; peak RSS includes import'])
    finally:
        begin=time.perf_counter();closed=hub.close();shutdown_seconds=time.perf_counter()-begin
        httpd.shutdown();httpd.server_close();http_thread.join()
        if not closed:raise RuntimeError('Service cleanup pending; archive retained')
    owner=DataRootOwner(root);owner.close()
    if digest(recording)!=original_hash:raise RuntimeError('Original changed')
    result.update(shutdown_seconds=shutdown_seconds,closed=closed,owner_reacquired=True)
    Path(args.output).write_text(json.dumps(result,indent=2),encoding='utf-8')

def cleanup_owned_workspace(root):
    """Delete only the fresh, ordinary TEMP tree owned by this controller."""
    temp_parent=Path(tempfile.gettempdir()).resolve()
    if root.parent!=temp_parent or not root.name.startswith('hub-service-benchmark-') or root.resolve()!=root:
        raise RuntimeError('Benchmark cleanup target failed ownership/path check')
    def ordinary(path):
        info=path.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&getattr(stat,'FILE_ATTRIBUTE_REPARSE_POINT',0x400):
            raise RuntimeError('Benchmark cleanup refuses symlink/reparse target: '+str(path))
        resolved=path.resolve()
        if resolved!=root and root not in resolved.parents:
            raise RuntimeError('Benchmark cleanup target escaped owned workspace')
    ordinary(root)
    for directory,dirs,files in os.walk(root,followlinks=False):
        for name in dirs+files:ordinary(Path(directory)/name)
    shutil.rmtree(root)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline');parser.add_argument('--output',required=True);parser.add_argument('--cycles',type=int,default=300);parser.add_argument('--fields',type=int,default=180);parser.add_argument('--timeout',type=float,default=900)
    parser.add_argument('--source');parser.add_argument('--fixture');parser.add_argument('--sha256');parser.add_argument('--archive')
    args=parser.parse_args()
    if args.source:return worker(args)
    if not args.baseline:parser.error('--baseline required')
    if not 1<=args.cycles<=1000 or not 1<=args.fields<=208:parser.error('Fixture bounds exceeded')
    output=Path(args.output).resolve();output.mkdir(parents=True,exist_ok=False)
    recording=output/'invented-service.wpilog';begin=time.perf_counter();fixture(recording,args.cycles,args.fields);fixture_seconds=time.perf_counter()-begin
    temporary=Path(tempfile.mkdtemp(prefix='hub-service-benchmark-')).resolve()
    try:
        for label,source in (('baseline',Path(args.baseline)),('candidate',REPOSITORY)):
            # subprocess.run kills and reaps the owned child on TimeoutExpired.
            # Preserve its archive on any failure; never report cleanup success.
            subprocess.run([sys.executable,'-I',str(Path(__file__).resolve()),'--source',str(source),'--fixture',str(recording),'--sha256',digest(recording),'--archive',str(temporary/label),'--output',str(output/(label+'.json')),'--timeout',str(args.timeout)],check=True,timeout=args.timeout+60)
        baseline=json.loads((output/'baseline.json').read_text());candidate=json.loads((output/'candidate.json').read_text())
        equal=baseline['full_ordered_reports_sha256']==candidate['full_ordered_reports_sha256'] and baseline['report_ids']==candidate['report_ids']
        (output/'comparison.json').write_text(json.dumps(dict(full_reports_equal=equal,fixture_seconds=fixture_seconds,fixture_sha256=digest(recording),cycles_per_run=args.cycles,observations_per_cycle=args.fields+5,wall_ratio=candidate['service_wall_seconds']/baseline['service_wall_seconds']),indent=2))
        if not equal:raise RuntimeError('Full saved report compatibility failed')
        cleanup_owned_workspace(temporary)
    except BaseException:
        print('Benchmark failed; owned workspace retained: '+str(temporary),file=sys.stderr)
        raise

if __name__=='__main__':main()
