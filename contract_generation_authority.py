"""Strict Published-template authority for newly created student contracts.

The module reads templates and produces immutable in-memory bindings. It does
not write databases or files and has no signing, Workflow or storage behavior.
"""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Mapping

import contract_domain
import contract_display_bindings
import contract_template_editor as contract_template_renderer
from contract_template_editor import (
    AUTHORITY_V2_GST_VALUES,
    TemplateRuleError,
    authority_v2_commercial_issues,
    render_published_contract_artifacts,
)


AUTHORITY_ID = "PublishedContractTemplateV2"
RENDERER_SCHEMA = {
    "id": "AAIEContractMasterLayoutRenderer",
    "version": 4,
    "protected_regions": ["contract_identity", "commercial", "signature_region"],
    "instance_regions": ["selected_services", "other_service_scope", "special_terms"],
    "display_binding_schema_hash": contract_display_bindings.schema_hash(),
}
LEGACY_RENDERER_SCHEMA_V3 = {
    "id": "AAIEContractMasterLayoutRenderer",
    "version": 3,
    "protected_regions": ["contract_identity", "commercial", "signature_region"],
    "instance_regions": ["selected_services", "other_service_scope", "special_terms"],
    "implementation_hash": "a77eeb8415dfc54d21ebec3d5f29a142045f38b0520a014f386bcc50d7708d6d",
}
LEGACY_RENDERER_SCHEMA_V3_HASH = "6622ae22eea77ec41b6d003c130636a3255d438b64d95fbff8c7faa1e5b53f84"
LEGACY_RENDERER_SCHEMA_V2 = {
    "id": "PublishedContractTemplateV2Renderer",
    "version": 2,
    "protected_regions": ["contract_identity", "commercial", "signature_region"],
    "instance_regions": ["selected_services", "other_service_scope", "special_terms"],
    "implementation_hash": "2a35aa457228e4d8d2d88887810e85df010ecd948ed79784dc687aa886af5dfb",
}
LEGACY_RENDERER_SCHEMA_V2_HASH = "ae0b72465ef432bf22a0e8373aced367ad964d690875b9ef3ab6b8086ea4be7b"
LEGACY_RENDERER_SCHEMA = {
    "id": "PublishedContractTemplateV2Renderer",
    "version": 1,
    "protected_regions": ["contract_identity", "commercial", "signature_region"],
    "instance_regions": ["selected_services", "other_service_scope", "special_terms"],
    "implementation_hash": "0fa11c19d17a28f4a6878f7a25c23c7c257d9dbc23b3717a67ec734922e98afe",
}
LEGACY_RENDERER_SCHEMA_HASH = "30959ffe0306cb0cee3f750cfa28a92835bd0a5b2221f64b42f64ecc754cd0c2"


class PublishedTemplateAuthorityError(RuntimeError):
    pass


class PublishedTemplateUnavailable(PublishedTemplateAuthorityError):
    pass


class PublishedTemplateIntegrityError(PublishedTemplateAuthorityError):
    pass


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _renderer_implementation_hash() -> str:
    functions = (
        "build_visible_render_model",
        "render_published_contract_artifacts",
        "_build_docx_artifact_bytes",
        "_build_pdf_layout_plan",
        "_render_pdf_plan_bytes",
        "_build_signing_artifact_bytes",
        "build_master_render_model",
        "render_master_docx_bytes",
        "render_master_pdf_bytes",
        "render_master_signing_html",
    )
    source = "\n".join(
        inspect.getsource(getattr(contract_template_renderer, name)) for name in functions
    )
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


RENDERER_SCHEMA["implementation_hash"] = _renderer_implementation_hash()
RENDERER_SCHEMA_HASH = _hash(RENDERER_SCHEMA)


def _renderer_mode(bound: Mapping[str, object]) -> str:
    schema = bound.get("renderer_schema")
    schema_hash = bound.get("renderer_schema_hash")
    if schema == RENDERER_SCHEMA and schema_hash == RENDERER_SCHEMA_HASH:
        return "master_v4"
    if schema == LEGACY_RENDERER_SCHEMA_V3 and schema_hash == LEGACY_RENDERER_SCHEMA_V3_HASH:
        return "master_v3"
    if schema == LEGACY_RENDERER_SCHEMA_V2 and schema_hash == LEGACY_RENDERER_SCHEMA_V2_HASH:
        return "legacy_v2"
    if schema == LEGACY_RENDERER_SCHEMA and schema_hash == LEGACY_RENDERER_SCHEMA_HASH:
        return "legacy_v1"
    raise PublishedTemplateIntegrityError("合同冻结快照的渲染器版本不匹配。")


