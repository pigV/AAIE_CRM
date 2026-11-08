"""Transactional school invoice outbox and a file-only rehearsal mailbox."""

from email import policy
from email.message import EmailMessage
from email.utils import format_datetime
from datetime import datetime, timedelta, timezone
import hashlib
import json
from os.path import lexists
import sqlite3
from time import sleep

from revenue.invoice_templates import FORMAL_INVOICE_KIND
from revenue.repository import canonical_json, new_uid
from revenue.school_invoice_reminders import EMAIL
from revenue.school_invoice_release import release_values
from revenue.secure_storage import StorageUnavailable, read_secure, write_immutable
from revenue.storage_locations import resolve_location
from revenue.data_invoice_storage import SUFFIX, read_invoice_pdf


SETTINGS = "school_invoice_test_mail_settings"
QUEUED = "school_invoice_test_mail_queued"
ATTEMPTED = "school_invoice_test_mail_attempted"
RETRY = "school_invoice_test_mail_retry"


def mail_settings(connection, entity_id):
    row = connection.execute(
        """SELECT after_json FROM revenue_audit_events
           WHERE action=? AND aggregate_uid=? ORDER BY id DESC LIMIT 1""",
        (SETTINGS, str(entity_id)),
    ).fetchone()
    return json.loads(row[0]) if row else {"enabled": False, "recipient": "", "verified": False, "revision": ""}


def save_mail_settings(repository, entity_id, actor, key, recipient, enabled, verified, expected_revision, *, smtp_recipient=None):
    recipient = str(recipient or "").strip()
    if recipient and (len(recipient) > 254 or not EMAIL.fullmatch(recipient)
                      or (recipient != smtp_recipient if smtp_recipient else not recipient.lower().endswith(".test"))):
        raise ValueError("收件地址不符合当前隔离发信配置。")
    if enabled and (not recipient or not verified):
        raise ValueError("请核对学校财务邮箱后再启用自动发送。")
    payload = dict(uid=str(entity_id), enabled=bool(enabled), recipient=recipient,
                   verified=bool(recipient and verified), expected_revision=expected_revision,
                   transport="smtp" if smtp_recipient else "mock")
    def operation():
        current = mail_settings(repository.connection, entity_id)
        if current["revision"] != expected_revision:
            raise ValueError("学校发信设置已变更，请刷新后核对。")
        unchanged = (all(current[field] == payload[field] for field in ("enabled", "recipient", "verified"))
                     and current.get("transport", "mock") == payload["transport"])
        return {**payload, "revision": current["revision"] if unchanged else new_uid("mail-settings")}

    return repository.command(
        idempotency_key=f"invoice-mail-settings:{key}" if key else "",
        command_type=SETTINGS, aggregate_type="school_invoice_mail_settings", payload=payload,
        actor_user_id=actor, operation=operation,
    )


def save_mail_release(repository, entity_id, actor, key, revision, stage, recipient, evidence,
                      approval, claims, simulation, *, available_claims=None):
    request = dict(uid=str(entity_id), expected_revision=revision, stage=stage, recipient=recipient,
                   evidence=evidence, approval=approval, claims=list(claims), simulation=simulation)

    def operation():
        current = mail_settings(repository.connection,entity_id)
        if current["revision"]!=revision:
            raise ValueError("邮件试点设置已变更，请刷新后核对。")
        values = release_values(current,stage,recipient,evidence,approval,claims,simulation)
        if stage=="first" and (available_claims is None or not set(claims)<=available_claims()):
            raise ValueError("请选择本校尚未开票的已审核申领。")
        connection = repository.connection
        if stage=="first" and connection.execute(
            """SELECT 1 FROM revenue_outbox_events WHERE event_type=?
                 AND last_error_code IN ('smtp_inflight','smtp_uncertain')
                 AND json_extract(payload_json,'$.entity_id')=?
                 AND json_extract(payload_json,'$.transport')=? LIMIT 1""",
            (QUEUED,entity_id,values["transport"]),
        ).fetchone():
            raise ValueError("上一封邮件仍在发送或待核对，请先核对该邮件。")
        quarantined = connection.execute(
            """UPDATE revenue_outbox_events SET last_error_code='settings_changed'
               WHERE event_type=? AND dispatched_at IS NULL
                 AND coalesce(last_error_code,'') NOT IN ('smtp_inflight','smtp_uncertain','settings_changed')
                 AND json_extract(payload_json,'$.entity_id')=?
                 AND (json_extract(payload_json,'$.transport')=?
                      OR (?='candidate' AND json_extract(payload_json,'$.transport')='smtp'))""",
            (QUEUED,entity_id,values["transport"],values["transport"]),
        ).rowcount
        return {"uid":str(entity_id), **values, "revision":new_uid("mail-release"), "quarantined":quarantined}

    return repository.command(
        idempotency_key=f"invoice-mail-release:{key}" if key else "", command_type=SETTINGS,
        aggregate_type="school_invoice_mail_settings", payload=request, actor_user_id=actor, operation=operation,
    )


