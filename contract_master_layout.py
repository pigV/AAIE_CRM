"""Locked AAIE contract master layout shared by three content profiles.

The module owns physical layout and protected anchors.  Published template
blocks supply content only; they cannot redefine pages, tables or signature
geometry.
"""

from __future__ import annotations

import copy
import base64
import gzip
import html
import io
import json
import re
import zipfile
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Mapping

from pypdf import PdfReader

import contract_domain
import contract_display_bindings
from student_doc import (
    DEFAULT_PARTY_A_ADDRESS,
    DEFAULT_PARTY_A_EMAIL,
    DEFAULT_PARTY_A_NAME,
    DEFAULT_PARTY_A_PHONE,
    build_study_abroad_contract_document,
    study_contract_cover_png_bytes,
)


MASTER_LAYOUT_ID = "AAIEContractMasterLayoutV1"
MASTER_SLOT_ORDER = (
    "fixed_parties",
    "fixed_service_scope",
    "profile_3",
    "profile_4",
    "profile_5",
    "profile_6",
    "profile_7",
    "legal_dispute",
    "profile_9",
    "special_terms",
)
EDITABLE_MASTER_SLOTS = MASTER_SLOT_ORDER[2:]
MASTER_SECTION_ANCHORS = (
    "section_1_parties",
    "section_2_services",
    "section_3_fees",
    "section_4_party_a",
    "section_5_party_b",
    "section_6_refund",
    "section_7_force_majeure",
    "section_8_dispute",
    "section_9_variation",
    "section_10_special",
    "section_11_signature",
)
MASTER_LAYOUT_POLICY = {
    "id": MASTER_LAYOUT_ID,
    "version": 1,
    "slot_order": list(MASTER_SLOT_ORDER),
    "section_anchors": list(MASTER_SECTION_ANCHORS),
    "protected_regions": [
        "premium_cover", "contract_identity", "section_order", "pricing_table",
        "signature_party_table", "signature_aaie", "signature_family", "stamp",
        "confirmation",
    ],
    "signature": {
        "page_policy": "reserved_final_draft_page",
        "aaie_column": "left",
        "family_column": "right",
        "stamp_column": "left_only",
    },
}

HEADINGS = {
    "fixed_parties": "一、合同双方",
    "fixed_service_scope": "二、服务项目及服务范围",
    "profile_3": "三、服务费用与第三方费用",
    "profile_4": "四、甲方权利义务",
    "profile_5": "五、乙方权利义务",
    "profile_6": "六、退费及违约责任",
    "profile_7": "七、不可抗力、结果说明及免责",
    "legal_dispute": "八、适用法律及争议解决",
    "profile_9": "九、合同补充、变更和生效",
    "special_terms": "十、特别条款",
}


class MasterLayoutError(ValueError):
    pass


def is_master_snapshot(snapshot: Mapping[str, object]) -> bool:
    return (snapshot.get("layout_policy") or {}).get("id") == MASTER_LAYOUT_ID


def master_block_parent(block_key: object) -> str | None:
    """Return the protected section slot that owns a content block."""
    key = str(block_key or "")
    if key in MASTER_SLOT_ORDER:
        return key
    parent, separator, child = key.partition("::")
    if separator and child and parent in EDITABLE_MASTER_SLOTS:
        return parent
    return None


