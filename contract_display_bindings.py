"""Canonical, frozen display bindings for contract renderers.

This module is deliberately pure.  It receives already-authoritative CRM
snapshots and returns the only field/value object that contract information
renderers may consume.  It never queries students, applications, templates,
or storage.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Mapping

import contract_domain


BINDING_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class DisplayFieldDefinition:
    key: str
    label: str
    group: str
    source: str


FIELD_DEFINITIONS = (
    DisplayFieldDefinition("student_name", "学生姓名", "student", "students.passport_name"),
    DisplayFieldDefinition("date_of_birth", "学生出生日期", "student", "students.date_of_birth"),
    DisplayFieldDefinition("student_id", "学生护照号 / 学生 ID", "student", "students.student_id"),
    DisplayFieldDefinition("current_grade", "当前年级", "student", "students.current_year_level"),
    DisplayFieldDefinition("application_grade", "申请年级", "student", "students.applying_year_level"),
    DisplayFieldDefinition("planned_entry_year", "计划入读年份", "student", "students.preferred_intake_year"),
    DisplayFieldDefinition("planned_entry_term", "计划入读学期", "student", "students.preferred_intake_term"),
    DisplayFieldDefinition("guardian_name", "乙方签署人", "guardian", "contract.signer.name"),
    DisplayFieldDefinition("guardian_relationship", "与学生关系", "guardian", "contract.signer.relationship"),
    DisplayFieldDefinition("guardian_phone", "乙方电话", "guardian", "contract.signer.phone"),
    DisplayFieldDefinition("guardian_email", "乙方邮箱", "guardian", "contract.signer.email"),
    DisplayFieldDefinition("guardian_address", "乙方地址", "guardian", "contract.parent_address"),
    DisplayFieldDefinition("company_legal_entity", "甲方法律主体名称", "company", "published_template.commercial.company_name"),
    DisplayFieldDefinition("company_abn_acn", "甲方 ABN / ACN", "company", "published_template.commercial.company_abn"),
    DisplayFieldDefinition("company_address", "甲方地址", "company", "published_template.commercial.company_address"),
    DisplayFieldDefinition("company_phone", "甲方电话", "company", "published_template.commercial.company_contact"),
    DisplayFieldDefinition("company_email", "甲方邮箱", "company", "published_template.commercial.company_contact"),
    DisplayFieldDefinition("authorised_representative", "甲方授权代表 / 顾问", "company", "published_template.commercial.company_authorised_person"),
    DisplayFieldDefinition("contract_number", "合同编号", "contract", "contract.contract_uid"),
    DisplayFieldDefinition("contract_date", "合同日期", "contract", "contract.contract_date"),
    DisplayFieldDefinition("contract_type", "合同类型", "contract", "contract.contract_type"),
    DisplayFieldDefinition("payment_method", "付款方式", "pricing", "contract.commercial.payment_method"),
    DisplayFieldDefinition("standard_fee", "标准服务费（不含GST）", "pricing", "contract.commercial.standard_service_fee_ex_gst"),
    DisplayFieldDefinition("discount", "批准折扣（不含GST）", "pricing", "contract.commercial.discount_amount_ex_gst"),
    DisplayFieldDefinition("discounted_fee", "优惠后服务费（不含 GST）", "pricing", "contract.commercial.net_service_fee_ex_gst"),
    DisplayFieldDefinition("gst", "GST金额", "pricing", "contract.commercial.gst_amount"),
    DisplayFieldDefinition("final_total", "合同总额", "pricing", "contract.commercial.final_total"),
)

FIELD_BY_KEY = {field.key: field for field in FIELD_DEFINITIONS}

_COMMON_PARTY_COMPANY_CONTRACT_PRICING_REQUIRED = (
    "guardian_name", "guardian_relationship", "guardian_phone", "guardian_email",
    "company_legal_entity", "company_abn_acn", "company_address",
    "company_phone", "company_email", "authorised_representative",
    "contract_number", "contract_date", "payment_method",
    "standard_fee", "discount", "discounted_fee", "gst", "final_total",
)
_STUDY_ABROAD_REQUIRED = (
    "student_name", "date_of_birth", "student_id",
    "current_grade", "application_grade", "planned_entry_year",
    *_COMMON_PARTY_COMPANY_CONTRACT_PRICING_REQUIRED,
)
_TRANSFER_REQUIRED = (
    "student_name", "date_of_birth", "student_id",
    "current_grade", "application_grade", "planned_entry_year",
    *_COMMON_PARTY_COMPANY_CONTRACT_PRICING_REQUIRED,
)
_LOCAL_STUDENT_REQUIRED = (
    "student_name", "date_of_birth", "student_id",
    "current_grade", "application_grade", "planned_entry_year",
    *_COMMON_PARTY_COMPANY_CONTRACT_PRICING_REQUIRED,
)

# Profiles are intentionally independent declarations, even where today's
# business requirements coincide.  A future Transfer or Local change cannot
# silently alter the Study Abroad contract's mandatory display contract.
REQUIRED_FIELDS_BY_CONTRACT_TYPE = {
    contract_domain.NEW_INTERNATIONAL_STUDENT_CONTRACT: _STUDY_ABROAD_REQUIRED,
    contract_domain.TRANSFER_APPLICATION_CONTRACT: _TRANSFER_REQUIRED,
    contract_domain.LOCAL_STUDENT_APPLICATION_CONTRACT: _LOCAL_STUDENT_REQUIRED,
}


def _text(value: object) -> str:
    return str(value or "").strip()


def _company_contact(commercial: Mapping[str, object]) -> tuple[str, str]:
    parts = [
        part.strip()
        for part in _text(commercial.get("company_contact")).split("/")
        if part.strip()
    ]
    if len(parts) > 1:
        return parts[0], parts[-1]
    if parts and "@" in parts[0]:
        return "", parts[0]
    return (parts[0] if parts else ""), ""


def required_fields(contract_type: object) -> tuple[str, ...]:
    normalized = contract_domain.normalize_contract_type(contract_type)
    return REQUIRED_FIELDS_BY_CONTRACT_TYPE[normalized]


def mandatory_profile(contract_type: object) -> dict[str, object]:
    normalized = contract_domain.normalize_contract_type(contract_type)
    return {
        "schema_version": BINDING_SCHEMA_VERSION,
        "contract_type": normalized,
        "required_fields": list(required_fields(normalized)),
    }


def schema_hash() -> str:
    payload = {
        "schema_version": BINDING_SCHEMA_VERSION,
        "fields": [field.__dict__ for field in FIELD_DEFINITIONS],
        "profiles": {
            key: list(value)
            for key, value in sorted(REQUIRED_FIELDS_BY_CONTRACT_TYPE.items())
        },
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def mandatory_profile_issues(snapshot: Mapping[str, object]) -> list[dict[str, str]]:
    try:
        expected = mandatory_profile(snapshot.get("template_type"))
    except contract_domain.ContractDomainError:
        return [{
            "code": "mandatory_display_binding_profile_invalid",
            "message": "合同 mandatory display binding profile 类型无效",
        }]
    actual = snapshot.get("mandatory_display_bindings")
    if not isinstance(actual, Mapping):
        return [{
            "code": "mandatory_display_binding_profile_missing",
            "message": "合同模板缺少 mandatory display binding profile",
        }]
    if dict(actual) != expected:
        return [{
            "code": "mandatory_display_binding_profile_mismatch",
            "message": "合同模板 mandatory display binding profile 与合同类型不一致",
        }]
    return []


def build_frozen_display_bindings(
    contract_type: object,
    *,
    student_snapshot: Mapping[str, object],
    signer: Mapping[str, object],
    commercial: Mapping[str, object],
    contract_instance: Mapping[str, object],
) -> dict[str, object]:
    """Freeze all display values without consulting live business data."""
    normalized = contract_domain.normalize_contract_type(contract_type)
    phone, email = _company_contact(commercial)
    values = {
        "student_name": student_snapshot.get("student_full_name") or student_snapshot.get("student_name"),
        "date_of_birth": student_snapshot.get("date_of_birth"),
        "student_id": student_snapshot.get("student_id"),
        "current_grade": student_snapshot.get("current_year_level"),
        "application_grade": student_snapshot.get("applying_year_level"),
        "planned_entry_year": student_snapshot.get("preferred_intake_year"),
        "planned_entry_term": student_snapshot.get("preferred_intake_term"),
        "guardian_name": signer.get("name"),
        "guardian_relationship": signer.get("relationship"),
        "guardian_phone": signer.get("phone"),
        "guardian_email": signer.get("email"),
        "guardian_address": contract_instance.get("parent_address") or student_snapshot.get("home_address"),
        "company_legal_entity": commercial.get("company_name"),
        "company_abn_acn": commercial.get("company_abn"),
        "company_address": commercial.get("company_address"),
        "company_phone": phone,
        "company_email": email,
        "authorised_representative": contract_instance.get("company_authorised_person") or commercial.get("company_authorised_person"),
        "contract_number": contract_instance.get("contract_uid"),
        "contract_date": contract_instance.get("contract_date"),
        "contract_type": normalized,
        "payment_method": commercial.get("payment_method"),
        "standard_fee": (
            commercial.get("standard_service_fee_ex_gst")
            or commercial.get("standard_service_fee")
            or commercial.get("service_fee")
        ),
        "discount": commercial.get("discount_amount_ex_gst") or commercial.get("discount_amount"),
        "discounted_fee": commercial.get("net_service_fee_ex_gst"),
        "gst": commercial.get("gst_amount"),
        "final_total": commercial.get("final_total") or commercial.get("final_fee_amount"),
    }
    fields = {
        field.key: {
            "label": field.label,
            "group": field.group,
            "source": field.source,
            "value": _text(values.get(field.key)),
        }
        for field in FIELD_DEFINITIONS
    }
    return {
        "schema_version": BINDING_SCHEMA_VERSION,
        "schema_hash": schema_hash(),
        "contract_type": normalized,
        "required_fields": list(required_fields(normalized)),
        "fields": fields,
    }


def required_value_issues(bindings: Mapping[str, object]) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    fields = bindings.get("fields")
    if not isinstance(fields, Mapping):
        return [{"code": "display_bindings_missing", "message": "合同 canonical display bindings 缺失"}]
    if bindings.get("schema_version") != BINDING_SCHEMA_VERSION or bindings.get("schema_hash") != schema_hash():
        issues.append({"code": "display_binding_schema_mismatch", "message": "合同 canonical display binding schema 不匹配"})
    try:
        expected = required_fields(bindings.get("contract_type"))
    except contract_domain.ContractDomainError:
        return issues + [{"code": "display_binding_contract_type_invalid", "message": "合同 binding 类型无效"}]
    if list(bindings.get("required_fields") or []) != list(expected):
        issues.append({"code": "display_binding_profile_mismatch", "message": "合同 mandatory display binding profile 不匹配"})
    exact_messages = {
        "current_grade": "缺少当前年级",
        "application_grade": "缺少申请年级",
        "planned_entry_year": "缺少计划入读年份",
    }
    for key in expected:
        field = fields.get(key)
        if not isinstance(field, Mapping) or not _text(field.get("value")):
            label = FIELD_BY_KEY[key].label
            issues.append({
                "code": f"mandatory_display_binding_{key}_missing",
                "message": exact_messages.get(key, f"缺少{label}"),
            })
    return issues


def binding_value(bindings: Mapping[str, object] | None, key: str, default: object = "") -> str:
    fields = (bindings or {}).get("fields")
    if not isinstance(fields, Mapping):
        return _text(default)
    field = fields.get(key)
    if not isinstance(field, Mapping):
        return _text(default)
    return _text(field.get("value"))


def binding_label(bindings: Mapping[str, object] | None, key: str) -> str:
    fields = (bindings or {}).get("fields")
    field = fields.get(key) if isinstance(fields, Mapping) else None
    if isinstance(field, Mapping) and _text(field.get("label")):
        return _text(field.get("label"))
    return FIELD_BY_KEY[key].label
