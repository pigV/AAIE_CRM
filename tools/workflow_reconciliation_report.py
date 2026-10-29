#!/usr/bin/env python3
"""Render reconciliation findings from a strictly immutable SQLite source."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.workflow_backfill_preview import open_read_only_database
from workflow_reconciliation import (
    reconcile_workflow,
    reconciliation_to_json,
    reconciliation_to_markdown,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("database_uri")
    parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    conn = open_read_only_database(args.database_uri)
    try:
        issues = reconcile_workflow(conn)
    finally:
        conn.close()
    rendered = (
        reconciliation_to_json(issues)
        if args.format == "json"
        else reconciliation_to_markdown(issues)
    )
    if args.output:
        output = Path(args.output).resolve()
        if not output.is_relative_to(Path.cwd().resolve()):
            raise ValueError("output must remain inside the current CRM workspace")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
