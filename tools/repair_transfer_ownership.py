#!/usr/bin/env python3
"""Preview/apply a reviewed scope on the isolated staging database ONLY.

Production execution is deliberately unavailable. A future production repair
requires separate review and authorization, not an environment override.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from staging_environment import ACTIVE
from student_lifecycle.transfer_ownership_repair import apply, preview


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('preview','apply'))
    parser.add_argument('--scope', required=True)
    parser.add_argument('--approved-scope-sha256', required=True)
    parser.add_argument('--artifacts', required=True)
    parser.add_argument('--actor', required=True, type=int)
    parser.add_argument('--preview', required=True)
    parser.add_argument('--result', required=True)
    args = parser.parse_args()
    if ACTIVE is None or ACTIVE.root != ROOT / 'STAGING_DATA':
        raise RuntimeError('This tool requires this worktree isolated staging environment')
    # Independently provisioned approval anchor; the command line cannot
    # replace it by supplying a different scope and its matching digest.
    approval = json.loads(ACTIVE.check(ACTIVE.root/'config/transfer_ownership_approval.json').read_text())
    if args.approved_scope_sha256 != approval['scope_sha256']:
        raise RuntimeError('Scope is outside this environment approved repair batch')
    db_path = ACTIVE.check(ACTIVE.db)
    if db_path.resolve() != (ROOT/'STAGING_DATA/database/staging.db').resolve():
        raise RuntimeError('Unexpected staging database')
    scope = json.loads(ACTIVE.check(Path(args.scope).resolve()).read_text())
    identities = [{k:t[k] for k in ('student_id','cycle_id','contract_id','authority_id')}
                  for t in scope['targets']]
    if scope['batch_uid'] != approval['batch_uid'] or identities != approval['targets']:
        raise RuntimeError('Repair target identities differ from independent approval')
    artifacts = json.loads(ACTIVE.check(Path(args.artifacts).resolve()).read_text())
    paths = {str(cid):str(ACTIVE.check(Path(v['local_path']).resolve())) for cid,v in artifacts.items()}
    frozen_path = ACTIVE.check(Path(args.preview).resolve())
    result_path = ACTIVE.check(Path(args.result).resolve())
    if result_path.exists() or (args.action == 'preview' and frozen_path.exists()):
        raise RuntimeError('Refusing to overwrite existing review evidence')
    stamp = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('BEGIN IMMEDIATE' if args.action == 'apply' else 'BEGIN')
        if args.action == 'preview':
            result = preview(conn,scope,args.approved_scope_sha256,args.actor,stamp,paths)
            conn.rollback()
            frozen_path.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
        else:
            result = apply(conn,scope,args.approved_scope_sha256,json.loads(frozen_path.read_text()),args.actor,stamp,paths)
            conn.commit()
            result = dict(result.result, replayed=result.replayed,event_sequences=list(result.event_sequences))
    result_path.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'action':args.action,'status':'PASS','output':str(result_path)},ensure_ascii=False))


if __name__ == '__main__':
    main()
