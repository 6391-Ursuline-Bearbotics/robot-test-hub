"""Windows checkout regression: only JSON manifest EOL representation may vary."""
import hashlib
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from robot_test_hub import selftest


class SelftestCheckoutTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='selftest checkout ')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        self.data=selftest._sample()
        self.folder=self.root/'sample_data'
        self.folder.mkdir()
        source=Path(selftest.__file__).parent/'sample_data/manifest.json'
        self.manifest=source.read_bytes().replace(b'\r\n',b'\n')
        (self.folder/'manifest.json').write_bytes(self.manifest)
        (self.folder/'alpha7-main.wpilog').write_bytes(self.data)

    def sample(self):
        with patch.object(selftest.resources,'files',return_value=self.root):
            return selftest._sample()

    @unittest.skipUnless(shutil.which('git'),'Requires Git for actual checkout regression')
    def test_real_git_autocrlf_checkout_preserves_original_and_full_exercise(self):
        def git(*args):
            result=subprocess.run([shutil.which('git'),'-c','core.autocrlf=true',*args],
                cwd=self.root,capture_output=True,text=True,timeout=15)
            self.assertEqual(result.returncode,0,result.stderr)
        git('init','--quiet')
        git('add','--','sample_data')
        for path in self.folder.iterdir():path.unlink()
        git('checkout-index','--all','--force')
        self.assertIn(b'\r\n',(self.folder/'manifest.json').read_bytes())
        self.assertEqual((self.folder/'alpha7-main.wpilog').read_bytes(),self.data)
        with patch.object(selftest.resources,'files',return_value=self.root):
            result=selftest.run_selftest()
        self.assertTrue(result['success'],result)
        self.assertEqual(result['cleanup'],{'state':'passed'})
        self.assertEqual(result['stages'][0]['state'],'passed')

    def test_crlf_manifest_accepts_only_unchanged_canonical_values(self):
        crlf=self.manifest.replace(b'\n',b'\r\n')
        cases=(('crlf',crlf,True),('lone_cr',self.manifest.replace(b'\n',b'\r'),False),
               ('changed_pin',crlf.replace(selftest.SAMPLE_SHA256.encode(),b'0'*64),False),
               ('duplicate_key',crlf.replace(b'{',b'{"schema_version":1,',1),False))
        for label,payload,valid in cases:
            with self.subTest(label=label):
                (self.folder/'manifest.json').write_bytes(payload)
                if valid:self.assertEqual(self.sample(),self.data)
                else:
                    with self.assertRaises(ValueError):self.sample()

    def test_manifest_line_ending_tolerance_never_normalizes_original_bytes(self):
        (self.folder/'manifest.json').write_bytes(self.manifest.replace(b'\n',b'\r\n'))
        payload=bytearray(self.data)
        payload[-1]^=1
        (self.folder/'alpha7-main.wpilog').write_bytes(payload)
        self.assertNotEqual(hashlib.sha256(payload).hexdigest(),selftest.SAMPLE_SHA256)
        with self.assertRaises(ValueError):self.sample()
