"""Run task acceptance with kernel enforced staging isolation."""
from pathlib import Path
import os
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if __name__ == '__main__':
    if sys.argv[1:2] != ['--confined']:
        from tools import staging_control as control
        config = control.prepare()
        if (config.admin_port, config.parent_port) != (19146, 19147):
            raise RuntimeError('Unexpected task ports')
        env = control.environment(config)
        env['REVENUE_POSIX_TEST_ROOT'] = str(ROOT/'STAGING_DATA/revenue-secure-fixtures')
        raise SystemExit(subprocess.call(control.command(config, '-B', str(Path(__file__).resolve()),
            '--confined', *sys.argv[1:]), cwd=ROOT, env=env))
    from staging_environment import ACTIVE
    if ACTIVE is None or ACTIVE.root != ROOT/'STAGING_DATA':
        raise RuntimeError('Task staging required')
    from tests import revenue_secure_test_support as support
    support.APPROVED_ROOT = ACTIVE.root/'revenue_documents/test-fixtures'
    os.environ['REVENUE_POSIX_TEST_ROOT'] = str(support.APPROVED_ROOT)
    import tempfile
    tempfile.tempdir = str(ACTIVE.root/'tmp')
    if sys.argv[2:3] == ['--serve']:
        import runpy
        runpy.run_path(str(ROOT/'tests/tong_finance_browser.py'), run_name='__main__')
    else:
        import unittest
        if any(name.startswith('tests.test_tong_finance_http') for name in sys.argv[2:]):
            os.environ['AAIE_TONG_FINANCE_HTTP_ACCEPTANCE'] = '1'
        unittest.main(module=None, argv=['unittest', *sys.argv[2:]], verbosity=2)
