"""Owner-only test lifecycle with native Staging safeguards."""
import argparse
import fcntl
import json
from pathlib import Path
import subprocess
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tools import staging_control as control
from tools import test_extraction_control as extraction

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("action",choices=("prepare","probe","start","stop","restart","status","start-extraction"))
    action=parser.parse_args().action
    with (ROOT/"STAGING_DATA/lifecycle.lock").open("a+") as lock:
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another local test lifecycle action is active") from None
        c=control.load()
        if action in ("prepare","probe","start","restart","start-extraction"):
            control.prepare()
            extraction.prepare(c)
        if action=="probe":
            results={}
            for name,module in (("admin_parent",control),("extraction",extraction)):
                result=subprocess.run(module.command(c,str(ROOT/"tools/test_copy_probe.py")),
                    env=module.environment(c),cwd=ROOT,text=True,capture_output=True)
                if result.returncode: raise RuntimeError(result.stderr)
                results[name]=json.loads(result.stdout)
                if not all(results[name].values()): raise RuntimeError(name+" isolation probe failed")
            # Preserve the original probe result format for the original tests.
            print(json.dumps(results["admin_parent"]))
            (c.root/"audit/three_service_isolation.json").write_text(json.dumps(results,indent=2)+"\n")
        elif action=="start-extraction":
            print(json.dumps(extraction.start(c),indent=2))
        elif action in ("start","restart"):
            if action=="restart":
                control.stop(c)
                extraction.stop(c)
            extraction.start(c)
            if any(control.state_path(c,r).exists() for r in ("admin","parent")):
                for role in ("admin","parent"):
                    record=json.loads(control.state_path(c,role).read_text())
                    control.verify_process(c,role,record)
                    control.health(c,role)
            else:
                try: control.start(c)
                except Exception:
                    control.stop(c)
                    raise
            print("Local test running: Admin 18880, Parent 18881, Content Extraction 18892")
        elif action=="stop":
            control.stop(c)
            extraction.stop(c)
            print("Local test stopped")
        elif action=="status":
            result={r:control.health(c,r) for r in ("admin","parent")}
            result["content_extraction"]=extraction.health(c)
            print(json.dumps(result,indent=2))
        else: print("Local test prepared")
if __name__=="__main__":main()
