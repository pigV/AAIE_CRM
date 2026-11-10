"""Versioned invoice addressee details for one commission payer."""

import json
import re

from revenue.repository import new_uid
from revenue.school_invoice_mail import mail_settings, save_mail_settings

SAVED = "commission_invoice_billing_details_saved"


def billing_details(connection, entity_id, simulation):
    uid = f"{entity_id}:{'simulation' if simulation else 'production'}"
    entity = connection.execute(
        "SELECT legal_name FROM revenue_entities WHERE id=? AND status='active'", (entity_id,),
    ).fetchone()
    if not entity:
        raise ValueError("请先登记并核对付款主体。")
    row = connection.execute(
        "SELECT after_json FROM revenue_audit_events WHERE action=? AND aggregate_uid=? ORDER BY id DESC LIMIT 1",
        (SAVED, uid),
    ).fetchone()
    if row:
        details = json.loads(row[0])
    else:
        from revenue.invoice_templates import financial_profile

        profile, _ = financial_profile(connection, formal=True, entity_id=entity_id, simulation=simulation)
        details = dict(uid=uid, revision="", name=entity[0], address=profile.get("bill_to_address") or "", phone="")
    settings = mail_settings(connection, entity_id)
    # A legacy test transport can never supply a production invoice's recipients.
    matching_environment = settings.get("transport", "mock") == ("mock" if simulation else "production")
    return {
        **details,
        "recipient": settings["recipient"] if matching_environment else "",
        "enabled": matching_environment and settings["enabled"],
        "verified": matching_environment and settings["verified"],
        "settings_revision": settings["revision"],
    }


def save_billing_details(repository, entity_id, actor, key, form, *, simulation):
    if not isinstance(key, str) or not key.strip():
        raise ValueError("请刷新页面后保存开票资料。")

    def text(field, maximum, label, *, multiline=False):
        value = form.get(field)
        if not isinstance(value, str):
            raise ValueError(f"请填写{label}。")
        value = (value.replace("\r\n", "\n") if multiline else value).strip()
        if not value or len(value) > maximum or any(
            (ord(char) < 32 and not (char == "\n" and multiline)) or ord(char) == 127 for char in value
        ):
            raise ValueError(f"请核对{label}的格式和长度。")
        return value

    name = text("billing_name", 180, "发票显示名称")
    address = text("billing_address", 300, "账单地址", multiline=True)
    phone = text("billing_phone", 60, "联系电话")
    if not re.fullmatch(r"[0-9+(). #xX-]+", phone) or not any(char.isdecimal() for char in phone):
        raise ValueError("请核对联系电话格式。")
    recipient = text("recipient", 2048, "收票邮箱")
    payload = dict(name=name, address=address, phone=phone, recipient=recipient,
                   verified=form.get("verified") == "1", enabled=form.get("enabled") == "1",
                   settings_revision=form.get("settings_revision"), billing_revision=form.get("billing_revision"),
                   entity_id=entity_id, simulation=simulation)

    def operation():
        current = billing_details(repository.connection, entity_id, simulation)
        if current["revision"] != payload["billing_revision"]:
            raise ValueError("学校开票资料已变更，请刷新后核对。")
        mail = save_mail_settings(
            repository, entity_id, actor, key, recipient, payload["enabled"], payload["verified"],
            payload["settings_revision"], payer_routing=True, simulation=simulation,
        )
        return dict(uid=current["uid"], revision=new_uid("billing-details"), name=name, address=address, phone=phone,
                    recipient=mail["recipient"], enabled=mail["enabled"], verified=mail["verified"],
                    settings_revision=mail["revision"])

    return repository.command(
        idempotency_key=f"invoice-billing-details:{key}", command_type=SAVED, aggregate_type="invoice_billing_details",
        payload=payload, actor_user_id=actor, operation=operation,
    )


def bill_to_fields(connection, entity_id, simulation):
    details = billing_details(connection, entity_id, simulation)
    if not details["revision"]:
        return {}
    return dict(bill_to=details["name"], bill_to_address=details["address"],
                bill_to_phone=details["phone"], bill_to_email=details["recipient"])