def _money(value: object, label: str) -> Decimal:
    try:
        amount = Decimal(str(value or "").strip()).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    except (InvalidOperation, ValueError) as exc:
        raise PublishedTemplateIntegrityError(f"{label}无效。") from exc
    if amount < 0:
        raise PublishedTemplateIntegrityError(f"{label}无效。")
    return amount


def _money_text(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), ".2f")


def _verify_bound_snapshot(bound: Mapping[str, object]) -> None:
    candidate = copy.deepcopy(dict(bound))
    expected = str(candidate.pop("bound_snapshot_hash", ""))
    if not expected or _hash(candidate) != expected:
        raise PublishedTemplateIntegrityError("合同冻结快照hash不匹配。")


def _validate_snapshot(snapshot: Mapping[str, object], contract_type: str) -> None:
    if snapshot.get("template_type") != contract_type:
        raise PublishedTemplateIntegrityError("Published模板类型不匹配。")
    commercial = dict(snapshot.get("commercial") or {})
    issues = authority_v2_commercial_issues(commercial)
    if commercial.get("commercial_schema") != "authority_v2" or issues:
        detail = issues[0]["message"] if issues else "不是Authority V2商业配置"
        raise PublishedTemplateIntegrityError(f"Published模板费用配置不完整：{detail}。")
    if str(commercial.get("service_fee")) != "2400.00" or str(
        commercial.get("standard_service_fee")
    ) != "2400.00":
        raise PublishedTemplateIntegrityError("Published模板标准费用必须为AUD 2400.00。")
    if commercial.get("gst_treatment") not in AUTHORITY_V2_GST_VALUES:
        raise PublishedTemplateIntegrityError("Published模板GST配置无效。")
    refusal_keys = {"visa_refusal_retained_ex_gst", "visa_refusal_rule_source"}
    if contract_type == contract_domain.NEW_INTERNATIONAL_STUDENT_CONTRACT:
        if str(commercial.get("visa_refusal_retained_ex_gst") or "") != "900.00" or not str(
            commercial.get("visa_refusal_rule_source") or ""
        ).strip():
            raise PublishedTemplateIntegrityError("新留学生模板拒签退款配置不完整。")
    elif refusal_keys.intersection(commercial):
        raise PublishedTemplateIntegrityError("Transfer或Local模板不得携带签证拒签退款配置。")
    blocks = list(snapshot.get("blocks") or [])
    if not blocks:
        raise PublishedTemplateIntegrityError("Published模板没有条款区块。")
    required_legal = {"service_scope", "refund", "termination", "privacy", "dispute"}
    present_legal = {
        str(block.get("legal_kind") or "") for block in blocks
        if block.get("enabled", True) and str(block.get("body") or "").strip()
    }
    if not required_legal.issubset(present_legal):
        raise PublishedTemplateIntegrityError("Published模板缺少必要法律或服务范围条款。")
    for block in blocks:
        body = str(block.get("body") or "")
        if block.get("required") and (not block.get("enabled", True) or not body.strip()):
            raise PublishedTemplateIntegrityError("Published模板必要条款被停用或为空。")
        if any(str(variable) not in body for variable in block.get("required_variables") or []):
            raise PublishedTemplateIntegrityError("Published模板必要变量被删除或改变。")
    allowed = set(contract_domain.allowed_service_ids(contract_type))
    services = list(snapshot.get("services") or [])
    enabled = {
        str(item.get("service_id") or "") for item in services if item.get("enabled", True)
    }
    if not enabled or not enabled.issubset(allowed):
        raise PublishedTemplateIntegrityError("Published模板服务目录与合同类型不匹配。")
    defaults = {
        str(item.get("service_id") or "") for item in services
        if item.get("enabled", True) and item.get("default_selected")
    }
    if not defaults.issubset(enabled):
        raise PublishedTemplateIntegrityError("Published模板默认服务无效。")
    if contract_type == contract_domain.NEW_INTERNATIONAL_STUDENT_CONTRACT and defaults.intersection({
        "subclass_590_guardian_visa_lodgement",
        "subclass_590_guardian_visa_consulting",
    }):
        raise PublishedTemplateIntegrityError("590服务不得默认选择。")


@dataclass(frozen=True)
class PublishedTemplatePackage:
    identity_id: int
    version_id: int
    version_number: int
    contract_type: str
    content_hash: str
    snapshot_hash: str
    snapshot: dict[str, object]


@dataclass(frozen=True)
class RenderedBoundContract:
    template_version_id: int
    docx_bytes: bytes
    pdf_bytes: bytes
    signing_preview_bytes: bytes
    authority_content: str
    visible_model: dict[str, object]
    semantic_hashes: dict[str, str]


