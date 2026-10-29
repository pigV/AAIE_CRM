#!/usr/bin/env python3
"""Run the hermetic, explicit Workflow cutover verification profile."""

from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
MODULES = (
    "tests.test_workflow_cutover",
    "tests.test_workflow_cutover_parity",
    "tests.test_workflow_backfill",
    "tests.test_workflow_backfill_apply",
    "tests.test_workflow_data_resolution",
    "tests.test_workflow_application_cycles_migration",
    "tests.test_workflow_resolver",
    "tests.test_workflow_projection",
    "tests.test_dashboard_workflow_metrics",
    "tests.test_dashboard_kpi_cards",
    "tests.test_student_application_stage_distribution",
    "tests.test_student_management_summary",
    "tests.test_student_detail_summary_cards",
    "tests.test_workflow_offer_events",
    "tests.test_workflow_visa_events",
)


def main() -> int:
    runs = ROOT / "backups" / "test-runs"
    runs.mkdir(parents=True, exist_ok=True)
    unique = runs / datetime.now().strftime("workflow-cutover-%Y%m%d-%H%M%S-%f")
    unique.mkdir()
    env = os.environ.copy()
    env.update(
        {
            "TMPDIR": str(unique),
            "TEMP": str(unique),
            "TMP": str(unique),
            "AAIE_CRM_TEST_RUN_DIR": str(unique),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    command = [sys.executable, "-m", "unittest", *MODULES, "-v"]
    print(f"isolated_test_dir={unique}", flush=True)
    return subprocess.run(command, cwd=ROOT, env=env, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
