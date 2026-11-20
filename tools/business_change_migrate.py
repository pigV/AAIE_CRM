"""Explicitly migrate only this worktree's isolated business-change test DB.

No production override exists here. Formal release uses its separate review and
the existing migration runner; this operator cannot restart any service.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import stat
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from business_changes.schema import MIGRATION,require_schema
from migration_runner import apply_migrations

CONFIRMATION='APPLY-ISOLATED-BUSINESS-CHANGES'


def validate_database(path):
    candidate=Path(path).absolute()
    allowed=ROOT/'STAGING_DATA'
    try:
        if not candidate.is_relative_to(allowed):raise ValueError('outside_task_staging')
        for part in (candidate,*candidate.parents):
            if part.is_symlink():raise ValueError('symlink_not_allowed')
            if part==ROOT:break
        resolved=candidate.resolve(strict=True)
        if not resolved.is_relative_to(allowed.resolve(strict=True)):raise ValueError('outside_task_staging')
        meta=resolved.stat()
        if not stat.S_ISREG(meta.st_mode) or meta.st_nlink!=1:raise ValueError('not_single_regular_file')
        return resolved
    except (OSError,RuntimeError) as exc:
        raise ValueError('isolated_database_missing_or_invalid') from exc


def apply_isolated(path,*,confirmation):
    if confirmation!=CONFIRMATION:raise ValueError('exact_isolated_confirmation_required')
    database=validate_database(path)
    package=ROOT/'STAGING_DATA/migration-packages/business-changes-050'
    for relative in (Path(MIGRATION),Path('checks')/(Path(MIGRATION).stem+'_forward_check.sql')):
        source=ROOT/'migrations'/relative;target=package/relative
        target.parent.mkdir(parents=True,exist_ok=True)
        if target.exists():
            if target.read_bytes()!=source.read_bytes():raise ValueError('isolated_migration_package_changed')
        else:shutil.copy2(source,target)
    result=apply_migrations(database,package,applied_by='authorized-isolated-test',source_commit='ffbcab1+system-business-sync-development')
    with sqlite3.connect(database.as_uri()+'?mode=ro',uri=True) as conn:
        require_schema(conn)
        if conn.execute('PRAGMA foreign_key_check').fetchall():raise ValueError('foreign_key_check_failed')
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',type=Path,required=True)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--confirmation')
    args=parser.parse_args()
    path=validate_database(args.database)
    if args.apply:result=apply_isolated(path,confirmation=args.confirmation)
    else:result={'mode':'read-only-plan','database':str(path),'migration':MIGRATION,
                 'database_sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