def enqueue_mail(repository, invoice, document, entity_id, actor, *, release=None, claim_uids=()):
    settings = mail_settings(repository.connection, entity_id)
    if not (settings["enabled"] and settings["verified"]):
        return
    mode = settings.get("transport","mock")
    if release:
        if mode!=release or list(sorted(claim_uids))!=settings.get("claims"):
            return
    elif mode not in {"mock","smtp"}:
        return
    payload = dict(uid=invoice["uid"], entity_id=entity_id,
                   recipient=settings.get("delivery_recipient",settings["recipient"]),
                   settings_revision=settings["revision"], invoice_number=invoice["invoice_number"],
                   gross_cents=invoice["gross_cents"], actor=actor, document_uid=document["uid"],
                   transport=mode)
    if release:
        payload.update(finance_recipient=settings["recipient"], evidence=settings["evidence"],
                       approval=settings["approval"])
    repository.command(
        idempotency_key=f"invoice-mail:{invoice['uid']}", command_type=QUEUED,
        aggregate_type="invoice", payload=payload, actor_user_id=actor, operation=lambda: payload,
    )


def mail_status(connection, invoice_uid):
    row = connection.execute(
        """SELECT * FROM revenue_outbox_events WHERE event_type=? AND aggregate_uid=?""",
        (QUEUED, invoice_uid),
    ).fetchone()
    if row is None:
        return None
    attempts = [
        {**json.loads(record["after_json"]), "occurred_at": record["occurred_at"]}
        for record in connection.execute(
            """SELECT after_json,occurred_at FROM revenue_audit_events
               WHERE action=? AND aggregate_uid=? ORDER BY id""", (ATTEMPTED, invoice_uid),
        )
    ]
    if row["dispatched_at"]:
        status = "sent"
    elif row["last_error_code"] == "smtp_inflight":
        started = datetime.fromisoformat(attempts[-1]["occurred_at"])
        status = "sending" if datetime.now(timezone.utc) - started < timedelta(minutes=3) else "needs_review"
    elif row["last_error_code"] in {"settings_changed","smtp_disabled","smtp_limit","smtp_recipient_not_allowed","smtp_invoice_not_approved"}:
        status = "cancelled"
    elif row["last_error_code"] in {"simulation_mailbox_needs_review","smtp_uncertain"}:
        status = "needs_review"
    elif row["last_error_code"]:
        status = "failed"
    else:
        status = "pending"
    return {**json.loads(row["payload_json"]), "status": status, "attempt_count": row["attempt_count"],
            "queued_at": row["created_at"], "sent_at": row["dispatched_at"], "attempts": attempts}


def retry_mail(repository, invoice_uid, entity_id, actor, key, attempt_count):
    payload = dict(uid=invoice_uid, entity_id=entity_id, attempt_count=attempt_count, actor=actor)

    def operation():
        event = mail_status(repository.connection, invoice_uid)
        if (not event or event["entity_id"] != entity_id or event["status"] != "failed"
                or event["attempt_count"] != attempt_count):
            raise ValueError("邮件状态已变更，请刷新后再重试。")
        repository.connection.execute(
            """UPDATE revenue_outbox_events SET last_error_code=NULL
               WHERE event_type=? AND aggregate_uid=?""", (QUEUED, invoice_uid),
        )
        return payload

    return repository.command(
        idempotency_key=f"invoice-mail-retry:{key}" if key else "", command_type=RETRY,
        aggregate_type="invoice", payload=payload, actor_user_id=actor, operation=operation,
    )


def _message(event, created_at, pdf, sender=None):
    message = EmailMessage(policy=policy.SMTP)
    number = event["invoice_number"]
    message["From"] = sender or "AAIE Accounts Receivable <invoices@aaie.example.test>"
    message["To"] = event["recipient"]
    amount = f"{event['gross_cents'] // 100:,}.{event['gross_cents'] % 100:02d}"
    production = event.get("transport") == "production"
    if production:
        message["Subject"] = f"Commission invoice {number} | Toorak College"
    elif sender:
        message["Subject"] = f"TEST ONLY | CRM Queue | Toorak | AUD {amount} | {number}"
    else:
        message["Subject"] = f"TEST ONLY | Commission invoice {number}"
    message["Date"] = format_datetime(datetime.fromisoformat(created_at))
    message["Message-ID"] = _message_id(event, sender)
    notice = "" if production else "TEST ONLY - simulated school commission invoice.\n\n"
    message.set_content(
        notice + "Dear Accounts Payable team,\n\n"
        f"Please find attached commission invoice {number} for AUD {amount}.\n"
        "Please acknowledge receipt and quote the invoice number in your remittance advice.\n\n"
        "Kind regards,\nAAIE Accounts Receivable\n"
    )
    message.add_attachment(pdf, maintype="application", subtype="pdf", filename=f"{number}.pdf")
    message.set_boundary(f"aaie-invoice-{event['uid']}")
    return message.as_bytes()