def published_service_scope_options(package: PublishedTemplatePackage) -> tuple[
    tuple[tuple[str, str], ...], tuple[str, ...]
]:
    """Project the immutable Published package into its enabled UI scope authority."""
    contract_type = contract_domain.normalize_contract_type(package.contract_type)
    snapshot = dict(package.snapshot or {})
    if snapshot.get("template_type") != contract_type:
        raise PublishedTemplateIntegrityError("Published模板类型不匹配。")
    allowed = set(contract_domain.allowed_service_ids(contract_type))
    options: list[tuple[str, str]] = []
    defaults: list[str] = []
    seen: set[str] = set()
    for item in snapshot.get("services") or []:
        if not item.get("enabled", True):
            continue
        service_id = str(item.get("service_id") or "").strip()
        label = str(item.get("label") or "").strip()
        if not service_id or service_id not in allowed or service_id in seen or not label:
            raise PublishedTemplateIntegrityError("Published模板服务目录与合同类型不匹配。")
        seen.add(service_id)
        options.append((service_id, label))
        if item.get("default_selected"):
            defaults.append(service_id)
    if not options:
        raise PublishedTemplateIntegrityError("Published模板服务目录与合同类型不匹配。")
    return tuple(options), tuple(defaults)


def load_published_template(conn, contract_type: str) -> PublishedTemplatePackage:
    try:
        normalized = contract_domain.normalize_contract_type(contract_type)
    except contract_domain.ContractDomainError as exc:
        raise PublishedTemplateUnavailable("合同类型不存在。") from exc
    identity = conn.execute(
        "SELECT * FROM contract_template_identities WHERE template_type=?", (normalized,)
    ).fetchone()
    if identity is None or identity["current_published_version_id"] is None:
        raise PublishedTemplateUnavailable("当前合同类型没有Published模板。")
    version = conn.execute(
        "SELECT * FROM contract_template_versions WHERE id=? AND identity_id=?",
        (identity["current_published_version_id"], identity["id"]),
    ).fetchone()
    if version is None:
        raise PublishedTemplateUnavailable("Published模板指针无效。")
    if (
        version["status"] != "Published" or int(version["immutable"] or 0) != 1
        or version["published_by"] is None or not str(version["published_at"] or "").strip()
    ):
        raise PublishedTemplateIntegrityError("当前模板不是不可变Published版本。")
    try:
        snapshot = json.loads(version["snapshot_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise PublishedTemplateIntegrityError("Published模板快照无效。") from exc
    if not isinstance(snapshot, dict):
        raise PublishedTemplateIntegrityError("Published模板快照无效。")
    _validate_snapshot(snapshot, normalized)
    content_hash = _hash(snapshot)
    if content_hash != version["content_hash"]:
        raise PublishedTemplateIntegrityError("Published模板内容hash不匹配。")
    block_rows = conn.execute(
        "SELECT block_key,title,body,sort_order,enabled,fixed_layout,required,legal_kind,required_variables_json "
        "FROM contract_template_clause_blocks WHERE version_id=? ORDER BY sort_order",
        (version["id"],),
    ).fetchall()
    service_rows = conn.execute(
        "SELECT service_id,label,sort_order,enabled,default_selected "
        "FROM contract_template_service_items WHERE version_id=? ORDER BY sort_order",
        (version["id"],),
    ).fetchall()
    if len(block_rows) != len(snapshot.get("blocks") or []) or len(service_rows) != len(snapshot.get("services") or []):
        raise PublishedTemplateIntegrityError("Published模板登记结构不一致。")
    for order, (row, block) in enumerate(zip(block_rows, snapshot["blocks"]), 1):
        if (
            row["block_key"] != block.get("key") or row["title"] != block.get("title")
            or row["body"] != block.get("body") or int(row["sort_order"]) != order
            or bool(row["enabled"]) != bool(block.get("enabled"))
            or bool(row["fixed_layout"]) != bool(block.get("fixed_layout"))
            or bool(row["required"]) != bool(block.get("required"))
            or str(row["legal_kind"] or "") != str(block.get("legal_kind") or "")
            or json.loads(row["required_variables_json"] or "[]") != list(block.get("required_variables") or [])
        ):
            raise PublishedTemplateIntegrityError("Published模板条款区块不一致。")
    for order, (row, item) in enumerate(zip(service_rows, snapshot["services"]), 1):
        if (
            row["service_id"] != item.get("service_id") or row["label"] != item.get("label")
            or int(row["sort_order"]) != order or bool(row["enabled"]) != bool(item.get("enabled"))
            or bool(row["default_selected"]) != bool(item.get("default_selected"))
        ):
            raise PublishedTemplateIntegrityError("Published模板服务目录不一致。")
    return PublishedTemplatePackage(
        identity_id=int(identity["id"]), version_id=int(version["id"]),
        version_number=int(version["version_number"]), contract_type=normalized,
        content_hash=content_hash, snapshot_hash=_hash(snapshot),
        snapshot=copy.deepcopy(snapshot),
    )


def _actor_permissions(actor: Mapping[str, object]) -> set[str]:
    raw = actor.get("actor_permissions") or actor.get("permissions") or []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = [raw]
    return {str(value) for value in raw}


def _approval(
    value: Mapping[str, object] | None, actor: Mapping[str, object], label: str, *,
    permission: str | None = None,
) -> dict[str, object] | None:
    if value is None:
        return None
    approval = copy.deepcopy(dict(value))
    authorized = str(actor.get("role_name") or "") == "Admin"
    if permission:
        authorized = authorized or permission in _actor_permissions(actor) or "*" in _actor_permissions(actor)
    if not authorized:
        raise PublishedTemplateIntegrityError(f"只有Admin可以批准{label}。")
    for field in ("reason", "approved_by", "operator_id", "approved_at"):
        if not str(approval.get(field) or "").strip():
            raise PublishedTemplateIntegrityError(f"{label}审批信息不完整。")
    if int(approval["approved_by"]) != int(actor.get("id") or 0):
        raise PublishedTemplateIntegrityError(f"{label}批准人与当前Admin不一致。")
    if int(approval["operator_id"]) != int(actor.get("id") or 0):
        raise PublishedTemplateIntegrityError(f"{label}操作者与当前Admin不一致。")
    return approval


TRANSFER_FEE_RULE_VERSION = "transfer-source-fee-v1"


def _transfer_fee_provenance(source, student_id):
    if not isinstance(source, Mapping):
        raise PublishedTemplateIntegrityError("Transfer来源尚未核验；暂停生成及签署。")
    fields = (
        "authority_id", "transfer_contract_id", "student_id",
        "source_classification", "rule_version",
    )
    result = {key: source.get(key) for key in fields}
    for key in ("authority_id", "transfer_contract_id"):
        if type(result[key]) is not int or result[key] <= 0:
            raise PublishedTemplateIntegrityError("Transfer来源必须关联精确合同及核验记录。")
    if (
        not isinstance(result["student_id"], str)
        or not result["student_id"].strip()
        or result["student_id"] != student_id
    ):
        raise PublishedTemplateIntegrityError("Transfer来源与合同学生不一致。")
    if not isinstance(result["rule_version"], str) or not result["rule_version"].strip():
        raise PublishedTemplateIntegrityError("Transfer来源规则版本缺失。")
    fees = {"aaie_existing": "1200.00", "new_transfer": "2400.00"}
    if (
        not isinstance(result["source_classification"], str)
        or result["source_classification"] not in fees
    ):
        raise PublishedTemplateIntegrityError("Transfer来源缺失或冲突；暂停收费。")
    result.update(
        fee_rule_version=TRANSFER_FEE_RULE_VERSION,
        standard_service_fee_ex_gst=fees[result["source_classification"]],
    )
    return result


def validate_transfer_fee_authority(bound: Mapping[str, object]) -> None:
    if bound.get("contract_type") != "TransferApplicationContract":
        if bound.get("transfer_fee_authority") is not None:
            raise PublishedTemplateIntegrityError("非Transfer合同不能使用Transfer收费来源。")
        return
    _verify_bound_snapshot(bound)
    source = bound.get("transfer_fee_authority")
    expected = _transfer_fee_provenance(
        source, (bound.get("student_snapshot") or {}).get("student_id")
    )
    if source != expected:
        raise PublishedTemplateIntegrityError("Transfer冻结来源或收费规则不一致。")
    commercial = bound.get("commercial") or {}
    standard = Decimal(expected["standard_service_fee_ex_gst"])
    discount = _money(commercial.get("discount_amount"), "折扣")
    if discount < 0 or discount >= standard:
        raise PublishedTemplateIntegrityError("Transfer冻结折扣无效。")
    net = standard - discount
    treatment = (
        "GST_FREE" if commercial.get("gst_free_approval")
        else "EXCLUSIVE" if commercial.get("payment_method") == "澳洲公户"
        else "NOT_APPLICABLE"
    )
    if treatment == "GST_FREE" and commercial.get("payment_method") != "澳洲公户":
        raise PublishedTemplateIntegrityError("Transfer GST-free付款方式不一致。")
    gst = (
        (net * Decimal("0.10")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if treatment == "EXCLUSIVE" else Decimal("0.00")
    )
    values = {
        "standard_service_fee": standard,
        "standard_service_fee_ex_gst": standard,
        "service_fee": standard,
        "discount_amount_ex_gst": discount,
        "net_service_fee_ex_gst": net,
        "gst_rate": Decimal("0.10"),
        "gst_amount": gst,
        "final_fee_amount": net + gst,
        "final_total": net + gst,
    }
    for current in (
        commercial,
        (bound.get("template_snapshot") or {}).get("commercial") or {},
    ):
        if current.get("gst_treatment") != treatment or any(
            current.get(key) != _money_text(value) for key, value in values.items()
        ):
            raise PublishedTemplateIntegrityError("Transfer冻结费用与来源或GST不一致。")
    instance = bound.get("contract_instance") or {}
    if any(
        instance.get(key) != _money_text(value)
        for key, value in {
            "consulting_and_visa_service_fee": standard,
            "discount_amount": discount,
            "final_fee_amount": net + gst,
        }.items()
    ):
        raise PublishedTemplateIntegrityError("Transfer合同金额与冻结费用不一致。")


def bind_new_contract(
    package: PublishedTemplatePackage, payload: Mapping[str, object], *,
    actor: Mapping[str, object], discount_approval: Mapping[str, object] | None = None,
    payment_plan_approval: Mapping[str, object] | None = None,
    gst_free_approval: Mapping[str, object] | None = None,
    trusted_transfer_source: Mapping[str, object] | None = None,
    actual_paid_total: object | None = None,
    generated_at: str | None = None,
) -> dict[str, object]:
    contract_type = contract_domain.normalize_contract_type(payload.get("contract_type"))
    if contract_type != package.contract_type:
        raise PublishedTemplateIntegrityError("合同类型与Published模板不匹配。")
    _validate_snapshot(package.snapshot, package.contract_type)
    raw_services = payload.get("service_scope_ids")
    if not isinstance(raw_services, list):
        try:
            raw_services = json.loads(str(payload.get("service_scope_json") or "[]"))
        except json.JSONDecodeError as exc:
            raise PublishedTemplateIntegrityError("服务范围结构无效。") from exc
    selected = list(dict.fromkeys(str(value) for value in raw_services))
    if len(selected) != len(raw_services):
        raise PublishedTemplateIntegrityError("服务范围包含重复项目。")
    allowed = {
        str(item.get("service_id") or "") for item in package.snapshot.get("services") or []
        if item.get("enabled", True)
    }
    if not selected or any(service_id not in allowed for service_id in selected):
        raise PublishedTemplateIntegrityError("服务范围与Published模板不匹配。")
    signer = copy.deepcopy(dict(payload.get("signer") or {}))
    if any(not str(signer.get(field) or "").strip() for field in ("source", "name", "relationship", "phone", "email")):
        raise PublishedTemplateIntegrityError("合同签署人资料不完整。")
    expected_relation = {"father": "父亲", "mother": "母亲"}.get(str(signer.get("source") or ""))
    if expected_relation is None or signer.get("relationship") != expected_relation:
        raise PublishedTemplateIntegrityError("合同签署人与父母来源不一致。")
    student_snapshot = copy.deepcopy(dict(payload.get("student_snapshot") or {}))
    if not str(student_snapshot.get("student_full_name") or "").strip() or not str(
        student_snapshot.get("student_id") or ""
    ).strip():
        raise PublishedTemplateIntegrityError("学生冻结事实不完整。")
    for field, message in (
        ("current_year_level", "缺少当前年级"),
        ("applying_year_level", "缺少申请年级"),
        ("preferred_intake_year", "缺少计划入读年份"),
    ):
        if not str(student_snapshot.get(field) or "").strip():
            raise PublishedTemplateIntegrityError(message)
    commercial_policy = copy.deepcopy(dict(package.snapshot.get("commercial") or {}))
    if "company_authorised_person" in commercial_policy:
        company_authorised_person = str(
            commercial_policy.get("company_authorised_person") or ""
        ).strip()
    else:
        company_authorised_person = str(
            payload.get("company_authorised_person") or ""
        ).strip()
    if not company_authorised_person:
        raise PublishedTemplateIntegrityError("甲方授权代表不能为空。")
    standard = _money(
        commercial_policy.get("standard_service_fee_ex_gst"), "标准服务费（不含GST）"
    )
    if standard != Decimal("2400.00"):
        raise PublishedTemplateIntegrityError("标准服务费必须为AUD 2400.00。")
    transfer_fee_authority = None
    if package.contract_type == "TransferApplicationContract":
        transfer_fee_authority = _transfer_fee_provenance(
            trusted_transfer_source, student_snapshot.get("student_id")
        )
        standard = Decimal(transfer_fee_authority["standard_service_fee_ex_gst"])
    elif trusted_transfer_source is not None:
        raise PublishedTemplateIntegrityError("非Transfer合同不能使用Transfer收费来源。")
    payment_methods = json.loads(str(commercial_policy.get("payment_methods_json") or "[]"))
    payment_method = str(payload.get("payment_method") or "").strip()
    if payment_method not in payment_methods:
        raise PublishedTemplateIntegrityError("付款方式不在Published允许目录中。")
    discount = _approval(discount_approval, actor, "合同折扣")
    discount_amount = Decimal("0.00")
    if discount:
        discount_amount = _money(discount.get("discount_amount"), "折扣金额")
        if discount_amount <= 0 or discount_amount >= standard:
            raise PublishedTemplateIntegrityError("折扣金额无效。")
    net_ex_gst = standard - discount_amount
    gst_free = _approval(
        gst_free_approval, actor, "GST-free例外",
        permission="contracts.gst_free.approve",
    )
    is_australian_public_account = payment_method == "澳洲公户"
    if gst_free and not is_australian_public_account:
        raise PublishedTemplateIntegrityError("只有澳洲公户付款方式可批准GST-free例外。")
    gst_treatment = (
        "GST_FREE" if gst_free
        else "EXCLUSIVE" if is_australian_public_account
        else "NOT_APPLICABLE"
    )
    gst_rate = Decimal("0.10")
    gst_amount = (
        Decimal("0.00") if gst_treatment != "EXCLUSIVE"
        else (net_ex_gst * gst_rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    )
    final = net_ex_gst + gst_amount
    plan = _approval(payment_plan_approval, actor, "特殊分期付款计划")
    if plan:
        if plan.get("mode") != "installments" or not isinstance(plan.get("installments"), list):
            raise PublishedTemplateIntegrityError("特殊分期付款计划无效。")
        if not plan["installments"]:
            raise PublishedTemplateIntegrityError("特殊分期付款计划无效。")
        for item in plan["installments"]:
            due_date = str(item.get("due_date") or "").strip()
            try:
                date.fromisoformat(due_date)
            except ValueError as exc:
                raise PublishedTemplateIntegrityError("特殊分期必须提供有效到期日。") from exc
        total = sum(
            (_money(item.get("amount"), "分期金额") for item in plan["installments"]),
            Decimal("0.00"),
        )
        if total != final:
            raise PublishedTemplateIntegrityError("分期付款总额与最终费用不一致。")
        payment_plan = plan
        payment_display = "Admin批准的特殊分期付款计划。"
    else:
        payment_plan = {
            "mode": "single_payment",
            "due_event": "contract_signing",
            "must_be_paid_before": "formal_service_start",
        }
        payment_display = str(commercial_policy.get("payment_terms") or "")
    commercial = copy.deepcopy(commercial_policy)
    commercial.update({
        "standard_service_fee": _money_text(standard),
        "standard_service_fee_ex_gst": _money_text(standard),
        "service_fee": _money_text(standard),
        "discount_amount": _money_text(discount_amount),
        "discount_amount_ex_gst": _money_text(discount_amount),
        "net_service_fee_ex_gst": _money_text(net_ex_gst),
        "gst_treatment": gst_treatment,
        "gst_rate": _money_text(gst_rate),
        "gst_amount": _money_text(gst_amount),
        "final_fee_amount": _money_text(final),
        "final_total": _money_text(final),
        "discount_approval": discount,
        "gst_free_approval": gst_free,
        "payment_method": payment_method,
        "payment_plan": payment_plan,
        "payment_plan_display": payment_display,
        "gst_display": {
            "EXCLUSIVE": "标准费不含GST；按折后税前金额计10% GST",
            "GST_FREE": "GST-free（已批准）",
            "NOT_APPLICABLE": "此付款方式不收取GST",
        }[gst_treatment],
    })
    paid = final if actual_paid_total is None else _money(
        actual_paid_total, "实际已支付总额"
    )
    if package.contract_type == contract_domain.NEW_INTERNATIONAL_STUDENT_CONTRACT:
        retained_ex_gst = _money(
            commercial_policy.get("visa_refusal_retained_ex_gst"), "拒签保留金额"
        )
        retained_gst = (
            Decimal("0.00") if gst_treatment != "EXCLUSIVE"
            else (retained_ex_gst * gst_rate).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        )
        retained_total = retained_ex_gst + retained_gst
        commercial["visa_refusal_refund"] = {
            "retained_ex_gst": _money_text(retained_ex_gst),
            "gst_adjustment": _money_text(retained_gst),
            "retained_total": _money_text(retained_total),
            "actual_paid_total": _money_text(paid),
            "actual_refund_amount": _money_text(max(paid - retained_total, Decimal("0.00"))),
            "rule_source": str(commercial_policy.get("visa_refusal_rule_source") or ""),
            "template_version_id": package.version_id,
            "template_content_hash": package.content_hash,
        }
    instance = copy.deepcopy(dict(payload))
    for key in (
        "consulting_and_visa_service_fee", "standard_service_fee",
        "standard_service_fee_ex_gst", "service_fee", "currency",
        "discount_amount", "discount_amount_ex_gst", "discount_approval",
        "net_service_fee_ex_gst", "final_fee_amount", "final_total",
        "gst_treatment", "gst_amount", "gst_rate", "gst_free_approval",
        "actual_paid_total", "payment_plan", "payment_plan_approval",
        "payment_terms", "refund_policy", "visa_refusal_refund",
        "refusal_deduction_amount", "refusal_refund_amount",
        "service_fee_currency", "student_snapshot", "signer",
        "trusted_transfer_source", "transfer_fee_authority",
    ):
        instance.pop(key, None)
    instance.update({
        "consulting_and_visa_service_fee": _money_text(standard),
        "discount_amount": _money_text(discount_amount),
        "final_fee_amount": _money_text(final),
        "service_fee_currency": payment_method,
        "payment_method": payment_method,
        "student_full_name": student_snapshot.get("student_full_name", ""),
        "student_date_of_birth": student_snapshot.get("date_of_birth", ""),
        "student_id_or_passport_number": student_snapshot.get("student_id", ""),
        "student_home_address": student_snapshot.get("home_address", ""),
        "student_current_year_level": student_snapshot.get("current_year_level", ""),
        "student_applying_year_level": student_snapshot.get("applying_year_level", ""),
        "student_planned_entry_year": student_snapshot.get("preferred_intake_year", ""),
        "student_planned_entry_term": student_snapshot.get("preferred_intake_term", ""),
        "intake_year": student_snapshot.get("preferred_intake_year", ""),
        "intake_term": student_snapshot.get("preferred_intake_term", ""),
        "company_authorised_person": company_authorised_person,
        "parent_signer_name": signer.get("name", ""),
        "parent_signer_relationship": signer.get("relationship", ""),
        "parent_phone": signer.get("phone", ""),
        "parent_email": signer.get("email", ""),
    })
    template_snapshot = copy.deepcopy(package.snapshot)
    template_snapshot["commercial"] = commercial
    stamp = generated_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    master_snapshot = contract_template_renderer.is_master_snapshot(package.snapshot)
    renderer_schema = copy.deepcopy(
        RENDERER_SCHEMA if master_snapshot else LEGACY_RENDERER_SCHEMA_V2
    )
    renderer_schema_hash = (
        RENDERER_SCHEMA_HASH if master_snapshot else LEGACY_RENDERER_SCHEMA_V2_HASH
    )
    display_bindings = contract_display_bindings.build_frozen_display_bindings(
        package.contract_type,
        student_snapshot=student_snapshot,
        signer=signer,
        commercial=commercial,
        contract_instance=instance,
    )
    binding_issues = contract_display_bindings.required_value_issues(display_bindings)
    if binding_issues:
        raise PublishedTemplateIntegrityError(binding_issues[0]["message"])
    bound = {
        "snapshot_version": 1,
        "generation_authority": AUTHORITY_ID,
        "renderer_schema": renderer_schema,
        "renderer_schema_hash": renderer_schema_hash,
        "template_identity_id": package.identity_id,
        "template_version_id": package.version_id,
        "template_version": package.version_number,
        "template_content_hash": package.content_hash,
        "template_snapshot_hash": package.snapshot_hash,
        "contract_type": package.contract_type,
        "published_template_snapshot": copy.deepcopy(package.snapshot),
        "template_snapshot": template_snapshot,
        "clause_blocks": copy.deepcopy(package.snapshot.get("blocks") or []),
        "service_items": copy.deepcopy(package.snapshot.get("services") or []),
        "selected_service_ids": selected,
        "commercial": commercial,
        "display_bindings": display_bindings,
        "company_configuration": {
            key: commercial.get(key, "") for key in
            ("company_name", "company_abn", "company_address", "company_contact")
        } | {"company_authorised_person": company_authorised_person},
        "student_snapshot": student_snapshot,
        "signer": signer,
        "contract_instance": instance,
        "generation_request_id": str(payload.get("generation_request_id") or "").strip(),
        "request_fingerprint": _hash({
            "student": student_snapshot, "signer": signer, "services": selected,
            "contract_type": package.contract_type, "payment_method": payment_method,
            "company_authorised_person": company_authorised_person,
            "discount": discount, "gst_free_approval": gst_free,
            "transfer_fee_authority": transfer_fee_authority,
            "actual_paid_total": _money_text(paid), "payment_plan": payment_plan,
            "contract_date": instance.get("contract_date", ""),
            "other_service_scope_text": instance.get("other_service_scope_text", ""),
            "special_terms": instance.get("special_terms", ""),
            "parent_address": instance.get("parent_address", ""),
            "notes": instance.get("notes", ""),
        }),
        "generated_at": stamp,
    }
    if transfer_fee_authority is not None:
        bound["transfer_fee_authority"] = transfer_fee_authority
    if not bound["generation_request_id"]:
        raise PublishedTemplateIntegrityError("生成请求ID不能为空。")
    bound["bound_snapshot_hash"] = _hash(bound)
    return bound


def rebind_existing_draft(
    existing_bound: Mapping[str, object], payload: Mapping[str, object], *,
    request_id: str, generated_at: str | None = None,
) -> dict[str, object]:
    if existing_bound.get("generation_authority") != AUTHORITY_ID:
        raise PublishedTemplateIntegrityError("现有合同不是Published V2冻结快照。")
    _verify_bound_snapshot(existing_bound)
    validate_transfer_fee_authority(existing_bound)
    _renderer_mode(existing_bound)
    rebound = copy.deepcopy(dict(existing_bound))
    selected = payload.get("service_scope_ids")
    if not isinstance(selected, list):
        raise PublishedTemplateIntegrityError("服务范围结构无效。")
    allowed = {
        str(item.get("service_id") or "") for item in rebound.get("service_items") or []
        if item.get("enabled", True)
    }
    selected = list(dict.fromkeys(str(value) for value in selected))
    if not selected or any(item not in allowed for item in selected):
        raise PublishedTemplateIntegrityError("服务范围与冻结模板不匹配。")
    rebound["selected_service_ids"] = selected
    instance = copy.deepcopy(dict(rebound.get("contract_instance") or {}))
    for key in ("other_service_scope_text", "special_terms"):
        if key in payload:
            instance[key] = str(payload.get(key) or "")
    rebound["contract_instance"] = instance
    rebound["generation_request_id"] = str(request_id or "").strip()
    if not rebound["generation_request_id"]:
        raise PublishedTemplateIntegrityError("重建请求ID不能为空。")
    rebound["generated_at"] = generated_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    rebound["request_fingerprint"] = _hash({
        "original": existing_bound.get("bound_snapshot_hash"),
        "services": selected,
        "other_service_scope_text": instance.get("other_service_scope_text", ""),
        "special_terms": instance.get("special_terms", ""),
    })
    rebound.pop("bound_snapshot_hash", None)
    rebound["bound_snapshot_hash"] = _hash(rebound)
    return rebound


def render_bound_contract(bound: Mapping[str, object]) -> RenderedBoundContract:
    if bound.get("generation_authority") != AUTHORITY_ID:
        raise PublishedTemplateIntegrityError("合同快照没有绑定Published V2权威。")
    _verify_bound_snapshot(bound)
    if bound.get("transfer_fee_authority") is not None:
        validate_transfer_fee_authority(bound)
    renderer_mode = _renderer_mode(bound)
    published = copy.deepcopy(dict(bound.get("published_template_snapshot") or {}))
    if _hash(published) != bound.get("template_content_hash"):
        raise PublishedTemplateIntegrityError("冻结Published模板hash不匹配。")
    snapshot = copy.deepcopy(dict(bound.get("template_snapshot") or {}))
    if snapshot.get("template_type") != bound.get("contract_type"):
        raise PublishedTemplateIntegrityError("冻结模板类型不匹配。")
    try:
        display_bindings = None
        if renderer_mode == "master_v4":
            display_bindings = copy.deepcopy(dict(bound.get("display_bindings") or {}))
            issues = contract_display_bindings.required_value_issues(display_bindings)
            if issues:
                raise PublishedTemplateIntegrityError(issues[0]["message"])
        artifacts = render_published_contract_artifacts(
            snapshot,
            student_name=str((bound.get("student_snapshot") or {}).get("student_full_name") or ""),
            signer=dict(bound.get("signer") or {}),
            selected_services=list(bound.get("selected_service_ids") or []),
            contract_instance=dict(bound.get("contract_instance") or {}),
            include_p0_mandatory_fields=renderer_mode in {"master_v4", "master_v3", "legacy_v2"},
            use_master_layout=renderer_mode in {"master_v4", "master_v3"},
            display_bindings=display_bindings,
        )
    except TemplateRuleError as exc:
        raise PublishedTemplateIntegrityError(str(exc)) from exc
    semantic_hash = str(artifacts["semantic_hash"])
    return RenderedBoundContract(
        template_version_id=int(bound["template_version_id"]),
        docx_bytes=bytes(artifacts["docx_bytes"]),
        pdf_bytes=bytes(artifacts["pdf_bytes"]),
        signing_preview_bytes=bytes(artifacts["signing_preview_bytes"]),
        authority_content=str(artifacts["authority_content"]),
        visible_model=copy.deepcopy(dict(artifacts["model"])),
        semantic_hashes={
            "docx": semantic_hash, "pdf": semantic_hash,
            "backend_preview": semantic_hash, "signing_preview": semantic_hash,
        },
    )


def is_published_v2_snapshot(snapshot: Mapping[str, object]) -> bool:
    return snapshot.get("generation_authority") == AUTHORITY_ID
