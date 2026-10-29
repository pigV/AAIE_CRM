"""Serve an already initialized, verified backup; never initialize/migrate it."""
import argparse
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from staging_environment import ACTIVE as c
if c is None:raise SystemExit("Use the protected test-copy launcher")
from migration_runner import verify_migrations
def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--host",required=True)
    parser.add_argument("--port",required=True,type=int)
    parser.add_argument("--service",choices=("admin","parent"),required=True)
    args=parser.parse_args()
    expected=c.admin_port if args.service=="admin" else c.parent_port
    if args.host!="127.0.0.1" or args.port!=expected:
        raise RuntimeError("Only the configured local test endpoint is allowed")
    result=verify_migrations(c.check(c.db),ROOT/"migrations")
    if result["pending"] or result["foreign_key_violations"]:
        raise RuntimeError("Backup/code compatibility verification failed")
    if not c.check(c.root/"audit/test_database_prepared.json").is_file():
        raise RuntimeError("Test database has not been sanitized")
    import app
    if app.DB_PATH!=c.db or app.STAGING is not c:
        raise RuntimeError("Staging identity mismatch")
    build_path = ROOT / "TEST_BUILD.json"
    if build_path.is_file():
        app.VERSION = json.loads(build_path.read_text())["build_id"]
    # Native Staging mode disables all class-two/background startup tasks.
    # This is an existing initialized database, checked above, not a fresh fixture.
    app.run_server(args.host,args.port,args.service,startup_preflight_done=True)
if __name__=="__main__":main()
