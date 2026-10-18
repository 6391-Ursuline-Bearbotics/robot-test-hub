import contextlib
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from robot_test_hub import import_folder
from robot_test_hub.importer import Importer
from robot_test_hub.runs import RunCatalog
from robot_test_hub.storage import DataRootOwner, open_catalog

FIXTURES = Path(__file__).parent / "fixtures/synthetic"


class ImportFolderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.usb, self.data = base / "usb", base / "data"
        (self.usb / "nested").mkdir(parents=True)
        shutil.copy(FIXTURES / "alpha7-main.wpilog", self.usb / "a.wpilog")
        shutil.copy(FIXTURES / "alpha7-boot-b.wpilog", self.usb / "nested/b.WPILOG")
        (self.usb / "empty.wpilog").write_bytes(b"")
        (self.usb / "notes.txt").write_text("not a log")

    def tearDown(self):
        self.temp.cleanup()

    def run_cli(self, *extra):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = import_folder.main([str(self.usb), "--data-dir", str(self.data), "--json", "--synthetic", *extra])
        return code, json.loads(output.getvalue())

    def test_finds_nested_logs_case_insensitively_and_skips_empty_or_other_files(self):
        names = [path.name for path in import_folder.find_logs(self.usb)]
        self.assertEqual(names, ["a.wpilog", "b.WPILOG"])

    def test_imports_indexes_and_reports_then_is_idempotent(self):
        code, first = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual(first["counts"], {"imported": 2})
        self.assertGreaterEqual(first["processing"]["runs"], 1)
        code, second = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual([r["job_id"] for r in first["files"]], [r["job_id"] for r in second["files"]])
        owner = DataRootOwner(self.data)
        db = open_catalog(owner.root)
        try:
            self.assertEqual(len(Importer(owner.root, db).list_jobs()), 2)
            runs = RunCatalog(db).current()["runs"]
            self.assertTrue(runs)
            self.assertTrue(all(run["source_types"] == ["SYNTHETIC"] for run in runs))
            self.assertGreaterEqual(db.execute("SELECT count(*) FROM analysis_reports").fetchone()[0], 1)
            archived = sorted(p.read_bytes() for p in (owner.root / "raw").rglob("*.wpilog"))
            self.assertEqual(archived, sorted([(FIXTURES / "alpha7-main.wpilog").read_bytes(),
                                               (FIXTURES / "alpha7-boot-b.wpilog").read_bytes()]))
        finally:
            db.close()
            owner.close()

    def test_bad_file_does_not_stop_the_rest_and_sets_failure_exit(self):
        (self.usb / "broken.wpilog").write_bytes(b"not a wpilog at all")
        code, result = self.run_cli("--no-process")
        self.assertEqual(code, 1)
        states = {Path(r["file"]).name: r["state"] for r in result["files"]}
        self.assertEqual(states["a.wpilog"], "succeeded")
        self.assertEqual(states["b.WPILOG"], "succeeded")
        self.assertNotIn(states["broken.wpilog"], ("succeeded", "succeeded_with_unsupported"))
        self.assertIsNone(result["processing"])

    def test_unknown_profile_is_recorded_as_unsupported_not_guessed(self):
        code, result = self.run_cli("--no-process", "--profile", "wpilib-1999")
        self.assertEqual(code, 1)
        self.assertEqual({r["state"] for r in result["files"]}, {"unsupported"})

    def test_missing_folder_is_an_error(self):
        with self.assertRaises(FileNotFoundError):
            import_folder.find_logs(self.usb / "nope")


if __name__ == "__main__":
    unittest.main()
