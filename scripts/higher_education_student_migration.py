#!/usr/bin/env python3
"""Generate the higher-education legacy classification dry run; never writes CRM data."""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from higher_education_migration import build_dry_run, open_read_only


def main():
    parser = argparse.ArgumentParser(description="Higher-education migration dry run (read-only)")
    parser.add_argument("--database", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    if ROOT not in output.parents:
        parser.error("--output must stay inside the candidate worktree")
    with open_read_only(Path(args.database).resolve()) as conn:
        report = build_dry_run(conn)
    report["source_database"] = str(Path(args.database).resolve())
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("mode", "definite_count", "manual_review_count", "excluded_legacy_cycle_repairs", "writes_performed")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
