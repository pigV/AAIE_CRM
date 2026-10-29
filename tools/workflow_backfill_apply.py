#!/usr/bin/env python3
"""Apply deterministic Workflow SAFE proposals to an isolated DB copy."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from workflow_backfill import apply_deterministic_backfill


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--formal-database")
    args = parser.parse_args()
    result = apply_deterministic_backfill(
        args.database,
        actor=args.actor,
        formal_database_path=args.formal_database,
    )
    sys.stdout.write(result.to_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
