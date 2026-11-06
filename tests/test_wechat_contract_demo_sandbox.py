"""Native macOS sandbox checks; run with the bundled Python outside nested sandbox."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from tools import staging_control
from tools.wechat_contract_demo_control import profile


class NativeDemoIsolationTests(unittest.TestCase):
    def test_only_this_run_is_writable_and_other_business_data_is_unreadable(self):
        # This policy probe needs no deployed staging config or live data.
        from staging_environment import Staging
        staging_control.DATA.mkdir(parents=True, exist_ok=True)
        fixture = tempfile.TemporaryDirectory(prefix='owned-policy-', dir=staging_control.DATA)
        self.addCleanup(fixture.cleanup)
        c = Staging(Path(fixture.name), 18880, 18881, 'aaie_staging_')
        base = c.root / 'wechat-contract-demo'
        base.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='sandbox-test-', dir=base) as folder:
            root = Path(folder)
            sb = root / 'test.sb'
            sb.write_text(profile(c, root))
            checks = {
                'live_database': c.db,
                'live_backup': c.root / 'backups/AAIE_CRM_Startup_Backup_20261030_210002.zip',
                'old_reproduction': c.root / 'transaction-conflict-20261030/reproduction.db',
                'sibling_demo': base / 'another-run/demo.db',
                'nas': Path('/Volumes/Server/Students/CRM_TEST'),
                'legacy_production': Path('/Volumes/Zhang/AAIE CodeX/CRM/database/aaie_crm.db'),
                'production': Path('/Volumes/Data/AAIE_CRM/runtime/production/database/aaie_crm.db'),
            }
            code = '''import ctypes,json,os
from pathlib import Path
lib=ctypes.CDLL('/usr/lib/libSystem.B.dylib')
lib.sandbox_check.argtypes=[ctypes.c_int,ctypes.c_char_p,ctypes.c_int]
lib.sandbox_check.restype=ctypes.c_int
def denied(operation,path): return lib.sandbox_check(os.getpid(),operation.encode(),1,str(path).encode())==1
paths=CHECKS
results={k:denied('file-read-data',p) and denied('file-write-data',p) for k,p in paths.items()}
p=Path(RUNROOT)/'write-probe.txt';p.write_text('synthetic');results['own_run_writable']=p.read_text()=='synthetic'
print(json.dumps(results))
'''.replace('CHECKS', repr({k: str(v) for k, v in checks.items()})).replace('RUNROOT', repr(str(root)))
            result = subprocess.run(['/usr/bin/sandbox-exec', '-f', str(sb), staging_control.PYTHON, '-B', '-c', code],
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            results = json.loads(result.stdout)
            self.assertTrue(all(results.values()), results)


if __name__ == '__main__':
    unittest.main()
