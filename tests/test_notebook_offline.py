"""Offline browser logic checks. Node is development-only; browser IDB requires UI QA."""
from pathlib import Path
import re
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "Node unavailable for development-only notebook JavaScript checks")
class OfflineNotebookTests(unittest.TestCase):
    def test_request_outbox_and_service_worker_transitions(self):
        result = subprocess.run([NODE, str(ROOT / "tests/notebook_offline_checks.js")], cwd=ROOT,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("immutable retries", result.stdout)

    def test_page_inline_script_and_helper_parse_without_browser_dependencies(self):
        page = (ROOT / "robot_test_hub/static/notebook.html").read_text(encoding="utf-8")
        self.assertNotIn("innerHTML", page)
        self.assertIn('src="/notebook.js"', page)
        inline = "\n".join(re.findall(r"<script>(.*?)</script>", page, flags=re.DOTALL))
        self.assertTrue(inline)
        checked = subprocess.run([NODE, "--check", "-"], input=inline, capture_output=True,
                                 text=True, cwd=ROOT, timeout=10)
        self.assertEqual(checked.returncode, 0, checked.stderr)


if __name__ == "__main__":
    unittest.main()
