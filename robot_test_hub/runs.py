"""Reproducible run intervals derived from explicitly mapped import cycles."""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation
import hashlib
import json
import sqlite3
from typing import Iterable

from .timebase import Anchor, NS, build_mapping

VERSION = "runs-1"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _alias(cycle, name, default=None):
    return cycle.get("aliases", {}).get(name, {}).get("value", default)


def _id(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _enabled(cycle):
    known = _alias(cycle, 'state_known')
    if known is False:
        return None
    return _alias(cycle, 'status_enabled') if known is True else _alias(cycle, 'enabled')


def derive_runs(datasets: Iterable[tuple[str, Iterable[dict]]], *, max_gap_ns=250_000_000):
    """Input IDs identify immutable imported datasets, never filenames.

    Boundary gaps end runs conservatively. Mode aliases are profile-defined by
    the importer; fixture_mode is only a fallback for the explicit fixture schema.
    """
    if type(max_gap_ns) is not int or max_gap_ns <= 0:
        raise ValueError("Maximum run gap must be positive integer nanoseconds")
    groups, fingerprints = defaultdict(list), []
    for segment_id, records in datasets:
        cycles = [row for row in records if row.get("kind") == "cycle"]
        fingerprints.append([segment_id, _id(cycles)])
        for row in cycles:
            boot = _alias(row, "status_boot_id") or _alias(row, "real_metadata_boot_id") or _alias(row, "boot_id")
            robot = _alias(row, "status_robot_id") or _alias(row, "real_metadata_robot_id") or _alias(row, "robot_id")
            known_boot = isinstance(boot, str) and bool(boot)
            boot = boot if known_boot else "unknown-boot:" + segment_id
            groups[(str(robot or "unknown"), boot)].append({**row, "segment_ids": [segment_id],
                "known_boot": known_boot, "timestamp_ns": int(row["timestamp_ns"])})
    revision = _id([VERSION, max_gap_ns, sorted(fingerprints)])
    runs, mappings = [], []
    for (robot, boot), raw in sorted(groups.items()):
        by_time = {}
        for row in sorted(raw, key=lambda x: (x["timestamp_ns"], x["segment_ids"])):
            t = row["timestamp_ns"]
            if t not in by_time:
                by_time[t] = row
            else:
                old = by_time[t]
                old["segment_ids"] = sorted(set(old["segment_ids"] + row["segment_ids"]))
                # Duplicate segment overlap must agree on semantic aliases.
                if {k: v.get("value") for k,v in old.get("aliases",{}).items()} != {
                        k: v.get("value") for k,v in row.get("aliases",{}).items()}:
                    old["conflict"] = True
                else:
                    old['aliases'] = {key: {**value, 'updated_in_cycle':
                        value.get('updated_in_cycle', False) or row['aliases'][key].get('updated_in_cycle', False)}
                        for key, value in old.get('aliases', {}).items()}
        cycles = list(by_time.values())
        anchors = []
        for row in cycles:
            utc = None
            valid = _alias(row, "epoch_valid") is True and not row.get("conflict")
            # Epoch can be held by AK between cycles, but a held value is not a
            # fresh anchor at the later robot timestamp.
            sample = row.get("aliases", {}).get("epoch_us", {})
            if valid and sample.get("updated_in_cycle", False):
                try:
                    value = Decimal(str(sample["value"])) * 1000
                    if value.is_finite() and value == value.to_integral_value():
                        utc = int(value)
                except (ValueError, KeyError, InvalidOperation):
                    pass
            if valid and not sample.get('updated_in_cycle', False):
                continue
            uncertainty = sample.get("uncertainty_ns", 1_000_000)
            anchors.append(Anchor(row["timestamp_ns"], utc, valid and utc is not None,
                                  uncertainty if type(uncertainty) is int and uncertainty >= 0 else 1_000_000))
        mapping = build_mapping(boot, revision, anchors, max_gap_ns=max_gap_ns)
        mappings.append({"robot_id":robot, "boot_id":boot, "revision":revision,
            "pieces":[{"start_ns":str(p.start_ns), "end_ns":str(p.end_ns), "reason":p.reason,
                       "anchors":[{"robot_ns":str(a.robot_ns),"utc_ns":str(a.utc_ns),
                                   "uncertainty_ns":str(a.uncertainty_ns)} for a in p.anchors]} for p in mapping.pieces],
            "rejected":list(mapping.rejected)})
        active = None
        previous = None

        def finish(end, complete, why):
            nonlocal active
            if active is None:
                return
            active["end_monotonic_ns"] = str(end)
            active["end_complete"] = complete
            active["completeness"] = "complete" if active["start_complete"] and complete else "incomplete"
            active["end_reason"] = why
            active["phases"][-1]["end_monotonic_ns"] = str(end)
            intervals = []
            start = int(active["start_monotonic_ns"])
            for piece in mapping.pieces:
                lo, hi = max(start, piece.start_ns), min(end, piece.end_ns)
                if lo <= hi:
                    left, right = mapping.map(boot, lo), mapping.map(boot, hi)
                    intervals.append({"robot_start_ns":str(lo),"robot_end_ns":str(hi),
                                      "utc_start_ns":left["utc_ns"],"utc_end_ns":right["utc_ns"],
                                      "uncertainty_ns":str(max(int(left["uncertainty_ns"]),int(right["uncertainty_ns"])))})
            active["utc_intervals"] = intervals
            active["wall_clock_quality"] = "partial" if intervals else "unavailable"
            if intervals and len(intervals)==1 and intervals[0]["robot_start_ns"] == str(start) and intervals[0]["robot_end_ns"]==str(end):
                active["wall_clock_quality"] = "anchored"
            active["segment_ids"] = sorted(active["segment_ids"])
            runs.append(active)
            active = None

        for row in cycles:
            t = row["timestamp_ns"]
            enabled = _enabled(row)
            if row.get("conflict"):
                enabled = None
            gap = previous is not None and t-previous["timestamp_ns"] > max_gap_ns
            if gap:
                finish(previous["timestamp_ns"], False, "recording_gap")
            if type(enabled) is not bool:
                finish(previous["timestamp_ns"] if previous else t, False, "unknown_state")
            elif not enabled:
                if active:
                    active["segment_ids"].update(row["segment_ids"])
                finish(t, True, "disabled")
            else:
                logged_id = _alias(row, "run_id")
                if active and logged_id and active["logged_run_id"] and logged_id != active["logged_run_id"]:
                    finish(previous["timestamp_ns"] if previous else t, False, "run_identity_change")
                mode = str(_alias(row, 'status_mode') or _alias(row, "robot_mode") or _alias(row, "fixture_mode") or "UNKNOWN")
                if active is None:
                    start_complete = (previous is not None and not gap
                                      and not previous.get("conflict") and _enabled(previous) is False)
                    identity = _id([robot, boot, logged_id, t])
                    active = {"run_id":identity,"logged_run_id":logged_id,"robot_id":robot,"boot_id":boot,
                              "known_boot":row["known_boot"],"mapping_revision":revision,
                              "runtime_mode":_alias(row,'runtime_mode','unknown'),
                              "source_types":[],
                              "start_monotonic_ns":str(t),"start_complete":start_complete,
                              "segment_ids":set(),"phases":[]}
                active["segment_ids"].update(row["segment_ids"])
                active['source_types'] = sorted(set(active['source_types']+[row.get('source_type','unknown')]))
                if not active["phases"] or active["phases"][-1]["mode"] != mode:
                    if active["phases"]:
                        active["phases"][-1]["end_monotonic_ns"] = str(t)
                    active["phases"].append({"mode":mode,"start_monotonic_ns":str(t)})
            previous = row
        if previous:
            finish(previous["timestamp_ns"], False, "end_of_recording")
    return {"revision":revision,"version":VERSION,"runs":runs,"mappings":mappings,
            "inputs":sorted(fingerprints),"max_gap_ns":str(max_gap_ns)}


def install_schema(db):
    db.execute("CREATE TABLE IF NOT EXISTS run_catalog_revisions (id TEXT PRIMARY KEY, document TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS run_catalog_state (id INTEGER PRIMARY KEY CHECK(id=1), revision TEXT NOT NULL REFERENCES run_catalog_revisions(id))")


class RunCatalog:
    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def rebuild(self, datasets, **policy):
        document = derive_runs(datasets, **policy)
        from .run_lookup import publish_document
        return publish_document(self.db,document)

    def current(self):
        row = self.db.execute("SELECT document FROM run_catalog_revisions r JOIN run_catalog_state s ON s.revision=r.id WHERE s.id=1").fetchone()
        return json.loads(row[0]) if row else {"revision":None,"runs":[],"mappings":[]}

    def search(self, *, utc_from_ns=None, utc_to_ns=None, robot=None, include_unknown=False, offset=0, limit=50):
        from .run_lookup import search
        return search(self.db,utc_from_ns=utc_from_ns,utc_to_ns=utc_to_ns,robot=robot,
            include_unknown=include_unknown,offset=offset,limit=limit)


def rebuild_from_imports(root, db):
    from .importer import Importer
    importer = Importer(root, db)
    # Old derived revisions remain immutable, but must not be mixed as if they
    # were separate recordings of the same physical bytes.
    latest={}
    # Wall clocks can have equal resolution. For equal update
    # and creation times, catalog insertion order identifies the later job;
    # arbitrary content hashes do not establish which mapping was introduced last.
    insertion_order={row['id']:row['rowid'] for row in db.execute('SELECT rowid,id FROM import_jobs')}
    def revision_order(job):
        return (job['updated_utc_ns'],job['created_utc_ns'],insertion_order[job['id']])
    for job in importer.list_jobs():
        if job['state'] in ('succeeded','succeeded_with_unsupported'):
            key=(job['artifact_sha256'],job['profile'])
            previous=latest.get(key)
            if previous is None or revision_order(job) > revision_order(previous):
                latest[key]=job
    jobs=list(latest.values())
    def datasets():
        for job in jobs:
            source_type=importer.read_manifest(job['id'])['source_type']
            yield job['id'], ({**row,'source_type':source_type} for row in importer.iter_dataset(job['id'],kind='cycle'))
    return RunCatalog(db).rebuild(datasets())


def main():
    import argparse
    from pathlib import Path
    from .storage import DataRootOwner, open_catalog
    parser=argparse.ArgumentParser(description='Rebuild run/time catalog from successful immutable imports')
    parser.add_argument('--data-dir',type=Path,required=True)
    args=parser.parse_args()
    owner=DataRootOwner(args.data_dir)
    try:
        db=open_catalog(owner.root)
        try:
            document=rebuild_from_imports(owner.root,db)
            print(json.dumps({'revision':document['revision'],'runs':len(document['runs'])}))
        finally:
            db.close()
    finally:
        owner.close()


if __name__=='__main__':
    main()
