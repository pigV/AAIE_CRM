import fcntl
import json
from pathlib import Path
import subprocess
import sys
import unittest
ROOT = Path(__file__).resolve().parents[1]
CONTROL = ROOT / "tools/test_copy_control.py"
class BackupTestIsolation(unittest.TestCase):
    def test_kernel_isolation_and_loopback_configuration(self):
        result = subprocess.run([sys.executable, "-B", str(CONTROL), "probe"], cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        report=json.loads(result.stdout)
        for key in ["source_write_denied","production_read_denied","production_write_denied","external_connection_denied","wrong_bind_denied","child_process_denied","local_write_allowed"]:
            self.assertIs(report[key], True, key)
    def test_lifecycle_lock_prevents_double_start(self):
        with (ROOT/"STAGING_DATA/lifecycle.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
            result=subprocess.run([sys.executable,"-B",str(CONTROL),"start"],cwd=ROOT,capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0)
        self.assertIn("Another local test lifecycle action is active",result.stderr)
if __name__=="__main__":
    unittest.main()