def _message_id(event, sender):
    domain = sender.rsplit("@", 1)[1] if sender else "aaie.example.test"
    return f"<{event['uid']}@{domain}>"


def _verified_pdf(connection, root, event):
    document = connection.execute(
        """SELECT d.*
           FROM revenue_invoices i JOIN revenue_documents d ON d.id=i.document_id
           WHERE i.uid=? AND i.revenue_entity_id=? AND i.status='issued'
             AND d.uid=? AND d.document_kind=?""",
        (event["uid"], event["entity_id"], event["document_uid"], FORMAL_INVOICE_KIND),
    ).fetchone()
    if not document:
        raise StorageUnavailable()
    def query_all(sql, params=()):
        return connection.execute(sql, params).fetchall()

    path = resolve_location(document, query_all)
    if str(path).endswith(SUFFIX):
        return read_invoice_pdf(path,document["sha256"],document["size_bytes"])
    if root is None:
        raise StorageUnavailable()
    return read_secure(root, path, document["sha256"], document["size_bytes"])


def _verified_content(connection, root, row, event):
    return _message(event, row["created_at"], _verified_pdf(connection, root, event))


def _deliver(connection, root, row, event, authorized):
    if root is None:
        return ("failed", "simulation_mailbox_unavailable") if authorized else (
            "needs_review", "simulation_mailbox_needs_review"
        )
    mailbox = root / "school-invoice-test-mailbox"
    filename = event["uid"] + ".eml"
    if not authorized and not lexists(mailbox / filename):
        return "cancelled", "settings_changed"
    try:
        content = _verified_content(connection, root, row, event)
        if authorized:
            write_immutable(mailbox, filename, content)
        else:
            # Recover a prior durable local write before applying a setting change.
            read_secure(root, mailbox / filename, hashlib.sha256(content).hexdigest(),
                        len(content), max_bytes=len(content))
    except (OSError, StorageUnavailable):
        return ("failed", "simulation_mailbox_unavailable") if authorized else (
            "needs_review", "simulation_mailbox_needs_review"
        )
    return "sent", None


def _dispatch(repository, root, row, event):
    connection = repository.connection
    settings = mail_settings(connection, event["entity_id"])
    authorized = settings["enabled"] and settings["verified"] and settings["revision"] == event["settings_revision"]
    result, error = _deliver(connection, root, row, event, authorized)
    attempt = row["attempt_count"] + int(authorized or result == "sent")
    now = repository.now()
    connection.execute(
        """UPDATE revenue_outbox_events SET dispatched_at=?,attempt_count=?,last_error_code=? WHERE id=?""",
        (now if result == "sent" else None, attempt, error, row["id"]),
    )
    detail = dict(result=result, attempt=attempt, error=error, recipient=event["recipient"],
                  document_uid=event["document_uid"], simulation=True)
    connection.execute(
        """INSERT INTO revenue_audit_events(uid,action,aggregate_type,aggregate_uid,after_json,actor_user_id,occurred_at)
           VALUES(?,?,'invoice',?,?,?,?)""",
        (new_uid("audit"), ATTEMPTED, event["uid"], canonical_json(detail), event["actor"], now),
    )
    return detail


def _settings_match(connection, event):
    settings = mail_settings(connection, event["entity_id"])
    return (settings["enabled"] and settings["verified"]
            and settings["revision"] == event["settings_revision"]
            and settings.get("delivery_recipient",settings["recipient"]) == event["recipient"]
            and settings.get("transport", "mock") == event.get("transport", "mock"))


def _smtp_audit(repository, event, attempt, result, **detail):
    repository.connection.execute(
        """INSERT INTO revenue_audit_events(uid,action,aggregate_type,aggregate_uid,after_json,actor_user_id,occurred_at)
           VALUES(?,?,'invoice',?,?,?,?)""",
        (new_uid("audit"), ATTEMPTED, event["uid"], canonical_json({
            "result": result, "attempt": attempt, "recipient": event["recipient"],
            "document_uid": event["document_uid"], "transport": event.get("transport","smtp"),
            "settings_revision":event["settings_revision"], **detail,
        }), event["actor"], repository.now()),
    )


