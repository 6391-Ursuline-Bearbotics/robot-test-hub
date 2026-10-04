import hashlib
from pathlib import Path
import tempfile
import time
import unittest

from robot_test_hub.collector import Collector, LogFile, RobotStatus
from robot_test_hub.importer import Importer
from robot_test_hub.pipeline import Pipeline
from robot_test_hub.runs import RunCatalog
from robot_test_hub.storage import DataRootOwner, open_catalog
from robot_test_hub.wpilog import PROFILE


class FixtureSource:
    def __init__(self, profile=PROFILE):
        self.data=(Path(__file__).parent/'fixtures/synthetic/alpha7-main.wpilog').read_bytes()
        self.profile=profile
    def status(self):
        return RobotStatus(False,time.monotonic(),'synthetic-boot',0)
    def list_closed_files(self):
        return [LogFile('synthetic-log','synthetic.wpilog',len(self.data),hashlib.sha256(self.data).hexdigest(),1,
                        format='wpilog',format_profile=self.profile)]
    def read(self,identity,offset,length):
        return self.data[offset:offset+length]


class PipelineTests(unittest.TestCase):
    def test_verified_transfer_imports_indexes_and_restarts_idempotently(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            source=FixtureSource()
            collector=Collector(root,source,idle_delay=0)
            collector.tick()
            self.assertEqual(collector.snapshot()['completed_files'],1)
            collector.close()
            owner=DataRootOwner(root)
            db=open_catalog(root)
            try:
                result=Pipeline(root,db).tick()
                self.assertEqual(result['runs'],2)
                imports=Importer(root,db).list_jobs()
                self.assertEqual(len(imports),1)
                self.assertEqual(imports[0]['state'],'succeeded')
                runs=RunCatalog(db).current()['runs']
                self.assertEqual(runs[0]['start_monotonic_ns'],'1020000123')
                self.assertEqual([p['mode'] for p in runs[0]['phases']],['AUTONOMOUS','TELEOP'])
                self.assertEqual(runs[0]['source_types'],['VERIFIED_TRANSFER'])
                self.assertEqual(runs[0]['wall_clock_quality'],'partial')
                self.assertEqual(db.execute("SELECT format_status FROM transfer_meta").fetchone()[0],'valid')
                again=Pipeline(root,db).tick()
                self.assertEqual(result['revision'],again['revision'])
                self.assertEqual(db.execute('SELECT count(*) FROM run_catalog_revisions').fetchone()[0],1)
                archived=next((root/'archive').iterdir()).read_bytes()
                self.assertEqual(archived,source.data)
            finally:
                db.close();owner.close()

    def test_unsupported_profile_persists_without_blocking_verified_original(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            collector=Collector(root,FixtureSource('unqualified-2026'),idle_delay=0)
            collector.tick();collector.close()
            db=open_catalog(root)
            try:
                pipeline=Pipeline(root,db)
                pipeline.tick();pipeline.tick()
                self.assertEqual(Importer(root,db).list_jobs()[0]['state'],'unsupported')
                self.assertEqual(db.execute('SELECT state FROM files').fetchone()[0],'complete')
                self.assertEqual(db.execute('SELECT format_status FROM transfer_meta').fetchone()[0],'unsupported')
                self.assertEqual(RunCatalog(db).search()['total'],0)
            finally:
                db.close()
