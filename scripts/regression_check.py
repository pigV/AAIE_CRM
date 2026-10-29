#!/usr/bin/env python3
"""Run CRM regression check suites and write a project-local report."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = ROOT / "reports" / "regression"


SUITES = {
    "core": [
        "tests.test_regression_protection",
        "tests.test_core.CoreTests.test_student_info_docx_filename_uses_trimmed_student_name",
        "tests.test_core.CoreTests.test_document_preview_passes_docx_bytes_to_preview_generator",
        "tests.test_core.CoreTests.test_contract_signing_link_schema_and_routes_are_wired",
        "tests.test_core.CoreTests.test_school_plan_secure_share_token_is_hash_only_and_expires",
    ],
    "student-info": [
        "tests.test_core.CoreTests.test_student_info_docx_filename_uses_trimmed_student_name",
        "tests.test_core.CoreTests.test_student_info_docx_version_path_does_not_overwrite_existing_file",
        "tests.test_core.CoreTests.test_legacy_studentinfo_docx_is_renamed_to_student_info_filename",
        "tests.test_core.CoreTests.test_parent_form_date_fields_use_fixed_day_month_year_display",
        "tests.test_core.CoreTests.test_invalid_parent_form_dates_are_rejected",
        "tests.test_core.CoreTests.test_public_parent_form_has_dedicated_submit_feedback_hooks",
        "tests.test_core.CoreTests.test_parent_form_client_script_validates_and_submits_with_feedback",
        "tests.test_core.CoreTests.test_parent_form_submit_supports_json_and_friendly_errors",
    ],
    "contract": [
        "tests.test_core.CoreTests.test_voided_contract_signing_link_is_rejected",
        "tests.test_core.CoreTests.test_signed_contract_void_revokes_links_and_marks_documents_voided",
        "tests.test_core.CoreTests.test_apply_aaie_signature_and_stamp_creates_final_pdf",
        "tests.test_core.CoreTests.test_apply_manual_aaie_signature_and_stamp_creates_final_pdf",
        "tests.test_core.CoreTests.test_contract_management_mvp_ui_and_language_rules",
        "tests.test_core.CoreTests.test_contract_signing_link_schema_and_routes_are_wired",
        "tests.test_core.CoreTests.test_contract_signing_link_regeneration_supersedes_old_link",
        "tests.test_core.CoreTests.test_contract_signature_archives_signed_contract_and_confirmation_page",
        "tests.test_core.CoreTests.test_contract_generation_only_includes_selected_scopes",
        "tests.test_core.CoreTests.test_contract_signing_docx_to_pdf_preserves_tables",
        "tests.test_core.CoreTests.test_contract_pdf_validation_rejects_missing_party_and_signature_tables",
    ],
    "school-plan": [
        "tests.test_core.CoreTests.test_school_plan_secure_share_token_is_hash_only_and_expires",
        "tests.test_core.CoreTests.test_school_plan_secure_share_regenerate_revokes_old_and_expired_blocks",
        "tests.test_core.CoreTests.test_school_plan_multiple_authorised_viewers_have_independent_links_and_watermarks",
        "tests.test_core.CoreTests.test_school_plan_read_only_viewer_cannot_confirm",
        "tests.test_core.CoreTests.test_school_plan_report_content_cleanup_and_mobile_living_cost_wrapper",
        "tests.test_core.CoreTests.test_school_plan_new_version_supersedes_previous_report_and_all_links",
        "tests.test_core.CoreTests.test_school_plan_secure_share_viewer_blocks_unbound_and_other_wechat",
        "tests.test_core.CoreTests.test_school_plan_confirmation_requires_complete_unique_rankings",
        "tests.test_core.CoreTests.test_school_plan_confirmation_pdf_is_created_with_signature",
        "tests.test_core.CoreTests.test_school_plan_confirmation_archive_is_idempotent_for_same_confirmation",
        "tests.test_core.CoreTests.test_school_plan_generate_pdf_wires_post_snapshot_and_download",
        "tests.test_core.CoreTests.test_school_plan_wechat_oauth_uses_parent_domain_and_root_verify_file",
        "tests.test_core.CoreTests.test_school_plan_report_detail_and_preview_are_wired",
        "tests.test_core.CoreTests.test_school_plan_pdf_is_saved_to_student_applications_folder",
        "tests.test_core.CoreTests.test_school_plan_existing_report_sync_to_application_is_blocked_before_parent_confirmation",
    ],
    "preview": [
        "tests.test_core.CoreTests.test_docx_preview_text_extraction_includes_table_cells",
        "tests.test_core.CoreTests.test_docx_preview_page_count_splits_long_student_info",
        "tests.test_core.CoreTests.test_document_preview_passes_docx_bytes_to_preview_generator",
        "tests.test_core.CoreTests.test_document_preview_keeps_preview_path_on_non_first_thumbnail_page",
        "tests.test_core.CoreTests.test_document_preview_reports_pdf_render_failure",
        "tests.test_core.CoreTests.test_document_preview_regenerate_builds_every_pdf_page",
        "tests.test_core.CoreTests.test_document_preview_refreshes_pdf_even_when_cached_preview_exists",
        "tests.test_core.CoreTests.test_preview_thumbnails_are_not_recreated_after_page_count_unchanged",
        "tests.test_core.CoreTests.test_unified_preview_supports_page_jump_and_thumbnail_navigation",
        "tests.test_core.CoreTests.test_student_document_rows_use_unified_right_preview_panel",
        "tests.test_core.CoreTests.test_document_preview_regeneration_routes_are_wired",
        "tests.test_core.CoreTests.test_upload_review_uses_workbench_preview_layout",
        "tests.test_core.CoreTests.test_make_preview_creates_png",
        "tests.test_core.CoreTests.test_make_preview_renders_pdf_first_page",
    ],
    "interaction": [
        "tests.test_regression_protection",
        "tests.test_core.CoreTests.test_contract_management_mvp_ui_and_language_rules",
        "tests.test_core.CoreTests.test_unified_preview_supports_page_jump_and_thumbnail_navigation",
        "tests.test_core.CoreTests.test_school_plan_report_content_cleanup_and_mobile_living_cost_wrapper",
        "tests.test_core.CoreTests.test_parent_form_client_script_validates_and_submits_with_feedback",
    ],
}


MANUAL_NOTES = {
    "student-info": [
        "Open a real parent form link in the browser and confirm submit feedback is friendly.",
        "Open the generated StudentInfo Word file from Application preview/download.",
    ],
    "contract": [
        "Real WeChat OAuth requires a WeChat browser or configured test identity.",
        "Visually inspect signed contract PDF: parent signature must be in the Party B signing area.",
    ],
    "school-plan": [
        "Real WeChat OAuth and mobile rendering should be checked on a phone.",
        "Confirm watermark is readable but does not block school comparison content.",
    ],
    "preview": [
        "Open at least one PDF and one DOCX preview in the browser after large preview changes.",
    ],
    "interaction": [
        "Manually click bottom-page buttons and confirm the browser does not jump to the top.",
    ],
}


def selected_suites(args: argparse.Namespace) -> list[str]:
    names = [name for name in SUITES if getattr(args, name.replace("-", "_"))]
    if args.full:
        return list(SUITES)
    if not names:
        names = ["core"]
    return names


def dedupe_tests(suite_names: list[str]) -> list[str]:
    tests: list[str] = []
    seen: set[str] = set()
    for suite in suite_names:
        for test_name in SUITES[suite]:
            if test_name not in seen:
                tests.append(test_name)
                seen.add(test_name)
    return tests


def report_paths(now: datetime) -> tuple[Path, Path]:
    stamp = now.strftime("%Y%m%d_%H%M%S")
    return (
        REPORT_DIR / f"RegressionReport_{stamp}.md",
        REPORT_DIR / f"RegressionReport_{stamp}.json",
    )


def run_unittest(test_names: list[str]) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.setdefault("PYTHONPYCACHEPREFIX", str(ROOT / ".pycache_regression"))
    command = [sys.executable, "-m", "unittest", *test_names, "-v"]
    return subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )


def write_reports(
    suite_names: list[str],
    test_names: list[str],
    result: subprocess.CompletedProcess[str] | None,
    dry_run: bool,
) -> tuple[Path, Path]:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    md_path, json_path = report_paths(now)
    status = "DRY RUN" if dry_run else ("PASS" if result and result.returncode == 0 else "FAIL")
    output = "" if result is None else result.stdout
    command = [sys.executable, "-m", "unittest", *test_names, "-v"]
    manual_notes = []
    for suite in suite_names:
        manual_notes.extend(MANUAL_NOTES.get(suite, []))

    payload = {
        "created_at": now.isoformat(timespec="seconds"),
        "status": status,
        "suites": suite_names,
        "test_target_count": len(test_names),
        "tests": test_names,
        "command": command,
        "returncode": None if result is None else result.returncode,
        "manual_notes": manual_notes,
        "output": output,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# CRM Regression Report",
        "",
        f"- Created at: {payload['created_at']}",
        f"- Status: {status}",
        f"- Suites: {', '.join(suite_names)}",
        f"- Test targets: {len(test_names)}",
        f"- Command: `{' '.join(command)}`",
        "",
        "## Tests",
        "",
    ]
    lines.extend(f"- `{name}`" for name in test_names)
    lines.extend(["", "## Manual Follow-up", ""])
    if manual_notes:
        lines.extend(f"- {note}" for note in manual_notes)
    else:
        lines.append("- None recorded.")
    lines.extend(["", "## Output", "", "```text", output.strip() if output else "(dry run)", "```", ""])
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return md_path, json_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run AAIE CRM regression checks.")
    parser.add_argument("--core", action="store_true", help="Run the small core smoke suite.")
    parser.add_argument("--student-info", dest="student_info", action="store_true", help="Run student information form checks.")
    parser.add_argument("--contract", action="store_true", help="Run contract flow checks.")
    parser.add_argument("--school-plan", dest="school_plan", action="store_true", help="Run school plan checks.")
    parser.add_argument("--preview", action="store_true", help="Run file preview checks.")
    parser.add_argument("--interaction", action="store_true", help="Run page interaction and no-scroll-to-top checks.")
    parser.add_argument("--full", action="store_true", help="Run every regression suite defined in this script.")
    parser.add_argument("--list", action="store_true", help="List suites and tests without running them.")
    parser.add_argument("--dry-run", action="store_true", help="Show selected tests and write a dry-run report without executing tests.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    suite_names = selected_suites(args)
    test_names = dedupe_tests(suite_names)

    if args.list or args.dry_run:
        print("Selected regression suites:")
        for suite in suite_names:
            print(f"- {suite}")
        print("")
        print("Selected tests:")
        for test_name in test_names:
            print(f"- {test_name}")
        print("")
        print("Reports directory: reports/regression")
        if args.dry_run:
            md_path, json_path = write_reports(suite_names, test_names, None, dry_run=True)
            print(f"Dry-run report: {md_path.relative_to(ROOT)}")
            print(f"Dry-run JSON: {json_path.relative_to(ROOT)}")
        return 0

    result = run_unittest(test_names)
    print(result.stdout, end="")
    md_path, json_path = write_reports(suite_names, test_names, result, dry_run=False)
    print(f"\nRegression report: {md_path.relative_to(ROOT)}")
    print(f"Regression JSON: {json_path.relative_to(ROOT)}")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
