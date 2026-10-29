"""Atomic persistence boundary for Published V2 contract generation.

This module deliberately has no Production configuration, signing, Workflow or
NAS knowledge.  Callers provide an already-resolved target directory and emit
business facts only after this boundary returns successfully.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from contract_domain import snapshot_payload, snapshot_storage_json
from contract_generation_authority import RenderedBoundContract


class ContractGenerationPersistenceError(RuntimeError):
    pass


class ContractGenerationRequestConflict(ContractGenerationPersistenceError):
    pass


GENERATION_AUTHORITY_MODES = {"legacy_v6", "published_v2"}


def set_generation_authority_atomic(
    database,
    *,
    mode: str,
    actor: Mapping[str, object],
    occurred_at: str,
) -> tuple[str, str]:
    """Switch the single generation authority and audit it in one transaction.

    This helper is intentionally not exposed as a browser route. Production use
    remains a separately authorized deployment action; candidate tests exercise
    it only against an isolated SQLite database.
    """
    normalized = str(mode or "").strip()
    if normalized not in GENERATION_AUTHORITY_MODES:
        raise ContractGenerationPersistenceError("合同生成权威配置无效。")
    if str(actor.get("role_name") or "") != "Admin" or not actor.get("id"):
        raise ContractGenerationPersistenceError("只有Admin可以切换合同生成权威。")
    with database.transaction() as conn:
        row = conn.execute(
            "SELECT value FROM system_settings WHERE key='contract_generation_authority'"
        ).fetchone()
        previous = str(row["value"] if row else "legacy_v6")
        if previous not in GENERATION_AUTHORITY_MODES:
            raise ContractGenerationPersistenceError("现有合同生成权威配置无效。")
        if previous == normalized:
            return previous, normalized
        conn.execute(
            """INSERT INTO system_settings(key,value,updated_by,updated_at)
               VALUES('contract_generation_authority',?,?,?)
               ON CONFLICT(key) DO UPDATE SET
                 value=excluded.value,updated_by=excluded.updated_by,updated_at=excluded.updated_at""",
            (normalized, actor["id"], occurred_at),
        )
        conn.execute(
            """INSERT INTO audit_logs(
                 user_id,username,action_type,module_name,module_key,record_id,
                 old_value,new_value,status,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                actor["id"], str(actor.get("username") or ""),
                "contract_generation_authority_switched", "contracts", "contracts",
                "contract_generation_authority", previous, normalized, "success", occurred_at,
            ),
        )
    return previous, normalized


@dataclass(frozen=True)
class PersistedContractResult:
    contract_id: int
    document_id: int
    file_path: str
    created: bool


def _bound_from_row(row) -> dict[str, object]:
    return snapshot_payload(row["standalone_data_json"] or "")


def _matching_request(rows, request_id: str):
    for row in rows:
        bound = _bound_from_row(row)
        if str(bound.get("generation_request_id") or "") == request_id:
            return row, bound
    return None, None


def _existing_request(database, student_id: str, request_id: str):
    rows = database.all(
        "SELECT * FROM contracts WHERE student_id=? ORDER BY id DESC", (student_id,)
    )
    return _matching_request(rows, request_id)


def _existing_request_conn(conn, student_id: str, request_id: str):
    rows = conn.execute(
        "SELECT * FROM contracts WHERE student_id=? ORDER BY id DESC", (student_id,)
    ).fetchall()
    return _matching_request(rows, request_id)


def _assert_replay_fingerprint(bound: Mapping[str, object], expected: str) -> None:
    if str(bound.get("request_fingerprint") or "") != str(expected or ""):
        raise ContractGenerationRequestConflict(
            "同一生成请求ID已用于不同合同内容，请刷新页面后重试。"
        )


