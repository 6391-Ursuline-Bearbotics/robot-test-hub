"""Optional single-machine background analytics; immutable reports, no robot I/O."""
from __future__ import annotations

import argparse
from contextlib import closing
import json
import hashlib
from pathlib import Path
import re
import sqlite3
import time

from .backup import _path
from .columnar import ColumnarError, Converter, dependency
from .config import Config
from .importer import EXTRACTOR_VERSION, MAPPING_REVISION
from .runs import RunCatalog
from .storage import DataRootOwner, open_catalog
from .summaries import FILES, Summarizer
from .wpilog import PROFILE


class AnalyticsWorker:
    def __init__(self, root, db, config, *, stopping=lambda:False, publish=lambda value:None):
        dependency()
        self.root,self.db,self.config,self.stopping,self.publish=Path(root).resolve(),db,config,stopping,publish
        self.converter=Converter(root,db,threads=config.analytics_threads,
                                 memory_mb=config.analytics_memory_mb,stopping=stopping)
        self.summarizer=Summarizer(root,db,self.converter)
        self.report_retry=0
        last=db.execute("SELECT id FROM summary_reports WHERE state='succeeded' ORDER BY created_utc_ns DESC LIMIT 1").fetchone()
        self.state={"schema_version":1,"enabled":True,"state":"starting","error_code":None,
                    "converted_files":0,"pending_files":0,"retry_files":0,"latest_report_id":last["id"] if last else None,
                    "queried_runs":0,"cached_runs":0,"threads":config.analytics_threads,
                    "memory_limit_mb":config.analytics_memory_mb,
                    "summary_sharing_enabled":config.analytics_share_summaries}

    def emit(self, **values):
        self.state.update(values)
        self.publish(dict(self.state))
        return dict(self.state)

    def tick(self):
        if self.stopping():
            return self.emit(state="paused")
        imports=[dict(row) for row in self.db.execute("""SELECT * FROM import_jobs
            WHERE state IN ('succeeded','succeeded_with_unsupported')
            AND profile=? AND extractor_version=? AND mapping_revision=? ORDER BY created_utc_ns""",
            (PROFILE,EXTRACTOR_VERSION,MAPPING_REVISION))]
        ready=[];pending=[];retry=0
        now=time.time_ns()
        for job in imports:
            key=self.converter.key(job)
            row=self.db.execute("SELECT * FROM columnar_jobs WHERE id=?",(key,)).fetchone()
            if row and row["state"]=="succeeded":
                ready.append(job)
            else:
                pending.append((job,row))
                retry+=int(row is not None and row["state"]=="retry_wait")
        self.emit(converted_files=len(ready),pending_files=len(pending),retry_files=retry,error_code=None)
        for job,row in pending:
            if row and row["next_retry_utc_ns"]>now:
                continue
            self.emit(state="converting")
            try:
                self.converter.convert(job["id"])
                return self.emit(state="processing",converted_files=len(ready)+1,pending_files=len(pending)-1)
            except Exception as exc:
                if self.stopping():
                    with self.db:
                        self.db.execute("UPDATE columnar_jobs SET next_retry_utc_ns=0 WHERE id=?",(self.converter.key(job),))
                    return self.emit(state="paused")
                return self.emit(state="retry_wait",error_code=type(exc).__name__)
        if pending:
            return self.emit(state="retry_wait")
        if not imports:
            return self.emit(state="waiting_for_imports")
        document=RunCatalog(self.db).current()
        # The independent indexer owns run discovery. Wait until it includes new imports.
        mappings={item[0] for item in document.get("inputs",[])}
        if any(job["id"] not in mappings for job in imports):
            return self.emit(state="waiting_for_index")
        if time.monotonic()<self.report_retry:
            return self.emit(state="retry_wait")
        self.emit(state="summarizing")
        try:
            for job in ready:
                self.converter.convert(job["id"])  # Verify registered derivatives once per worker lifetime.
            report,queried,cached=self.summarizer.generate()
            return self.emit(state="idle",latest_report_id=report["id"],queried_runs=queried,cached_runs=cached)
        except Exception as exc:
            if self.stopping():
                return self.emit(state="paused")
            self.report_retry=time.monotonic()+60
            return self.emit(state="retry_wait",error_code=type(exc).__name__)


def report_file(root,report_id,name):
    """Allowlisted small report download with identity/hash validation."""
    if not re.fullmatch("[0-9a-f]{64}",report_id) or name not in FILES:
        raise KeyError("Unknown report")
    root=Path(root).resolve()
    with closing(sqlite3.connect(root/"catalog.sqlite3",timeout=2)) as db:
        row=db.execute("""SELECT a.path,a.sha256,a.size_bytes FROM summary_artifacts a
            JOIN summary_reports r ON r.id=a.report_id WHERE r.state='succeeded'
            AND r.id=? AND a.path=?""",(report_id,"analytics/reports/"+report_id+"/"+name)).fetchone()
    if row is None:
        raise KeyError("Report unavailable")
    path=_path(root,row[0])
    if not 0<=row[2]<=64*1024*1024:
        raise ColumnarError("Report size limit")
    with path.open("rb") as stream:
        payload=stream.read(row[2]+1)
    if len(payload)!=row[2] or hashlib.sha256(payload).hexdigest()!=row[1]:
        raise ColumnarError("Report integrity mismatch")
    return payload


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir",required=True)
    parser.add_argument("--threads",type=int,default=1)
    parser.add_argument("--memory-mb",type=int,default=512)
    args=parser.parse_args(argv)
    config=Config(data_dir=args.data_dir,analytics_enabled=True,analytics_threads=args.threads,
                  analytics_memory_mb=args.memory_mb)
    from .runs import rebuild_from_imports
    owner=DataRootOwner(Path(args.data_dir).resolve())
    try:
        with closing(open_catalog(owner.root)) as db:
            rebuild_from_imports(owner.root,db)
            worker=AnalyticsWorker(owner.root,db,config)
            while True:
                state=worker.tick()
                if state["state"] not in ("processing","converting","summarizing"):
                    print(json.dumps(state,sort_keys=True))
                    return 0 if state["state"]=="idle" else 1
    finally:
        owner.close()



if __name__=="__main__":
    raise SystemExit(main())
