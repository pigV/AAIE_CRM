"""Run material UX checks with the project's existing OS-confined staging tools."""
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def launch(arguments):
    from tools import staging_control as staging
    config_path = ROOT/'STAGING_DATA/staging.json'
    if not config_path.exists():
        config_path.parent.mkdir(exist_ok=True)
        config_path.write_text(json.dumps({'mode':'staging','root':str(config_path.parent),
                                           'admin_port':18968,'parent_port':18969,'namespace':'aaie_staging_'})+'\n')
    config = staging.prepare()
    if (config.admin_port,config.parent_port)!=(18968,18969):
        raise RuntimeError('Unexpected task staging ports')
    return subprocess.call(staging.command(config, '-B', str(Path(__file__).resolve()), '--confined', *arguments),
                           cwd=ROOT, env=staging.environment(config))


if __name__ == '__main__':
    if sys.argv[1:2] != ['--confined']:
        raise SystemExit(launch(sys.argv[1:]))
    import tempfile
    import staging_environment
    assert staging_environment.ACTIVE is not None
    tempfile.tempdir = str(staging_environment.ACTIVE.root/'tmp')
    import unittest
    unittest.main(module=None, argv=['unittest']+sys.argv[2:], verbosity=1)
