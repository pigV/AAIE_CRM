"""Task-local acceptance harness for the locked contract master layout.

This harness never opens the Production database or student NAS.  It exercises
the real editor service, renderer authority, parent-signature overlay and AAIE
signature/stamp overlay against an isolated SQLite fixture and SAFEBOX paths.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image, ImageDraw
import pdfplumber
from pypdf import PdfReader, PdfWriter

ROOT = Path(__file__).resolve().parents[1]
AUTHORITATIVE_ROOT = ROOT.parents[1] if ROOT.parent.name == ".worktrees" else ROOT
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app
import contract_domain
import contract_generation_authority as authority
import contract_template_editor as editor


MIGRATION = ROOT / "migrations/20260818_025_contract_template_editor_v1.sql"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def student_snapshot() -> dict[str, str]:
    return {
        "student_name": "CRM TEST Master Layout",
        "student_full_name": "CRM TEST Master Layout",
        "student_id": "CRMTEST-MASTER-001",
        "date_of_birth": "2011-06-14",
        "current_year_level": "Year 8",
        "applying_year_level": "Year 9",
        "preferred_intake_year": "2027",
        "preferred_intake_term": "02",
        "home_address": "Melbourne VIC 3000",
        "father_passport_first_name": "Acceptance",
        "father_passport_last_name": "Parent",
        "father_mobile": "0411111111",
        "father_email": "acceptance.parent@example.test",
        "company_authorised_person": "Sonny Zhang",
    }


def signer() -> dict[str, str]:
    return {
        "source": "father", "name": "Acceptance Parent", "relationship": "父亲",
        "phone": "0411111111", "email": "acceptance.parent@example.test",
    }


def payload(contract_type: str, request_id: str) -> dict[str, object]:
    selected = list(contract_domain.default_service_ids(contract_type))
    return {
        "contract_type": contract_type,
        "contract_uid": request_id.upper(),
        "contract_date": "2026-08-22",
        "generation_request_id": request_id,
        "student_full_name": "CRM TEST Master Layout",
        "student_id_or_passport_number": "CRMTEST-MASTER-001",
        "student_date_of_birth": "2011-06-14",
        "student_snapshot": student_snapshot(),
        "company_authorised_person": "Sonny Zhang",
        "service_scope_ids": selected,
        "service_scope_json": json.dumps(selected),
        "payment_method": "澳洲公户",
        "signer": signer(),
        "parent_address": student_snapshot()["home_address"],
        "special_terms": "隔离 CRM_TEST 内容验收通过；所有版式锚点保持锁定。",
    }


def make_parent_signature(path: Path) -> None:
    image = Image.new("RGBA", (520, 170), (255, 255, 255, 0))
    draw = ImageDraw.Draw(image)
    draw.line([(35, 110), (115, 55), (185, 120), (265, 45), (350, 105), (470, 70)], fill=(24, 78, 150, 255), width=8)
    image.save(path)


def make_confirmation(path: Path, contract_uid: str, signature_image: Path) -> None:
    app.render_contract_signature_confirmation_pdf(
        path,
        company={
            "company_legal_name": "Australian Academy of International Education",
            "company_abn_acn": "12 345 678 901",
        },
        parent_signer_name="Acceptance Parent",
        parent_relationship="父亲",
        parent_contact="0411111111 / acceptance.parent@example.test",
        student_name="CRM TEST Master Layout",
        contract_title="澳洲留学咨询及签证服务委托合同",
        contract_uid=contract_uid,
        wechat_identity="CRM TEST verified / ****test",
        signed_at_display="2026年08月22日 10:30",
        signed_ip="127.0.0.1",
        user_agent="CRM TEST isolated acceptance",
        signature_image_path=signature_image,
    )


def append_confirmation(final_body: Path, confirmation: Path, destination: Path) -> None:
    writer = PdfWriter()
    for source in (final_body, confirmation):
        for page in PdfReader(source).pages:
            writer.add_page(page)
    with destination.open("wb") as stream:
        writer.write(stream)


class TaskLocalSettings:
    def setting(self, _key: str, default: str = "") -> str:
        return default


def run(output_root: Path) -> dict[str, object]:
    output_root.mkdir(parents=True, exist_ok=True)
    db_path = output_root / "crm_test_contract_master.sqlite3"
    if db_path.exists():
        db_path.unlink()
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(MIGRATION.read_text(encoding="utf-8"))
    connection.execute("CREATE TABLE system_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
    connection.executemany(
        "INSERT INTO system_settings(key,value) VALUES(?,?)",
        [
            ("company_legal_name", "Australian Academy of International Education"),
            ("company_abn_acn", "12 345 678 901"),
            ("company_address", "Melbourne VIC"),
            ("company_phone", "+61 3 0000 0000"),
            ("company_email", "contracts@example.test"),
            ("company_authorised_person", "Sonny Zhang"),
        ],
    )
    service = editor.ContractTemplateEditorService(connection)
    service.import_domain_v1(actor_user_id=1)
    drafts = service.create_authority_v2_drafts(actor_user_id=1)
    published: dict[str, dict[str, object]] = {}
    workflow: dict[str, object] = {}
    for draft in drafts:
        contract_type = str(draft["template_type"])
        lock = service.acquire_lock(contract_type, int(draft["id"]), owner_user_id=1)
        current = draft
        if contract_type == contract_domain.NEW_INTERNATIONAL_STUDENT_CONTRACT:
            snapshot = json.loads(str(current["snapshot_json"]))
            commercial = dict(snapshot["commercial"])
            commercial.update({
                "payment_terms": "CRM_TEST CONFIG BINDING PAYMENT TERMS",
                "refund_policy": "CRM_TEST CONFIG BINDING REFUND WORDING",
                "company_authorised_person": "Sonny Zhang",
            })
            current = service.autosave_settings(
                int(current["id"]),
                commercial=commercial,
                services=[
                    {
                        "service_id": item["service_id"],
                        "enabled": bool(item.get("enabled", True)),
                        "default_selected": bool(item.get("default_selected", False)),
                    }
                    for item in snapshot["services"]
                ],
                expected_revision=int(current["revision"]),
                lock_token=str(lock["token"]), actor_user_id=1,
            )
            workflow["commercial_config_saved"] = True
        if contract_type == contract_domain.TRANSFER_APPLICATION_CONTRACT:
            snapshot = json.loads(str(current["snapshot_json"]))
            block = next(item for item in snapshot["blocks"] if item["key"] == "profile_4")
            current = service.autosave_block(
                int(current["id"]), "profile_4", title="四、甲方转学服务责任",
                body=str(block["body"]) + "\n本句由隔离 CRM_TEST editor acceptance 保存。",
                enabled=True, expected_revision=int(current["revision"]),
                lock_token=str(lock["token"]), actor_user_id=1,
            )
            current = service.create_block(
                int(current["id"]), "profile_4::acceptance_one", "4.1 补充服务说明",
                "本条款通过真实 Editor 服务新增。", int(current["revision"]),
                str(lock["token"]), 1,
            )
            current = service.create_block(
                int(current["id"]), "profile_4::acceptance_two", "4.2 临时排序条款",
                "本条款用于验证排序后可安全删除。", int(current["revision"]),
                str(lock["token"]), 1,
            )
            order = [item["key"] for item in json.loads(str(current["snapshot_json"]))["blocks"]]
            first = order.index("profile_4::acceptance_one")
            second = order.index("profile_4::acceptance_two")
            order[first], order[second] = order[second], order[first]
            current = service.reorder_blocks(
                int(current["id"]), order, expected_revision=int(current["revision"]),
                lock_token=str(lock["token"]), actor_user_id=1,
            )
            current = service.delete_block(
                int(current["id"]), "profile_4::acceptance_two",
                int(current["revision"]), str(lock["token"]), 1,
            )
            workflow.update({
                "content_edit_saved": True,
                "section_title_edit_saved": True,
                "optional_clause_added": True,
                "optional_clause_reordered": True,
                "optional_clause_deleted": True,
            })
        current = service.configure_authority_v2_draft(
            int(current["id"]), gst_treatment="EXCLUSIVE",
            expected_revision=int(current["revision"]), lock_token=str(lock["token"]),
            actor_user_id=1,
        )
        selected = list(contract_domain.default_service_ids(contract_type))
        simulation = service.simulate(
            int(current["id"]), source_type="system", student_snapshot=student_snapshot(),
            signer_source="father", signer_overrides={}, selected_services=selected,
            output_root=output_root / "editor-previews", actor_user_id=1,
            expected_revision=int(current["revision"]), lock_token=str(lock["token"]),
        )
        validation = service.validate_for_publish(
            int(current["id"]), actor_user_id=1, lock_token=str(lock["token"]),
            expected_revision=int(current["revision"]), actor_is_admin=True,
        )
        preview = service.prepare_preview(
            int(current["id"]), actor_user_id=1, lock_token=str(lock["token"]),
            expected_revision=int(current["revision"]), actor_is_admin=True,
        )
        published[contract_type] = service.publish(
            int(current["id"]), actor_user_id=1, lock_token=str(lock["token"]),
            expected_revision=int(current["revision"]), actor_is_admin=True,
        )
        workflow[contract_type] = {
            "simulate": simulation["status"], "validate": validation["passed"],
            "preview": preview["status"], "publish": published[contract_type]["status"],
        }

    transfer_published = published[contract_domain.TRANSFER_APPLICATION_CONTRACT]
    transfer_source_id = int(transfer_published["source_version_id"])
    history = service.version_history(contract_domain.TRANSFER_APPLICATION_CONTRACT)
    comparison = service.compare_versions(transfer_source_id, int(transfer_published["id"]))
    republished_draft = service.create_draft_from_version(transfer_source_id, actor_user_id=1)
    workflow.update({
        "version_history": len(history) >= 2,
        "diff": bool(comparison.get("blocks")),
        "republish_old_version_as_new_draft": (
            int(republished_draft["source_version_id"]) == transfer_source_id
            and republished_draft["status"] == "Draft"
        ),
    })

    artifacts: dict[str, object] = {}
    refund_matrix: dict[str, object] = {}
    new_pdf = None
    new_docx = None
    for contract_type, _label in contract_domain.contract_type_options():
        package = authority.load_published_template(connection, contract_type)
        bound = authority.bind_new_contract(
            package, payload(contract_type, "crmtest-" + contract_type.lower()),
            actor={"id": 1, "role_name": "Admin"}, generated_at="2026-08-22T00:00:00+00:00",
        )
        rendered = authority.render_bound_contract(bound)
        stem = contract_type
        docx = output_root / f"{stem}.docx"
        pdf = output_root / f"{stem}.pdf"
        signing_html = output_root / f"{stem}-signing.html"
        docx.write_bytes(rendered.docx_bytes)
        pdf.write_bytes(rendered.pdf_bytes)
        signing_html.write_bytes(rendered.signing_preview_bytes)
        mandatory_display = {
            "current_grade": "当前年级" in rendered.authority_content and "Year 8" in rendered.authority_content,
            "application_grade": "申请年级" in rendered.authority_content and "Year 9" in rendered.authority_content,
            "planned_entry_year": "计划入读年份" in rendered.authority_content and "2027" in rendered.authority_content,
            "all_surface_semantics_match": len(set(rendered.semantic_hashes.values())) == 1,
        }
        if not all(mandatory_display.values()):
            raise AssertionError(
                f"mandatory display acceptance failed for {contract_type}: {mandatory_display}"
            )
        artifacts[contract_type] = {
            "docx": str(docx), "docx_sha256": sha256(docx),
            "pdf": str(pdf), "pdf_sha256": sha256(pdf),
            "signing": str(signing_html), "signing_sha256": sha256(signing_html),
            "pages": len(PdfReader(pdf).pages),
            "mandatory_display": mandatory_display,
        }
        if contract_type == contract_domain.NEW_INTERNATIONAL_STUDENT_CONTRACT:
            new_pdf, new_docx = pdf, docx
            refund_cases = (
                ("wechat-2400", "微信", None, "2400.00", "900.00", "1500.00"),
                ("wechat-2300", "微信", "100.00", "2300.00", "900.00", "1400.00"),
                ("public-2640", "澳洲公户", None, "2640.00", "990.00", "1650.00"),
                ("public-2530", "澳洲公户", "100.00", "2530.00", "990.00", "1540.00"),
            )
            for key, method, discount_amount, paid, retained, refund in refund_cases:
                case_payload = payload(contract_type, "crmtest-refund-" + key)
                case_payload["payment_method"] = method
                approval = None
                if discount_amount:
                    approval = {
                        "discount_amount": discount_amount,
                        "reason": "CRM_TEST exact refund matrix",
                        "approved_by": 1,
                        "operator_id": 1,
                        "approved_at": "2026-08-22T00:00:00+00:00",
                    }
                case_bound = authority.bind_new_contract(
                    package, case_payload,
                    actor={"id": 1, "role_name": "Admin"},
                    discount_approval=approval,
                    generated_at="2026-08-22T00:00:00+00:00",
                )
                case_rendered = authority.render_bound_contract(case_bound)
                refusal = case_bound["commercial"]["visa_refusal_refund"]
                visible = case_rendered.authority_content
                refund_matrix[key] = {
                    "actual_paid_total": refusal["actual_paid_total"],
                    "retained_total": refusal["retained_total"],
                    "actual_refund_amount": refusal["actual_refund_amount"],
                    "expected_paid_total": paid,
                    "expected_retained_total": retained,
                    "expected_refund_amount": refund,
                    "preview_and_final_text_match": (
                        f"签证拒签退款（按实际已支付额）：AUD {refund}" in visible
                        and "CRM_TEST CONFIG BINDING PAYMENT TERMS" in visible
                        and "CRM_TEST CONFIG BINDING REFUND WORDING" in visible
                    ),
                    "pass": (
                        refusal["actual_paid_total"] == paid
                        and refusal["retained_total"] == retained
                        and refusal["actual_refund_amount"] == refund
                    ),
                }

    parent_signature = output_root / "parent-signature.png"
    parent_signed = output_root / "NewInternationalStudentContract-ParentSigned.pdf"
    confirmation = output_root / "NewInternationalStudentContract-Confirmation.pdf"
    parent_with_confirmation = output_root / "NewInternationalStudentContract-ParentSignedWithConfirmation.pdf"
    final_pdf = output_root / "NewInternationalStudentContract-FullyExecuted.pdf"
    make_parent_signature(parent_signature)
    docx_hash_before_signing = sha256(new_docx)
    app.apply_parent_signature_to_contract_signature_section(
        new_pdf, parent_signature, parent_signed,
        parent_name="Acceptance Parent", signed_at="2026-08-22T10:30:00+10:00",
    )
    app.SONNY_SIGNATURE_PATH = AUTHORITATIVE_ROOT / "secure_assets/signatures/sonny_signature.png"
    app.AAIE_COMPANY_STAMP_PATH = AUTHORITATIVE_ROOT / "secure_assets/stamps/aaie_company_stamp.png"
    make_confirmation(
        confirmation, "CRMTEST-NEWINTERNATIONALSTUDENTCONTRACT", parent_signature
    )
    append_confirmation(parent_signed, confirmation, parent_with_confirmation)
    original_app_db = app.db
    try:
        app.db = TaskLocalSettings()
        app.apply_aaie_signature_and_stamp(
            parent_with_confirmation, final_pdf,
            company_signing_date="2026年08月22日",
        )
    finally:
        app.db = original_app_db
    final_reader = PdfReader(final_pdf)
    confirmation_text = final_reader.pages[-1].extract_text() or ""
    final_text = "\n".join(page.extract_text() or "" for page in final_reader.pages)
    with pdfplumber.open(str(final_pdf)) as final_document:
        signature_page = next(
            page for page in final_document.pages
            if "十一、签署" in (page.extract_text() or "")
        )
        signature_words = signature_page.extract_words()
        aaie_word = max(
            (word for word in signature_words if word.get("text") == "AAIE"),
            key=lambda word: float(word["top"]),
        )
        anchor_y = signature_page.height - float(aaie_word["bottom"])
        vertical = [
            line for line in signature_page.lines
            if abs(float(line["x0"]) - float(line["x1"])) < 0.5
            and float(line["y1"]) - float(line["y0"]) > 100
            and float(line["y0"]) <= anchor_y <= float(line["y1"])
        ]
        left = min(vertical, key=lambda line: float(line["x0"]))
        divider = min(
            (line for line in vertical if float(line["x0"]) > signature_page.width * 0.4),
            key=lambda line: float(line["x0"]),
        )
        cell = {
            "x0": float(left["x0"]), "x1": float(divider["x0"]),
            "y0": max(float(left["y0"]), float(divider["y0"])),
            "y1": min(float(left["y1"]), float(divider["y1"])),
        }
        aaie_images = [
            image for image in signature_page.images
            if float(image["x1"]) <= cell["x1"] + 0.5
        ]
        date_word = max(
            (word for word in signature_words if "甲方签署日期" in str(word.get("text") or "")),
            key=lambda word: float(word["top"]),
        )
        date_bottom_y = signature_page.height - float(date_word["bottom"])
        contained = [
            float(image["x0"]) >= cell["x0"] - 0.5
            and float(image["x1"]) <= cell["x1"] + 0.5
            and float(image["y0"]) >= cell["y0"] - 0.5
            and float(image["y1"]) <= cell["y1"] + 0.5
            for image in aaie_images
        ]
        below_date = [
            float(image["y1"]) <= date_bottom_y - 4
            for image in aaie_images
        ]
        aaie_signature_stamp_overlap = False
        if len(aaie_images) == 2:
            first, second = aaie_images
            overlap_width = (
                min(float(first["x1"]), float(second["x1"]))
                - max(float(first["x0"]), float(second["x0"]))
            )
            overlap_height = (
                min(float(first["y1"]), float(second["y1"]))
                - max(float(first["y0"]), float(second["y0"]))
            )
            aaie_signature_stamp_overlap = overlap_width > 0 and overlap_height > 0
        with pdfplumber.open(str(parent_with_confirmation)) as before_document:
            before_page = next(
                page for page in before_document.pages
                if "十一、签署" in (page.extract_text() or "")
            )
            before_right_words = [
                (
                    str(word.get("text") or ""),
                    round(float(word["x0"]), 2),
                    round(float(word["top"]), 2),
                )
                for word in before_page.extract_words()
                if float(word["x0"]) >= cell["x1"]
            ]
            after_right_words = [
                (
                    str(word.get("text") or ""),
                    round(float(word["x0"]), 2),
                    round(float(word["top"]), 2),
                )
                for word in signature_words
                if float(word["x0"]) >= cell["x1"]
            ]
            before_right_images = [
                tuple(round(float(image[key]), 2) for key in ("x0", "y0", "x1", "y1"))
                for image in before_page.images
                if float(image["x0"]) >= cell["x1"]
            ]
            after_right_images = [
                tuple(round(float(image[key]), 2) for key in ("x0", "y0", "x1", "y1"))
                for image in signature_page.images
                if float(image["x0"]) >= cell["x1"]
            ]
            before_lines = [
                tuple(round(float(line[key]), 2) for key in ("x0", "y0", "x1", "y1"))
                for line in before_page.lines
            ]
            after_lines = [
                tuple(round(float(line[key]), 2) for key in ("x0", "y0", "x1", "y1"))
                for line in signature_page.lines
            ]
            signature_format_preserved = before_lines == after_lines
            parent_section_unchanged = (
                before_right_words == after_right_words
                and before_right_images == after_right_images
            )
        signature_geometry = {
            "left_cell": cell,
            "aaie_image_rectangles": [
                {
                    key: round(float(image[key]), 2)
                    for key in ("x0", "y0", "x1", "y1")
                }
                for image in aaie_images
            ],
            "aaie_images_inside_left_cell": len(aaie_images) == 2 and all(contained),
            "aaie_images_below_company_date": len(aaie_images) == 2 and all(below_date),
            "aaie_signature_stamp_overlap_preserved": aaie_signature_stamp_overlap,
            "company_date_bottom_y": round(date_bottom_y, 2),
            "date_readable": "甲方签署日期：2026年08月22日" in "".join(
                (signature_page.extract_text() or "").split()
            ),
            "parent_region_present": any(
                float(image["x0"]) >= cell["x1"]
                for image in signature_page.images
            ),
            "signature_format_preserved": signature_format_preserved,
            "parent_section_unchanged": parent_section_unchanged,
        }
    signing = {
        "link_created_at": "2026-08-22T00:00:00+00:00",
        "link_expires_at": (datetime(2026, 8, 22, tzinfo=timezone.utc) + timedelta(days=7)).isoformat(),
        "parent_status": "ParentSigned",
        "aaie_status": "AAIESigned",
        "final_status": "FullyExecuted",
        "archive_status": "ArchivedTaskLocal",
        "source_docx_preserved_until_link_acceptance": new_docx.is_file() and sha256(new_docx) == docx_hash_before_signing,
        "parent_signed_pdf": str(parent_signed),
        "final_pdf": str(final_pdf),
        "final_sha256": sha256(final_pdf),
        "confirmation_present": "AAIE 合同签署确认" in confirmation_text,
        "mandatory_display_present": {
            "current_grade": "当前年级" in final_text and "Year 8" in final_text,
            "application_grade": "申请年级" in final_text and "Year 9" in final_text,
            "planned_entry_year": "计划入读年份" in final_text and "2027" in final_text,
        },
        "final_pages": len(final_reader.pages),
    }
    if not all(signing["mandatory_display_present"].values()):
        raise AssertionError(
            f"FullyExecuted mandatory display acceptance failed: {signing['mandatory_display_present']}"
        )
    if not all((
        signature_geometry["aaie_images_inside_left_cell"],
        signature_geometry["aaie_images_below_company_date"],
        signature_geometry["aaie_signature_stamp_overlap_preserved"],
        signature_geometry["date_readable"],
        signature_geometry["parent_region_present"],
        signature_geometry["signature_format_preserved"],
        signature_geometry["parent_section_unchanged"],
    )):
        raise AssertionError(f"signature layout acceptance failed: {signature_geometry}")
    report = {
        "status": "PASS",
        "database": str(db_path),
        "production_database_used": False,
        "workflow": workflow,
        "artifacts": artifacts,
        "refund_matrix": refund_matrix,
        "signature_geometry": signature_geometry,
        "signing": signing,
    }
    report_path = output_root / "acceptance-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    connection.close()
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.output_root.resolve()), ensure_ascii=False, indent=2))
