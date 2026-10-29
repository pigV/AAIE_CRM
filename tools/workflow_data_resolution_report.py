#!/usr/bin/env python3
"""Generate deterministic Workflow data-resolution reports from a read-only DB."""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import sqlite3
import sys
from urllib.parse import parse_qs, urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from workflow_data_resolution import build_data_resolution_bundle, render_data_resolution_reports


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


def _workspace_output(path: str) -> Path:
    output = Path(path).resolve()
    workspace = Path.cwd().resolve()
    if not output.is_relative_to(workspace):
        raise ValueError("output directory must remain inside the current CRM workspace")
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("database_uri")
    parser.add_argument("preview_json")
    parser.add_argument("output_directory")
    parser.add_argument("--today", required=True)
    args = parser.parse_args(argv)

    as_of = date.fromisoformat(args.today)
    preview = json.loads(Path(args.preview_json).read_text(encoding="utf-8"))
    output = _workspace_output(args.output_directory)
    conn = open_read_only_database(args.database_uri)
    try:
        bundle = build_data_resolution_bundle(conn, preview, today=as_of)
        reports = render_data_resolution_reports(bundle)
    finally:
        conn.close()

    output.mkdir(parents=True, exist_ok=True)
    for name, contents in sorted(reports.items()):
        (output / name).write_text(contents, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
