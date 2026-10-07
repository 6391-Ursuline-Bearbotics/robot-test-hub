"""Compiler diagnostics stay captured when native preparation is used by redacted CLIs."""
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from tools.status_bridge import run


class StatusToolTests(unittest.TestCase):
    def test_compiler_success_and_failure_both_capture_diagnostics(self):
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                (root / 'src').mkdir()
                (root / 'src/StatusBridge.java').write_text('synthetic', encoding='utf-8')
                jar, source, native = root / 'dependency.jar', root / 'source.jar', root / 'native.zip'
                jar.write_bytes(b'synthetic dependency')
                source.write_bytes(b'synthetic source')
                with zipfile.ZipFile(native, 'w') as package:
                    package.writestr('synthetic.dll', b'synthetic native')
                hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (jar, source, native)}
                (root / 'dependencies.lock.json').write_text(json.dumps(dict(sha256=hashes)), encoding='utf-8')
                calls = []
                def invoke(command, **kwargs):
                    calls.append((command, kwargs))
                    self.assertTrue(kwargs['capture_output'])
                    self.assertTrue(kwargs['text'])
                    self.assertTrue(kwargs['check'])
                    if len(calls) == 1:
                        return SimpleNamespace(stderr='openjdk version "25"')
                    self.assertEqual(kwargs['timeout'], 30)
                    if fail:
                        raise subprocess.CalledProcessError(1, command, output='private-compiler-output',
                                                            stderr='private-compiler-error')
                    return SimpleNamespace(stderr='', stdout='')
                output = io.StringIO()
                with patch.object(run, 'HERE', root), \
                     patch.object(run, 'os', SimpleNamespace(name='nt', pathsep=os.pathsep, environ=os.environ)), \
                     patch.object(run, 'dependencies', return_value=([jar], [source], [native])), \
                     patch.object(run.subprocess, 'run', side_effect=invoke), \
                     redirect_stdout(output), redirect_stderr(output):
                    if fail:
                        with self.assertRaises(subprocess.CalledProcessError):
                            run.prepare(root)
                    else:
                        command, _ = run.prepare(root)
                        self.assertTrue(command)
                self.assertEqual(len(calls), 2)
                self.assertEqual(output.getvalue(), '')


if __name__ == '__main__':
    unittest.main()