def validate_master_layout_snapshot(snapshot: Mapping[str, object]) -> None:
    policy = snapshot.get("layout_policy")
    if policy != MASTER_LAYOUT_POLICY:
        raise MasterLayoutError("合同主版式策略已被修改。")
    blocks = list(snapshot.get("blocks") or [])
    keys = [str(block.get("key") or "") for block in blocks]
    core_keys = [key for key in keys if key in MASTER_SLOT_ORDER]
    if core_keys != list(MASTER_SLOT_ORDER) or len(set(keys)) != len(keys):
        raise MasterLayoutError("合同主版式条款锚点缺失、重复或顺序改变。")
    active_parent = None
    by_key = {str(block.get("key") or ""): block for block in blocks}
    for key in keys:
        if key in MASTER_SLOT_ORDER:
            active_parent = key
            continue
        parent = master_block_parent(key)
        if parent is None or parent != active_parent:
            raise MasterLayoutError("可编辑条款必须保留在所属主版式章节内。")
        block = by_key[key]
        if block.get("fixed_layout") or block.get("required"):
            raise MasterLayoutError("新增内容条款不得伪装为主版式锚点。")
        if block.get("enabled", True) and (
            not str(block.get("title") or "").strip()
            or not str(block.get("body") or "").strip()
        ):
            raise MasterLayoutError("已启用的可编辑条款标题和正文不得为空。")
    for key in MASTER_SLOT_ORDER:
        block = by_key[key]
        if not block.get("enabled", True) or not str(block.get("body") or "").strip():
            raise MasterLayoutError("合同主版式必要内容不能为空或停用。")
        title = str(block.get("title") or "").strip()
        if key in EDITABLE_MASTER_SLOTS:
            ordinal = HEADINGS[key].split("、", 1)[0] + "、"
            if not title.startswith(ordinal):
                raise MasterLayoutError("可编辑章节标题必须保留主版式序号。")
        elif title != HEADINGS[key]:
            raise MasterLayoutError("合同主版式标题或章节锚点已移动。")


