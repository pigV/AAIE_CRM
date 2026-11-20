"""Launch app tests in this task's existing staging OS sandbox."""
from pathlib import Path
import subprocess
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


if __name__=='__main__':
    if sys.argv[1:2]!=['--confined']:
        from tools import staging_control as staging
        c=staging.prepare()
        if (c.admin_port,c.parent_port)!=(19060,19061):raise RuntimeError('unexpected staging ports')
        raise SystemExit(subprocess.call(staging.command(c,'-B',str(Path(__file__).resolve()),'--confined',*sys.argv[1:]),cwd=ROOT,env=staging.environment(c)))
    from staging_environment import ACTIVE
    if ACTIVE is None or ACTIVE.root!=ROOT/'STAGING_DATA':raise RuntimeError('task staging required')
    import tempfile
    tempfile.tempdir=str(ACTIVE.root/'tmp')
    test_arguments=sys.argv[2:]
    if test_arguments[:1]==['--data-cwd']:
        # Some retained tests use explicit relative dir="tmp". Keep those
        # writes inside the existing staging boundary, without weakening it.
        import os
        os.chdir(ACTIVE.root)
        test_arguments=test_arguments[1:]
    import unittest
    unittest.main(module=None,argv=['unittest',*test_arguments],verbosity=1)
