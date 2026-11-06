import fcntl
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
ROOT = Path(__file__).resolve().parents[1]
CONTROL = ROOT / "tools/test_copy_control.py"
class BackupTestIsolation(unittest.TestCase):
    def setUp(self):
        from tests.isolation_test_support import prepare_isolation_source
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=prepare_isolation_source(Path(self.temp.name)/"source")
        self.control=self.root/"tools/test_copy_control.py"
    def test_kernel_isolation_and_loopback_configuration(self):
        result = subprocess.run([sys.executable, "-B", str(self.control), "probe"], cwd=self.root, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        report=json.loads(result.stdout)
        for key in ["source_write_denied","production_read_denied","production_write_denied","external_connection_denied","wrong_bind_denied","child_process_denied","local_write_allowed"]:
            self.assertIs(report[key], True, key)
    def test_start_still_requires_verified_runtime_before_starting_services(self):
        result=subprocess.run([sys.executable,"-B",str(self.control),"start"],cwd=self.root,capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0)
        self.assertIn("Verified isolated runtime copy is missing",result.stderr)
        self.assertEqual(list((self.root/"STAGING_DATA").glob("*.pid.json")),[])
    def test_lifecycle_lock_prevents_double_start(self):
        with (self.root/"STAGING_DATA/lifecycle.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
            result=subprocess.run([sys.executable,"-B",str(self.control),"start"],cwd=self.root,capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0)
        self.assertIn("Another local test lifecycle action is active",result.stderr)
if __name__=="__main__":
    unittest.main()