def _money(value: object, default: str = "0.00") -> Decimal:
    try:
        return Decimal(str(value if value not in (None, "") else default)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    except (InvalidOperation, ValueError):
        return Decimal(default)


def _aud(value: Decimal | object) -> str:
    amount = value if isinstance(value, Decimal) else _money(value)
    return f"AUD {amount:.2f}"


def _pricing_rows(
    commercial: Mapping[str, object], instance: Mapping[str, object], config,
    display_bindings: Mapping[str, object] | None = None,
) -> list[tuple[str, str]]:
    binding_value = lambda key, fallback="": contract_display_bindings.binding_value(
        display_bindings, key, fallback
    )
    standard = _money(
        binding_value(
            "standard_fee",
            commercial.get("standard_service_fee_ex_gst")
            or commercial.get("standard_service_fee")
            or commercial.get("service_fee"),
        ),
        "2400.00",
    )
    discount = _money(
        binding_value(
            "discount",
            commercial.get("discount_amount_ex_gst")
            or commercial.get("discount_amount")
            or instance.get("discount_amount"),
        ),
        "0.00",
    )
    if discount > standard:
        discount = Decimal("0.00")
    net = _money(
        binding_value(
            "discounted_fee", commercial.get("net_service_fee_ex_gst")
        ),
        str(standard - discount),
    )
    method = binding_value(
        "payment_method",
        commercial.get("payment_method") or instance.get("payment_method") or "按合同约定",
    )
    treatment = str(commercial.get("gst_treatment") or "")
    australian_public = method in {"澳洲公户", "Australian public account"}
    if not treatment:
        treatment = "EXCLUSIVE" if australian_public else "NOT_APPLICABLE"
    gst = _money(
        binding_value("gst", commercial.get("gst_amount")),
        str((net * Decimal("0.10")).quantize(Decimal("0.01")) if treatment == "EXCLUSIVE" else Decimal("0.00")),
    )
    total = _money(
        binding_value(
            "final_total",
            commercial.get("final_total") or commercial.get("final_fee_amount"),
        ),
        str(net + gst),
    )
    gst_display = {
        "EXCLUSIVE": "按折后税前金额计 10% GST（仅澳洲公户）",
        "GST_FREE": "GST-free（已获批准）",
        "NOT_APPLICABLE": "该付款方式不收取 GST",
    }.get(treatment, str(commercial.get("gst_display") or treatment))
    rows = [
        ("费用名称", config.service_fee_label),
        ("服务类型", config.cover_service_summary),
        ("支付方式", method),
        ("标准服务费（不含 GST）", _aud(standard)),
    ]
    if discount:
        rows.extend([
            ("已批准优惠（不含 GST）", _aud(discount)),
            ("优惠后服务费（不含 GST）", _aud(net)),
        ])
    else:
        rows.append(("优惠后服务费（不含 GST）", _aud(net)))
    rows.extend([
        ("GST 处理", gst_display),
        ("GST 金额", _aud(gst)),
        ("最终应付总额", _aud(total)),
        ("付款计划", str(commercial.get("payment_plan_display") or commercial.get("payment_terms") or "合同签署时一次性支付，并在正式服务开始前付清。")),
        ("退款规则", str(commercial.get("refund_policy") or "按合同第六条约定处理。")),
    ])
    refusal = commercial.get("visa_refusal_refund")
    if isinstance(refusal, Mapping) and str(refusal.get("actual_refund_amount") or "").strip():
        rows.append(("签证拒签退款（按实际已支付额）", _aud(refusal.get("actual_refund_amount"))))
    return rows


def _paragraphs(value: object) -> list[dict[str, list[str]]]:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    return [
        {"lines": chunk.split("\n")}
        for chunk in re.split(r"\n[ \t]*\n", text)
        if chunk.strip()
    ]


def _table(title: str, key: str, rows) -> dict[str, object]:
    return {
        "kind": "table",
        "key": key,
        "title": title,
        "rows": [{"cells": [{"lines": [str(label)]}, {"lines": [str(value)]}]} for label, value in rows],
    }


def build_master_render_model(
    snapshot: Mapping[str, object], *, student_name: str,
    signer: Mapping[str, object], selected_services: list[str],
    contract_instance: Mapping[str, object] | None = None,
    display_bindings: Mapping[str, object] | None = None,
) -> dict[str, object]:
    validate_master_layout_snapshot(snapshot)
    instance = dict(contract_instance or {})
    commercial = dict(snapshot.get("commercial") or {})
    config = contract_domain.contract_type_config(str(snapshot.get("template_type") or ""))
    blocks = {str(item["key"]): dict(item) for item in snapshot["blocks"]}
    children: dict[str, list[dict[str, object]]] = {
        key: [] for key in EDITABLE_MASTER_SLOTS
    }
    for item in snapshot["blocks"]:
        key = str(item["key"])
        parent = master_block_parent(key)
        if key not in MASTER_SLOT_ORDER and parent and item.get("enabled", True):
            children[parent].append(dict(item))

    def slot_body(key: str) -> str:
        parts = [str(blocks[key]["body"])]
        parts.extend(
            f'{str(item["title"]).strip()}\n{str(item["body"]).strip()}'
            for item in children.get(key, [])
        )
        return "\n\n".join(value for value in parts if value.strip())
    labels = {
        str(item.get("service_id") or ""): str(item.get("label") or "")
        for item in snapshot.get("services") or [] if item.get("enabled", True)
    }
    services = [labels[value] for value in selected_services]
    company_contact = str(commercial.get("company_contact") or "")
    contact_parts = [value.strip() for value in company_contact.split("/") if value.strip()]
    if len(contact_parts) > 1:
        company_phone = contact_parts[0]
        company_email = contact_parts[-1]
    elif contact_parts and "@" in contact_parts[0]:
        company_phone = DEFAULT_PARTY_A_PHONE
        company_email = contact_parts[0]
    else:
        company_phone = contact_parts[0] if contact_parts else DEFAULT_PARTY_A_PHONE
        company_email = DEFAULT_PARTY_A_EMAIL
    if display_bindings:
        identity_rows = [
            (
                contract_display_bindings.binding_label(display_bindings, key),
                contract_display_bindings.binding_value(display_bindings, key),
            )
            for key in (
                "contract_number", "contract_date", "company_legal_entity",
                "company_abn_acn", "company_address", "company_phone", "company_email",
                "authorised_representative", "guardian_name", "guardian_relationship",
                "guardian_phone", "guardian_email", "guardian_address", "student_name",
                "date_of_birth", "student_id", "current_grade", "application_grade",
                "planned_entry_year",
            )
        ]
    else:
        identity_rows = [
            ("合同编号", instance.get("contract_uid", "")),
            ("合同日期", instance.get("contract_date", "")),
            ("甲方法律主体名称", commercial.get("company_name") or DEFAULT_PARTY_A_NAME),
            ("甲方 ABN / ACN", commercial.get("company_abn", "")),
            ("甲方地址", commercial.get("company_address") or DEFAULT_PARTY_A_ADDRESS),
            ("甲方电话", company_phone),
            ("甲方邮箱", company_email),
            ("甲方授权代表 / 顾问", instance.get("company_authorised_person", "")),
            ("乙方签署人", signer.get("name", "")),
            ("与学生关系", signer.get("relationship", "")),
            ("乙方电话", signer.get("phone", "")),
            ("乙方邮箱", signer.get("email", "")),
            ("乙方地址", instance.get("parent_address") or instance.get("student_home_address", "")),
            ("学生姓名", instance.get("student_full_name") or student_name),
            ("学生出生日期", instance.get("student_date_of_birth", "")),
            ("学生护照号 / 学生 ID", instance.get("student_id_or_passport_number", "")),
            ("当前年级", instance.get("student_current_year_level", "")),
            ("申请年级", instance.get("student_applying_year_level", "")),
        ]
    binding_value = lambda key, fallback="": contract_display_bindings.binding_value(
        display_bindings, key, fallback
    )
    pricing_rows = _pricing_rows(commercial, instance, config, display_bindings)
    content: list[dict[str, object]] = [
        _table(HEADINGS["fixed_parties"], "protected_contract_identity", identity_rows),
        {
            "kind": "section", "key": "fixed_service_scope",
            "title": HEADINGS["fixed_service_scope"],
            "paragraphs": _paragraphs(config.service_scope_intro) + _paragraphs(
                "\n".join(f"{index}. {label}" for index, label in enumerate(services, 1))
            ),
        },
    ]
    for key in ("profile_3", "profile_4", "profile_5", "profile_6", "profile_7"):
        body = slot_body(key)
        content.append({"kind": "section", "key": key, "title": blocks[key]["title"], "paragraphs": _paragraphs(body)})
        if key == "profile_3":
            content.append(_table("费用明细", "protected_pricing_table", pricing_rows))
    for key in ("legal_dispute", "profile_9"):
        content.append({
            "kind": "section", "key": key, "title": blocks[key]["title"],
            "paragraphs": _paragraphs(slot_body(key)),
        })
    special = str(instance.get("special_terms") or slot_body("special_terms") or "无。")
    content.append({"kind": "section", "key": "special_terms", "title": blocks["special_terms"]["title"], "paragraphs": _paragraphs(special)})
    signature_lines = [
        "AAIE / 甲方（签字 / 盖章）",
        f"代理方（甲方盖章）：{binding_value('company_legal_entity', commercial.get('company_name') or DEFAULT_PARTY_A_NAME)}",
        f"授权代表：{binding_value('authorised_representative', instance.get('company_authorised_person', ''))}",
        "甲方签署日期：",
        "委托方（乙方签字）",
        f"法定代理人或者监护人：{binding_value('guardian_name', signer.get('name', ''))}",
        f"关系：{binding_value('guardian_relationship', signer.get('relationship', ''))}",
        "家长/乙方签署日期：",
    ]
    return {
        "schema_version": 3,
        "layout_policy": copy.deepcopy(MASTER_LAYOUT_POLICY),
        "header": [{"lines": [str(snapshot.get("contract_title") or config.title)]}],
        "content": content,
        "footer": [{"lines": [str(commercial.get("company_name") or DEFAULT_PARTY_A_NAME), company_contact]}],
        "signature_region": {"title": "十一、签署", "paragraphs": [{"lines": signature_lines}]},
        "display_bindings": copy.deepcopy(dict(display_bindings or {})),
        "master_data": {
            "contract_title": snapshot.get("contract_title") or config.title,
            "contract_uid": binding_value("contract_number", instance.get("contract_uid", "")),
            "contract_date": binding_value("contract_date", instance.get("contract_date", "")),
            "service_type": config.cover_service_summary,
            "service_scope_items": services,
            "service_scope_override_text": "",
            "service_fee_label": config.service_fee_label,
            "pricing_rows": pricing_rows,
            "contract_sections": {
                key: slot_body(key)
                for key in MASTER_SLOT_ORDER
                if key.startswith("profile_") or key == "legal_dispute"
            },
            "contract_section_titles": {
                key: str(blocks[key]["title"])
                for key in EDITABLE_MASTER_SLOTS
            },
            "special_terms": special,
            "company_legal_name": binding_value("company_legal_entity", commercial.get("company_name") or DEFAULT_PARTY_A_NAME),
            "company_abn_acn": binding_value("company_abn_acn", commercial.get("company_abn", "")),
            "company_address": binding_value("company_address", commercial.get("company_address") or DEFAULT_PARTY_A_ADDRESS),
            "company_phone": binding_value("company_phone", company_phone),
            "company_email": binding_value("company_email", company_email),
            "company_authorised_person": binding_value("authorised_representative", instance.get("company_authorised_person", "")),
            "parent_signer_name": binding_value("guardian_name", signer.get("name", "")),
            "parent_signer_relationship": binding_value("guardian_relationship", signer.get("relationship", "")),
            "parent_phone": binding_value("guardian_phone", signer.get("phone", "")),
            "parent_email": binding_value("guardian_email", signer.get("email", "")),
            "parent_address": binding_value("guardian_address", instance.get("parent_address") or instance.get("student_home_address", "")),
            "student_full_name": binding_value("student_name", instance.get("student_full_name") or student_name),
            "student_date_of_birth": binding_value("date_of_birth", instance.get("student_date_of_birth", "")),
            "student_id_or_passport_number": binding_value("student_id", instance.get("student_id_or_passport_number", "")),
            "student_current_year_level": binding_value("current_grade", instance.get("student_current_year_level", "")),
            "study_level": binding_value("application_grade", instance.get("student_applying_year_level", "")),
            "student_applying_year_level": binding_value("application_grade", instance.get("student_applying_year_level", "")),
            "planned_entry_year": binding_value("planned_entry_year", instance.get("intake_year", "")),
            "intake_year": binding_value("planned_entry_year", instance.get("intake_year", "")),
            "intake_term": binding_value("planned_entry_term", instance.get("intake_term", "")),
            "canonical_display_binding_schema_version": (
                display_bindings.get("schema_version") if display_bindings else None
            ),
        },
    }


def render_master_docx_bytes(model: Mapping[str, object]) -> bytes:
    document = build_study_abroad_contract_document(dict(model["master_data"]))
    packed_model = base64.b64encode(
        gzip.compress(
            json.dumps(model, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"),
            mtime=0,
        )
    ).decode("ascii")
    output = io.BytesIO()
    document.save(output)
    with zipfile.ZipFile(io.BytesIO(output.getvalue()), "r") as source:
        parts = {name: source.read(name) for name in source.namelist()}
    parts["customXml/aaieMasterModel.xml"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<aaieMasterModel xmlns="urn:aaie:contract-master-layout:v1" encoding="gzip-base64">'
        + packed_model + '</aaieMasterModel>'
    ).encode("utf-8")
    from lxml import etree
    content_types = etree.fromstring(parts["[Content_Types].xml"])
    content_ns = "http://schemas.openxmlformats.org/package/2006/content-types"
    if not any(node.get("PartName") == "/customXml/aaieMasterModel.xml" for node in content_types):
        etree.SubElement(
            content_types, f"{{{content_ns}}}Override",
            {"PartName": "/customXml/aaieMasterModel.xml", "ContentType": "application/xml"},
        )
    parts["[Content_Types].xml"] = etree.tostring(content_types, xml_declaration=True, encoding="UTF-8", standalone=True)
    relationships = etree.fromstring(parts["_rels/.rels"])
    rel_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
    if not any(node.get("Id") == "rIdAAIEMasterModel" for node in relationships):
        etree.SubElement(
            relationships, f"{{{rel_ns}}}Relationship",
            {
                "Id": "rIdAAIEMasterModel",
                "Type": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/customXml",
                "Target": "customXml/aaieMasterModel.xml",
            },
        )
    parts["_rels/.rels"] = etree.tostring(relationships, xml_declaration=True, encoding="UTF-8", standalone=True)
    packaged = io.BytesIO()
    with zipfile.ZipFile(packaged, "w", compression=zipfile.ZIP_DEFLATED) as target:
        for name in sorted(parts):
            target.writestr(name, parts[name])
    return packaged.getvalue()


def _pdf_styles():
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from pathlib import Path

    font_path = Path(__file__).resolve().parent / "assets/fonts/noto-sans-cjk-sc/NotoSansSC-Regular.ttf"
    name = "AAIEMasterNotoSansSC"
    if name not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(name, str(font_path)))
    styles = getSampleStyleSheet()
    return name, {
        "body": ParagraphStyle("AAIEBody", parent=styles["BodyText"], fontName=name, fontSize=9.2, leading=13.2, spaceAfter=5),
        "heading": ParagraphStyle("AAIEHeading", parent=styles["Heading2"], fontName=name, fontSize=12.5, leading=16, textColor="#1F5A75", spaceBefore=9, spaceAfter=5, keepWithNext=True),
        "signature": ParagraphStyle("AAIESignature", parent=styles["BodyText"], fontName=name, fontSize=9.5, leading=17),
        "cover": ParagraphStyle("AAIECover", parent=styles["Title"], fontName=name, alignment=TA_CENTER),
    }


def render_master_pdf_bytes(model: Mapping[str, object]) -> tuple[bytes, dict[str, object]]:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas
    from reportlab.platypus import Image, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    font, styles = _pdf_styles()
    output = io.BytesIO()
    document = SimpleDocTemplate(
        output, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=16 * mm, bottomMargin=16 * mm,
        title=str(model["header"][0]["lines"][0]), author="AAIE CRM",
    )
    story = []
    cover = Image(io.BytesIO(study_contract_cover_png_bytes(dict(model["master_data"]))), width=174 * mm, height=246 * mm)
    story.extend([cover, PageBreak()])

    def markup(value):
        return html.escape(str(value)).replace("\n", "<br/>")

    for node in model["content"]:
        story.append(Paragraph(markup(node["title"]), styles["heading"]))
        if node["kind"] == "section":
            for paragraph in node["paragraphs"]:
                story.append(Paragraph(markup("\n".join(paragraph["lines"])), styles["body"]))
        else:
            rows = [[Paragraph(markup(cell["lines"][0]), styles["body"]) for cell in row["cells"]] for row in node["rows"]]
            table = Table(rows, colWidths=[52 * mm, 122 * mm], repeatRows=0, hAlign="LEFT")
            table.setStyle(TableStyle([
                ("FONTNAME", (0, 0), (-1, -1), font),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#F3F6F8")),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#B7CFEA")),
                ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]))
            story.append(table)
    story.append(PageBreak())
    story.append(Paragraph("十一、签署", styles["heading"]))
    party_rows = model["master_data"]
    details = Table([
        ["甲方法律主体名称", party_rows.get("company_legal_name", "")],
        ["甲方 ABN / ACN", party_rows.get("company_abn_acn", "")],
        ["甲方授权代表", party_rows.get("company_authorised_person", "")],
        ["甲方电话", party_rows.get("company_phone", "")],
        ["甲方联系邮箱", party_rows.get("company_email", "")],
        ["甲方地址", party_rows.get("company_address", "")],
        ["乙方签署人", party_rows.get("parent_signer_name", "")],
        ["与学生关系", party_rows.get("parent_signer_relationship", "")],
        ["乙方电话", party_rows.get("parent_phone", "")],
        ["乙方邮箱", party_rows.get("parent_email", "")],
        ["乙方地址", party_rows.get("parent_address", "")],
    ], colWidths=[87 * mm, 87 * mm])
    details.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), font), ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#B7CFEA")),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#F3F6F8")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.extend([
        details,
        Spacer(1, 3 * mm),
        Paragraph(
            "请乙方/监护人自行签名并填写乙方日期；甲方签名、公章及甲方日期由 AAIE 在最终合同中加盖。",
            styles["body"],
        ),
        Spacer(1, 2 * mm),
    ])

    signature_column_width = 87 * mm

    def signature_line_row(label, value="", label_width=42 * mm):
        field_width = signature_column_width - label_width - 5 * mm
        line = Table(
            [[Paragraph(markup(label), styles["signature"]), Paragraph(markup(value), styles["signature"])]],
            colWidths=[label_width, field_width],
            rowHeights=[10 * mm],
        )
        line.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 2),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
            ("LINEBELOW", (1, 0), (1, 0), 0.7, colors.HexColor("#333333")),
        ]))
        return line

    left_body = [
        Paragraph(
            "代理方（甲方盖章）：" + markup(party_rows.get("company_legal_name", "")),
            styles["signature"],
        ),
        Paragraph(
            "授权代表：" + markup(party_rows.get("company_authorised_person", "")),
            styles["signature"],
        ),
        Paragraph("甲方签署日期：", styles["signature"]),
    ]
    right_body = [
        signature_line_row("委托方（乙方签字）：", label_width=41 * mm),
        Spacer(1, 2 * mm),
        signature_line_row("法定代理人或者监护人（名字）：", label_width=56 * mm),
        Spacer(1, 2 * mm),
        signature_line_row("家长/乙方签署日期：", "____年____月____日", label_width=42 * mm),
        Paragraph("（乙方/监护人自行签署并填写日期）", styles["signature"]),
    ]
    signature = Table([
        [Paragraph("<b>AAIE / 甲方（签字 / 盖章）</b>", styles["signature"]),
         Paragraph("<b>委托方（乙方签字）</b>", styles["signature"])],
        [left_body, right_body],
    ], colWidths=[signature_column_width, signature_column_width], rowHeights=[None, 72 * mm])
    signature.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.6, colors.HexColor("#B7CFEA")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F3F6F8")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(signature)

    def canvasmaker(*args, **kwargs):
        kwargs["invariant"] = 1
        return canvas.Canvas(*args, **kwargs)

    document.build(story, canvasmaker=canvasmaker)
    raw = output.getvalue()
    page_count = len(PdfReader(io.BytesIO(raw)).pages)
    manifest = {
        "premium_cover": {"page": 1},
        "signature_aaie": {"page": page_count, "x": 68.0, "column": "left"},
        "signature_family": {"page": page_count, "x": 315.0, "column": "right"},
        "stamp": {"page": page_count, "x": 136.0, "column": "left_only"},
        "confirmation": {"placement": "after_fully_executed"},
    }
    return raw, manifest


