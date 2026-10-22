"""Detached, coherent benchmark snapshots while actual workers update counters."""
import importlib.util
from pathlib import Path
import threading
import unittest

spec=importlib.util.spec_from_file_location('bench_stream_service_under_test',Path(__file__).resolve().parents[1]/'tools'/'bench_stream_service.py')
benchmark=importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)

class BenchmarkSnapshotTests(unittest.TestCase):
    def test_snapshot_detaches_histogram_from_later_shutdown_waits(self):
        stats=benchmark.Stats();stats.add(.5)
        saved=stats.result();stats.add(.25)
        self.assertEqual(saved['count'],1)
        self.assertEqual(sum(saved['log2_microsecond_buckets']),1)
        saved['log2_microsecond_buckets'][0]=100
        self.assertEqual(sum(stats.result()['log2_microsecond_buckets']),2)

    def test_concurrent_updates_have_consistent_scalar_and_bucket_snapshots(self):
        stats=benchmark.Stats();start=threading.Event()
        def update():
            start.wait()
            for _ in range(5000):stats.add(.5)
        workers=[threading.Thread(target=update) for _ in range(4)]
        for worker in workers:worker.start()
        start.set()
        try:
            while any(worker.is_alive() for worker in workers):
                saved=stats.result()
                self.assertEqual(saved['count'],sum(saved['log2_microsecond_buckets']))
                self.assertEqual(saved['seconds'],saved['count']*.5)
        finally:
            for worker in workers:worker.join()
        self.assertEqual(stats.result()['count'],20000)

if __name__=='__main__':unittest.main()
