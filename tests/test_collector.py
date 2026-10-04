import hashlib
from pathlib import Path
import tempfile
import unittest

from robot_test_hub.collector import Collector, LogFile, RobotStatus


class FakeSource:
    def __init__(self):
        self.now = 100.0
        self.enabled = False
        self.connected = True
        self.stale = False
        self.generation = 0
        self.boot = "boot-a"
        self.reads = []
        self.listings = 0
        self.after_read = None
        self.corrupt = False
        self.data = b"abcdefghijkl"
        self.file = LogFile("segment-a", "run-a.wpilog", len(self.data), hashlib.sha256(self.data).hexdigest(), 100)

    def clock(self):
        return self.now

    def status(self):
        return RobotStatus(self.enabled if self.connected else None,
                           self.now - 10 if self.stale else self.now, self.boot, self.generation)

    def list_closed_files(self):
        self.listings += 1
        return [self.file]

    def read(self, identity, offset, length):
        self.reads.append((identity, offset, length))
        self.now += 1
        if self.after_read:
            self.after_read()
        result = self.data[offset:offset + length]
        return b"x" * len(result) if self.corrupt else result


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = FakeSource()
        self.collector = self.make_collector()

    def make_collector(self):
        return Collector(self.root, self.source, idle_delay=2, chunk_size=4, clock=self.source.clock)

    def tearDown(self):
        self.collector.close()
        self.tmp.cleanup()

    def start(self):
        self.collector.tick()
        self.source.now += 2
        self.collector.tick()

    def row(self):
        return self.collector.snapshot()["files"][0]

    def restart(self):
        self.collector.close()
        self.collector = self.make_collector()

    def test_enabled_never_lists_or_reads(self):
        self.source.enabled = True
        for _ in range(5):
            self.source.now += 10
            self.collector.tick()
        self.assertEqual(self.source.listings, 0)
        self.assertEqual(self.source.reads, [])

    def test_idle_delay_then_verified_completion(self):
        self.collector.tick()
        self.assertEqual(self.source.listings, 0)
        self.source.now += 2
        for _ in range(4):
            self.collector.tick()
        self.assertEqual(self.row()["state"], "complete")
        self.assertEqual((self.root / "archive/segment-a.logdata").read_bytes(), self.source.data)
        self.assertEqual(self.collector.snapshot()["state"], "caught_up")

    def test_enable_during_read_does_not_commit_block(self):
        self.start()
        self.source.after_read = lambda: setattr(self.source, "enabled", True)
        self.collector.tick()
        self.assertEqual(self.row()["offset"], 4)
        self.assertEqual(self.collector.snapshot()["state"], "paused")

    def test_transition_generation_catches_enable_disable_between_polls(self):
        self.start()
        self.source.after_read = lambda: setattr(self.source, "generation", self.source.generation + 2)
        self.collector.tick()
        self.assertEqual(self.row()["offset"], 4)

    def test_reconnect_resumes_at_checkpoint(self):
        self.start()
        self.source.connected = False
        self.collector.tick()
        self.source.connected = True
        self.source.now += 20
        self.collector.tick()
        self.assertEqual(len(self.source.reads), 1)
        self.source.now += 2
        self.collector.tick()
        self.assertEqual(self.source.reads[-1][1], 4)

    def test_stale_status_stops_collection(self):
        self.start()
        self.source.stale = True
        self.collector.tick()
        self.assertEqual(len(self.source.reads), 1)
        self.assertIsNone(self.collector.snapshot()["bytes_per_second"])

    def test_reboot_requires_new_idle_window(self):
        self.start()
        self.source.boot = "boot-b"
        self.collector.tick()
        self.assertEqual(len(self.source.reads), 1)
        self.source.now += 2
        self.collector.tick()
        self.assertEqual(len(self.source.reads), 2)

    def test_restart_preserves_offset_and_truncates_uncommitted_tail(self):
        self.start()
        with (self.root / "partial/segment-a.part").open("ab") as f:
            f.write(b"crash-tail")
        self.restart()
        self.assertEqual((self.root / "partial/segment-a.part").read_bytes(), b"abcd")
        self.start()
        self.assertEqual(self.source.reads[-1][1], 4)

    def test_missing_partial_restarts_from_zero(self):
        self.start()
        (self.root / "partial/segment-a.part").unlink()
        self.restart()
        self.start()
        self.assertEqual(self.source.reads[-1][1], 0)

    def test_manual_pause_survives_restart(self):
        self.start()
        self.collector.set_paused(True)
        self.restart()
        self.source.now += 20
        self.collector.tick()
        self.assertEqual(len(self.source.reads), 1)
        self.assertTrue(self.collector.snapshot()["paused_by_operator"])

    def test_renamed_file_keeps_progress(self):
        self.start()
        old = self.source.file
        self.source.file = LogFile(old.id, "new-name.wpilog", old.size, old.sha256, old.created_at)
        self.source.now += 5
        self.collector.tick()
        self.assertEqual(self.row()["name"], "new-name.wpilog")
        self.assertEqual(self.row()["offset"], 8)

    def test_changed_closed_identity_rejected(self):
        self.start()
        old = self.source.file
        self.source.file = LogFile(old.id, old.name, old.size + 1, old.sha256, old.created_at)
        self.source.now += 5
        self.collector.tick()
        self.assertEqual(len(self.source.reads), 1)
        self.assertIn("changed identity", self.collector.snapshot()["error"])

    def test_checksum_failure_quarantines_and_can_retry(self):
        self.source.corrupt = True
        self.start()
        self.collector.tick()
        self.collector.tick()
        self.assertEqual(self.row()["state"], "error")
        self.assertFalse((self.root / "archive/segment-a.logdata").exists())
        self.assertTrue((self.root / "partial/segment-a.invalid").exists())
        self.source.corrupt = False
        self.collector.retry_errors()
        for _ in range(3):
            self.collector.tick()
        self.assertEqual(self.row()["state"], "complete")

    def test_eta_uses_remaining_bytes_and_active_time(self):
        self.start()
        snapshot = self.collector.snapshot()
        self.assertEqual(snapshot["remaining_bytes"], 8)
        self.assertEqual(snapshot["bytes_per_second"], 4)
        self.assertEqual(snapshot["eta_seconds"], 2)
        self.source.enabled = True
        self.source.now += 600
        self.collector.tick()
        self.assertIsNone(self.collector.snapshot()["eta_seconds"])

    def test_crash_after_rename_before_database_commit(self):
        self.start()
        (self.root / "archive/segment-a.logdata").write_bytes(self.source.data)
        self.restart()
        self.assertEqual(self.row()["state"], "complete")

    def test_path_traversal_identity_rejected(self):
        old = self.source.file
        self.source.file = LogFile("../escape", old.name, old.size, old.sha256, old.created_at)
        self.start()
        self.assertEqual(self.source.reads, [])
        self.assertIn("Unsafe", self.collector.snapshot()["error"])

    def test_pause_resume_during_read_discards_block_and_restarts_idle_window(self):
        preference = [False, 0]
        self.collector._paused_provider = lambda: tuple(preference)
        self.start()
        def pause_resume():
            preference[1] += 2
        self.source.after_read = pause_resume
        self.collector.tick()
        self.assertEqual(self.row()["offset"], 4)
        self.assertEqual(self.collector.snapshot()["state"], "waiting")
        self.assertIsNone(self.collector.snapshot()["bytes_per_second"])
        self.source.after_read = None
        self.collector.tick()
        self.assertEqual(len(self.source.reads), 2)
        self.source.now += 2
        self.collector.tick()
        self.assertEqual(self.row()["offset"], 8)

    def test_pause_resume_between_ticks_restarts_idle_window(self):
        preference = [False, 0]
        self.collector._paused_provider = lambda: tuple(preference)
        self.start()
        preference[1] += 2
        self.collector.tick()
        self.assertEqual(len(self.source.reads), 1)
        self.assertEqual(self.collector.snapshot()["state"], "waiting")
        self.source.now += 2
        self.collector.tick()
        self.assertEqual(self.row()["offset"], 8)


if __name__ == "__main__":
    unittest.main()