def render_master_signing_html(model: Mapping[str, object]) -> bytes:
    packed_model = base64.b64encode(
        gzip.compress(
            json.dumps(model, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"),
            mtime=0,
        )
    ).decode("ascii")
    parts = [
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">',
        '<style>body{font-family:Arial,sans-serif;max-width:900px;margin:auto;color:#1f2933}'
        'h2{color:#1f5a75}.identity,.pricing{width:100%;border-collapse:collapse}'
        'td{border:1px solid #b7cfea;padding:6px}.signature-grid{display:grid;grid-template-columns:1fr 1fr;gap:0}'
        '.signature-grid>section{border:1px solid #b7cfea;min-height:260px;padding:16px}'
        '.stamp-anchor{display:block;width:90px;height:90px}</style></head><body><article>',
    ]
    for node in model["content"]:
        parts.append(f'<section data-master-anchor="{html.escape(str(node["key"]))}"><h2>{html.escape(str(node["title"]))}</h2>')
        if node["kind"] == "section":
            for paragraph in node["paragraphs"]:
                parts.append("<p>" + "<br>".join(html.escape(str(line)) for line in paragraph["lines"]) + "</p>")
        else:
            parts.append('<table class="identity">')
            for row in node["rows"]:
                parts.append("<tr>" + "".join("<td>" + "<br>".join(html.escape(str(line)) for line in cell["lines"]) + "</td>" for cell in row["cells"]) + "</tr>")
            parts.append("</table>")
        parts.append("</section>")
    data = model["master_data"]
    parts.extend([
        '<section data-master-anchor="section_11_signature"><h2>十一、签署</h2><div class="signature-grid">',
        '<section data-signature-anchor="signature_aaie"><b>AAIE / 甲方（签字 / 盖章）</b><span class="stamp-anchor" data-stamp-anchor="aaie-only"></span>授权代表：' + html.escape(str(data.get("company_authorised_person", ""))) + '</section>',
        '<section data-signature-anchor="signature_family"><b>委托方（乙方签字）</b><br>法定代理人或者监护人：' + html.escape(str(data.get("parent_signer_name", ""))) + '</section>',
        '</div></section></article><script id="aaie-master-model" type="application/octet-stream">' + packed_model + '</script></body></html>',
    ])
    return "".join(parts).encode("utf-8")