def _smtp_invoice_approved(connection, event, transport):
    lines = connection.execute(
        """SELECT l.source_uid FROM revenue_invoices i JOIN revenue_invoice_lines l ON l.invoice_id=i.id
           JOIN revenue_documents d ON d.id=i.document_id
           WHERE i.uid=? AND i.revenue_entity_id=? AND i.status='issued'
             AND d.uid=? AND l.source_kind='claim'
             AND EXISTS (SELECT 1 FROM revenue_audit_events a WHERE a.aggregate_uid=i.uid
                         AND a.action='school_commission_invoice_issued'
                         AND json_extract(a.after_json,'$.simulation')=?)
             AND EXISTS (SELECT 1 FROM revenue_claims c WHERE c.uid=l.source_uid
                         AND c.revenue_entity_id=i.revenue_entity_id AND c.status='issued')
           ORDER BY l.source_uid""", (event["uid"],event["entity_id"],event["document_uid"],transport.simulation),
    )
    return tuple(row[0] for row in lines) in transport.claim_sets


def _claim_smtp(repository, row, event, transport):
    connection = repository.connection
    prior = connection.execute(
        """SELECT count(DISTINCT aggregate_uid),
                  coalesce(max(CASE WHEN aggregate_uid=? THEN 1 ELSE 0 END),0)
           FROM revenue_audit_events WHERE action=?
             AND json_extract(after_json,'$.result')='sending'
             AND json_extract(after_json,'$.transport')=?
             AND (?='smtp' OR json_extract(after_json,'$.settings_revision')=?)""",
        (event["uid"],ATTEMPTED,transport.mode,transport.mode,event["settings_revision"]),
    ).fetchone()
    error = None
    if not _settings_match(connection, event) or event["recipient"] != transport.recipient:
        error = "settings_changed"
    elif not _smtp_invoice_approved(connection,event,transport):
        error = "smtp_invoice_not_approved"
    elif prior[0] >= transport.limit and not prior[1]:
        error = "smtp_limit"
    attempt = row["attempt_count"] + int(error is None)
    connection.execute("UPDATE revenue_outbox_events SET last_error_code=?,attempt_count=? WHERE id=?",
                       (error or "smtp_inflight", attempt, row["id"]))
    _smtp_audit(repository,event,attempt,"cancelled" if error else "sending",
                message_id=_message_id(event,transport.sender),error=error)
    return error is None


def _submit_smtp(repository, root, row, event, transport, eligible):
    connection = repository.connection
    digest = None
    try:
        pdf = _verified_pdf(connection,root,event)
    except (OSError, StorageUnavailable):
        result,error,code = "failed","smtp_attachment_unavailable",None
    else:
        digest = hashlib.sha256(pdf).hexdigest()
        content = _message(event,row["created_at"],pdf,transport.sender)

        def allowed():
            return (eligible(event) and _settings_match(connection,event)
                    and _smtp_invoice_approved(connection,event,transport))

        result,error,code = transport.submit(event["recipient"],content,allowed)
    attempt = row["attempt_count"]+1
    for retry in range(5):
        try:
            with repository.transaction():
                update = connection.execute(
                    """UPDATE revenue_outbox_events SET last_error_code=?,dispatched_at=?
                       WHERE id=? AND attempt_count=? AND last_error_code='smtp_inflight'""",
                    (error,repository.now() if result=="sent" else None,row["id"],attempt),
                )
                if update.rowcount != 1:
                    raise ValueError("SMTP queue reservation changed")
                _smtp_audit(repository,event,attempt,result,error=error,smtp_code=code,
                            message_id=_message_id(event,transport.sender),pdf_sha256=digest)
            break
        except sqlite3.OperationalError as failure:
            connection.rollback()
            if retry == 4 or (getattr(failure,"sqlite_errorcode",0) & 255) != sqlite3.SQLITE_BUSY:
                raise
            sleep(0.25)
    return {"result":result,"error":error}


def dispatch_next(repository, root, eligible, *, smtp=None):
    """Process one eligible message under the SQLite writer lock.

    Mailbox publication uses a stable filename and deterministic bytes, so an
    interrupted database commit can replay without creating another message.
    """
    connection = repository.connection
    pending = """SELECT * FROM revenue_outbox_events WHERE event_type=?
                 AND dispatched_at IS NULL AND last_error_code IS NULL ORDER BY id"""
    if connection.execute(pending + " LIMIT 1", (QUEUED,)).fetchone() is None:
        return None
    connection.execute("BEGIN IMMEDIATE")
    try:
        for row in connection.execute(pending, (QUEUED,)):
            event = json.loads(row["payload_json"])
            if not eligible(event) or event.get("transport","mock") != (smtp.mode if smtp else "mock"):
                continue
            if smtp is None:
                result = _dispatch(repository, root, row, event)
                connection.commit()
                return result
            claimed = _claim_smtp(repository,row,event,smtp)
            connection.commit()
            if claimed:
                return _submit_smtp(repository,root,row,event,smtp,eligible)
            return {"result":"cancelled"}
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    return None