def _insert_audit(
    conn,
    *,
    user: Mapping[str, object],
    action: str,
    contract_id: int,
    student_id: str,
    file_path: str,
    new_value: Mapping[str, object],
    occurred_at: str,
) -> None:
    conn.execute(
        """INSERT INTO audit_logs(
           user_id,username,action_type,module_name,module_key,record_id,file_path,
           old_value,new_value,student_id,contract_id,ip_address,user_agent,device_info,
           status,error_message,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            user.get("id"), str(user.get("username") or ""), action,
            "contracts", "contracts", str(contract_id), file_path, "",
            json.dumps(dict(new_value), ensure_ascii=False, sort_keys=True),
            student_id, str(contract_id), "", "", "", "success", "", occurred_at,
        ),
    )


def _row_values(bound: Mapping[str, object]) -> dict[str, object]:
    template = dict(bound.get("template_snapshot") or {})
    commercial = dict(bound.get("commercial") or {})
    instance = dict(bound.get("contract_instance") or {})
    signer = dict(bound.get("signer") or {})
    return {
        "contract_category": str(template.get("display_name") or template.get("template_name") or "合同"),
        "contract_type": str(bound.get("contract_type") or ""),
        "contract_title": str(template.get("contract_title") or template.get("template_name") or "合同"),
        "contract_date": str(instance.get("contract_date") or ""),
        "service_type": "Published V2 Template Services",
        "service_scope_json": json.dumps(bound.get("selected_service_ids") or [], ensure_ascii=False),
        "other_service_scope_text": str(instance.get("other_service_scope_text") or ""),
        "service_fee_currency": str(commercial.get("payment_method") or ""),
        "consulting_and_visa_service_fee": str(commercial.get("standard_service_fee") or ""),
        "discount_amount": str(commercial.get("discount_amount") or "0.00"),
        "final_fee_amount": str(commercial.get("final_fee_amount") or ""),
        "payment_due_date": str(commercial.get("payment_plan_display") or ""),
        "refund_policy_type": "published_v2",
        "refund_policy_text": str(commercial.get("refund_policy") or ""),
        "refusal_deduction_currency": str(commercial.get("currency") or "AUD"),
        "refusal_deduction_amount": str(
            (commercial.get("visa_refusal_refund") or {}).get("retained_total") or ""
        ),
        "refusal_refund_currency": str(commercial.get("currency") or "AUD"),
        "refusal_refund_amount": str(
            (commercial.get("visa_refusal_refund") or {}).get("actual_refund_amount") or ""
        ),
        "refund_notes": str(commercial.get("refund_notes") or ""),
        "special_terms": str(instance.get("special_terms") or ""),
        "parent_signer_name": str(signer.get("name") or ""),
        "parent_phone": str(signer.get("phone") or ""),
        "parent_email": str(signer.get("email") or ""),
        "parent_address": str(instance.get("parent_address") or ""),
        "notes": str(instance.get("notes") or ""),
        "template_version": f"PublishedV{int(bound.get('template_version') or 0)}",
    }


def _stage_artifact(rendered: RenderedBoundContract, target: Path, request_id: str) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    safe_request = "".join(ch for ch in request_id if ch.isalnum() or ch in "-_")[:80]
    staging = target.with_name(f".{target.name}.{safe_request}.staging")
    if staging.exists():
        staging.unlink()
    staging.write_bytes(rendered.docx_bytes)
    return staging


def create_contract_atomic(
    database,
    *,
    bound: Mapping[str, object],
    rendered: RenderedBoundContract,
    student_data: Mapping[str, object],
    student_id: str,
    contract_uid: str,
    target: Path,
    file_version: int,
    user: Mapping[str, object],
    occurred_at: str,
) -> PersistedContractResult:
    request_id = str(bound.get("generation_request_id") or "")
    fingerprint = str(bound.get("request_fingerprint") or "")
    if not request_id or not fingerprint:
        raise ContractGenerationPersistenceError("合同请求缺少幂等身份。")
    existing, existing_bound = _existing_request(database, student_id, request_id)
    if existing is not None:
        _assert_replay_fingerprint(existing_bound, fingerprint)
        document = database.one(
            "SELECT id FROM student_documents WHERE student_id=? AND file_path=? ORDER BY id DESC LIMIT 1",
            (student_id, existing["contract_file_path"]),
        )
        return PersistedContractResult(
            int(existing["id"]), int(document["id"]) if document else 0,
            str(existing["contract_file_path"] or ""), False,
        )

    target = Path(target)
    staging = _stage_artifact(rendered, target, request_id)
    promoted = False
    try:
        with database.transaction() as conn:
            duplicate, duplicate_bound = _existing_request_conn(conn, student_id, request_id)
            if duplicate is not None:
                _assert_replay_fingerprint(duplicate_bound, fingerprint)
                document = conn.execute(
                    "SELECT id FROM student_documents WHERE student_id=? AND file_path=? ORDER BY id DESC LIMIT 1",
                    (student_id, duplicate["contract_file_path"]),
                ).fetchone()
                return PersistedContractResult(
                    int(duplicate["id"]), int(document["id"]) if document else 0,
                    str(duplicate["contract_file_path"] or ""), False,
                )
            if target.exists():
                raise ContractGenerationPersistenceError("合同目标文件已存在，请刷新后重试。")
            os.replace(staging, target)
            promoted = True
            values = _row_values(bound)
            snapshot_json = snapshot_storage_json(bound, student_data)
            cursor = conn.execute(
                """INSERT INTO contracts(
                  student_id,contract_uid,is_standalone_contract,standalone_data_json,
                  contract_category,contract_type,contract_title,contract_status,contract_date,
                  service_type,service_scope_json,other_service_scope_text,service_fee_currency,
                  consulting_and_visa_service_fee,discount_amount,final_fee_amount,payment_due_date,
                  refund_policy_type,refund_policy_text,refusal_deduction_currency,refusal_deduction_amount,
                  refusal_refund_currency,refusal_refund_amount,refund_notes,special_terms,
                  parent_signer_name,parent_phone,parent_email,parent_address,notes,contract_file_path,
                  template_version,generated_by,generated_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    student_id, contract_uid, 0, snapshot_json,
                    values["contract_category"], values["contract_type"], values["contract_title"],
                    "Draft", values["contract_date"], values["service_type"],
                    values["service_scope_json"], values["other_service_scope_text"],
                    values["service_fee_currency"], values["consulting_and_visa_service_fee"],
                    values["discount_amount"], values["final_fee_amount"], values["payment_due_date"],
                    values["refund_policy_type"], values["refund_policy_text"],
                    values["refusal_deduction_currency"], values["refusal_deduction_amount"],
                    values["refusal_refund_currency"], values["refusal_refund_amount"],
                    values["refund_notes"], values["special_terms"], values["parent_signer_name"],
                    values["parent_phone"], values["parent_email"], values["parent_address"],
                    values["notes"], str(target), values["template_version"], user.get("id"),
                    occurred_at, occurred_at,
                ),
            )
            contract_id = int(cursor.lastrowid)
            document = conn.execute(
                """INSERT INTO student_documents(
                   student_id,folder_category,document_type,original_name,stored_name,file_path,
                   version,file_size,uploaded_by,uploaded_at,preview_path,is_active
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,1)""",
                (
                    student_id, "Forms", "StudyAbroadContract", target.name, target.name,
                    str(target), file_version, len(rendered.docx_bytes), user.get("id"), occurred_at, "",
                ),
            )
            document_id = int(document.lastrowid)
            _insert_audit(
                conn, user=user, action="published_v2_contract_generated",
                contract_id=contract_id, student_id=student_id, file_path=str(target),
                new_value={
                    "document_id": document_id,
                    "template_identity_id": bound.get("template_identity_id"),
                    "template_version_id": bound.get("template_version_id"),
                    "request_id": request_id,
                    "commercial_snapshot_hash": hashlib.sha256(
                        json.dumps(
                            bound.get("commercial") or {}, ensure_ascii=False,
                            sort_keys=True, separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest(),
                    "discount_approval": (bound.get("commercial") or {}).get("discount_approval"),
                    "gst_free_approval": (bound.get("commercial") or {}).get("gst_free_approval"),
                    "payment_plan": (bound.get("commercial") or {}).get("payment_plan"),
                    "visa_refusal_refund": (bound.get("commercial") or {}).get("visa_refusal_refund"),
                },
                occurred_at=occurred_at,
            )
        return PersistedContractResult(contract_id, document_id, str(target), True)
    except Exception:
        if promoted and target.exists():
            target.unlink()
        raise
    finally:
        if staging.exists():
            staging.unlink()


def replace_draft_atomic(
    database,
    *,
    contract_id: int,
    student_id: str,
    bound: Mapping[str, object],
    rendered: RenderedBoundContract,
    student_data: Mapping[str, object],
    target: Path,
    file_version: int,
    user: Mapping[str, object],
    occurred_at: str,
) -> PersistedContractResult:
    request_id = str(bound.get("generation_request_id") or "")
    fingerprint = str(bound.get("request_fingerprint") or "")
    current = database.one(
        "SELECT * FROM contracts WHERE id=? AND student_id=?", (contract_id, student_id)
    )
    if current is None or current["contract_status"] != "Draft":
        raise ContractGenerationPersistenceError("只有Draft合同可以重新生成。")
    current_bound = _bound_from_row(current)
    if str(current_bound.get("generation_request_id") or "") == request_id:
        _assert_replay_fingerprint(current_bound, fingerprint)
        document = database.one(
            "SELECT id FROM student_documents WHERE student_id=? AND file_path=? ORDER BY id DESC LIMIT 1",
            (student_id, current["contract_file_path"]),
        )
        return PersistedContractResult(
            contract_id, int(document["id"]) if document else 0,
            str(current["contract_file_path"] or ""), False,
        )
    staging = _stage_artifact(rendered, Path(target), request_id)
    target = Path(target)
    promoted = False
    try:
        with database.transaction() as conn:
            locked = conn.execute(
                "SELECT * FROM contracts WHERE id=? AND student_id=?", (contract_id, student_id)
            ).fetchone()
            if locked is None or locked["contract_status"] != "Draft":
                raise ContractGenerationPersistenceError("合同状态已经变化，请刷新后重试。")
            locked_bound = _bound_from_row(locked)
            if str(locked_bound.get("generation_request_id") or "") == request_id:
                _assert_replay_fingerprint(locked_bound, fingerprint)
                document = conn.execute(
                    "SELECT id FROM student_documents WHERE student_id=? AND file_path=? ORDER BY id DESC LIMIT 1",
                    (student_id, locked["contract_file_path"]),
                ).fetchone()
                return PersistedContractResult(
                    contract_id, int(document["id"]) if document else 0,
                    str(locked["contract_file_path"] or ""), False,
                )
            if target.exists():
                raise ContractGenerationPersistenceError("合同目标文件已存在，请刷新后重试。")
            os.replace(staging, target)
            promoted = True
            values = _row_values(bound)
            snapshot_json = snapshot_storage_json(bound, student_data)
            document = conn.execute(
                """INSERT INTO student_documents(
                   student_id,folder_category,document_type,original_name,stored_name,file_path,
                   version,file_size,uploaded_by,uploaded_at,preview_path,is_active
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,1)""",
                (
                    student_id, "Forms", "StudyAbroadContract", target.name, target.name,
                    str(target), file_version, len(rendered.docx_bytes), user.get("id"), occurred_at, "",
                ),
            )
            document_id = int(document.lastrowid)
            conn.execute(
                """UPDATE contracts SET
                   contract_category=?,contract_type=?,contract_title=?,standalone_data_json=?,
                   contract_date=?,service_type=?,service_scope_json=?,other_service_scope_text=?,
                   service_fee_currency=?,consulting_and_visa_service_fee=?,discount_amount=?,
                   final_fee_amount=?,payment_due_date=?,refund_policy_type=?,refund_policy_text=?,
                   refusal_deduction_currency=?,refusal_deduction_amount=?,refusal_refund_currency=?,
                   refusal_refund_amount=?,refund_notes=?,special_terms=?,parent_signer_name=?,
                   parent_phone=?,parent_email=?,parent_address=?,notes=?,contract_file_path=?,
                   template_version=?,generated_by=?,generated_at=?,updated_at=?
                   WHERE id=? AND student_id=?""",
                (
                    values["contract_category"], values["contract_type"], values["contract_title"],
                    snapshot_json, values["contract_date"], values["service_type"],
                    values["service_scope_json"], values["other_service_scope_text"],
                    values["service_fee_currency"], values["consulting_and_visa_service_fee"],
                    values["discount_amount"], values["final_fee_amount"], values["payment_due_date"],
                    values["refund_policy_type"], values["refund_policy_text"],
                    values["refusal_deduction_currency"], values["refusal_deduction_amount"],
                    values["refusal_refund_currency"], values["refusal_refund_amount"],
                    values["refund_notes"], values["special_terms"], values["parent_signer_name"],
                    values["parent_phone"], values["parent_email"], values["parent_address"],
                    values["notes"], str(target), values["template_version"], user.get("id"),
                    occurred_at, occurred_at, contract_id, student_id,
                ),
            )
            _insert_audit(
                conn, user=user, action="published_v2_contract_draft_regenerated",
                contract_id=contract_id, student_id=student_id, file_path=str(target),
                new_value={
                    "document_id": document_id,
                    "request_id": request_id,
                    "commercial_snapshot_hash": hashlib.sha256(
                        json.dumps(
                            bound.get("commercial") or {}, ensure_ascii=False,
                            sort_keys=True, separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest(),
                    "discount_approval": (bound.get("commercial") or {}).get("discount_approval"),
                    "gst_free_approval": (bound.get("commercial") or {}).get("gst_free_approval"),
                    "payment_plan": (bound.get("commercial") or {}).get("payment_plan"),
                    "visa_refusal_refund": (bound.get("commercial") or {}).get("visa_refusal_refund"),
                },
                occurred_at=occurred_at,
            )
        return PersistedContractResult(contract_id, document_id, str(target), True)
    except Exception:
        if promoted and target.exists():
            target.unlink()
        raise
    finally:
        if staging.exists():
            staging.unlink()
