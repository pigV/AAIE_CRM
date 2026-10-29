#!/usr/bin/env python3
"""Render a workflow backfill preview from a strictly read-only SQLite URI."""

from __future__ import annotations

import argparse
from pathlib import Path
import sqlite3
import sys
from urllib.parse import parse_qs, urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from workflow_backfill import build_backfill_preview, preview_to_json, preview_to_markdown


def open_read_only_database(database_uri: str) -> sqlite3.Connection:
    parsed = urlparse(database_uri)
    query = parse_qs(parsed.query)
    if not database_uri.startswith("file:") or query.get("mode") != ["ro"]:
        raise ValueError("database URI must use file:...?...&mode=ro")
    if query.get("immutable") != ["1"]:
        raise ValueError("database URI must set immutable=1")
    conn = sqlite3.connect(database_uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("database_uri")
    parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    conn = open_read_only_database(args.database_uri)
    try:
        preview = build_backfill_preview(conn)
    finally:
        conn.close()
    rendered = preview_to_json(preview) if args.format == "json" else preview_to_markdown(preview)
    if args.output:
        output = Path(args.output).resolve()
        workspace = Path.cwd().resolve()
        if not output.is_relative_to(workspace):
            raise ValueError("output must remain inside the current CRM workspace")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
