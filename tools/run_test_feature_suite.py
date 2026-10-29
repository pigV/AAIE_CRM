"""Run feature tests under a separate offline sandbox, never against live test DBs."""
import json, os, subprocess, sys
from pathlib import Path
from tools import staging_control as s
from tools import test_extraction_control as e
c=s.load()
audit=c.root/"audit/feature-integration-20261030"
node=Path("/Users/faqiang/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node")
lines=[line for line in e.sandbox_profile(c).splitlines()
       if not line.startswith("(allow network-") and "extraction-runtime/bin/ffmpeg" not in line]
lines.append('(allow process-exec (literal '+json.dumps(str(node))+'))')
for name in ["database/staging.db","database/staging.db-wal","database/staging.db-shm",
             "database/content_extraction_service.db","database/content_extraction_service.db-wal","database/content_extraction_service.db-shm"]:
    lines.append('(deny file-read-data file-write* (literal '+json.dumps(str(c.root/name))+'))')
profile=audit/"unit-tests.sb";profile.write_text("\n".join(lines)+"\n")
groups=sys.argv[2:]
phase=sys.argv[1]
records=[]
for index,group in enumerate(groups):
    # Each group gets an independent interpreter so mocked app imports cannot leak across suites.
    code="import staging_environment; import unittest; unittest.main(module=None,argv=['unittest']+"+repr(group.split(","))+",verbosity=2)"
    command=["/usr/bin/sandbox-exec","-f",str(profile),s.PYTHON,"-B","-c",code]
    env=s.environment(c)
    result=subprocess.run(command,cwd=s.ROOT,env=env,capture_output=True,text=True,timeout=180)
    (audit/f"{phase}-{index}.log").write_text(result.stdout+"\n"+result.stderr)
    record={"group":group,"exit_code":result.returncode,"log":f"{phase}-{index}.log"}
    records.append(record)
    print(json.dumps(record),flush=True)
    print("\n".join(result.stderr.splitlines()[-8:]),flush=True)
(audit/f"{phase}-results.json").write_text(json.dumps(records,indent=2)+"\n")
sys.exit(0 if all(r["exit_code"]==0 for r in records) else 1)
