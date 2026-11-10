"""Transactional school invoice outbox and a file-only rehearsal mailbox."""

from email import policy
from email.message import EmailMessage
from email.utils import format_datetime
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import hashlib
import json
from os.path import lexists
import sqlite3
from time import sleep

from revenue.invoice_templates import FORMAL_INVOICE_KIND
from revenue.repository import canonical_json, new_uid
from revenue.school_invoice_reminders import EMAIL
from revenue.school_invoice_release import FINANCE_COPY_ADDRESS, SENDER, payer_mail_addresses, release_values
from revenue.secure_storage import StorageUnavailable, read_secure, write_immutable
from revenue.storage_locations import resolve_location
from revenue.data_invoice_storage import SUFFIX, read_invoice_pdf


SETTINGS = "school_invoice_test_mail_settings"
QUEUED = "school_invoice_test_mail_queued"
ATTEMPTED = "school_invoice_test_mail_attempted"
RETRY = "school_invoice_test_mail_retry"
ISSUE_ACTIONS = ("school_commission_invoice_issued", "authority_commission_invoice_issued")


def mail_settings(connection, entity_id):
    row = connection.execute(
        """SELECT after_json FROM revenue_audit_events
           WHERE action=? AND aggregate_uid=? ORDER BY id DESC LIMIT 1""",
        (SETTINGS, str(entity_id)),
    ).fetchone()
    return json.loads(row[0]) if row else {"enabled": False, "recipient": "", "verified": False, "revision": ""}


def save_mail_settings(repository, entity_id, actor, key, recipient, enabled, verified, expected_revision, *, smtp_recipient=None, payer_routing=False, simulation=True):
    recipient = str(recipient or "").strip()
    if recipient and payer_routing:
        recipient = ", ".join(payer_mail_addresses(recipient, simulation=simulation))
    elif recipient and (len(recipient) > 254 or not EMAIL.fullmatch(recipient)
                        or (recipient != smtp_recipient if smtp_recipient else
                            not recipient.casefold().endswith(".test"))):
        raise ValueError("收件地址不符合当前隔离发信配置。")
    if enabled and (not recipient or not verified):
        raise ValueError("请核对学校财务邮箱后再启用自动发送。")
    payload = dict(uid=str(entity_id), enabled=bool(enabled), recipient=recipient,
                   verified=bool(recipient and verified), expected_revision=expected_revision,
                   transport="smtp" if smtp_recipient else ("mock" if simulation else "production"))
    if payer_routing:
        payload["routing"] = "payer"
    def operation():
        current = mail_settings(repository.connection, entity_id)
        if current["revision"] != expected_revision:
            raise ValueError("学校发信设置已变更，请刷新后核对。")
        unchanged = (all(current[field] == payload[field] for field in ("enabled", "recipient", "verified"))
                     and current.get("transport", "mock") == payload["transport"]
                     and current.get("routing") == payload.get("routing"))
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


def enqueue_mail(repository, invoice, document, entity_id, actor, *, release=None, claim_uids=(),
                 payer_routing=False, simulation=True, authority_draft=None):
    settings = mail_settings(repository.connection, entity_id)
    if not (settings["enabled"] and settings["verified"]):
        return
    mode = settings.get("transport","mock")
    if payer_routing:
        if settings.get("routing") != "payer" or mode != ("mock" if simulation else "production"):
            return
        payer = repository.connection.execute(
            "SELECT legal_name FROM revenue_entities WHERE id=? AND status='active'", (entity_id,),
        ).fetchone()
        if not payer or invoice["revenue_entity_id"] != entity_id:
            raise ValueError("发票与付款主体不一致。")
    elif settings.get("routing") == "payer":
        return
    elif release:
        if mode!=release or list(sorted(claim_uids))!=settings.get("claims"):
            return
    elif mode not in {"mock","smtp"}:
        return
    payload = dict(uid=invoice["uid"], entity_id=entity_id,
                   recipient=settings.get("delivery_recipient",settings["recipient"]),
                   settings_revision=settings["revision"], invoice_number=invoice["invoice_number"],
                   gross_cents=invoice["gross_cents"], actor=actor, document_uid=document["uid"],
                   transport=mode)
    if mode in {"mock", "production"}:
        payload["cc"] = FINANCE_COPY_ADDRESS
    if payer_routing:
        payload.update(routing="payer", payer_name=" ".join(payer[0].split()))
        if authority_draft:
            payload["authority_draft"] = authority_draft
    payload["message"] = _message_fields(payload, SENDER if mode != "mock" or payer_routing else None)
    payload["attachment_sha256"] = document["sha256"]
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
    event = json.loads(row["payload_json"])
    if row["dispatched_at"]:
        status = "sent"
    elif row["last_error_code"] == "smtp_inflight":
        started = datetime.fromisoformat(attempts[-1]["occurred_at"])
        status = "sending" if datetime.now(timezone.utc) - started < timedelta(minutes=3) else "needs_review"
    elif row["last_error_code"] in {"settings_changed","smtp_disabled","smtp_limit","smtp_recipient_not_allowed","smtp_invoice_not_approved"}:
        status = "cancelled"
    elif row["last_error_code"] in {"simulation_mailbox_needs_review","smtp_uncertain"}:
        status = "needs_review"
    elif event.get("transport", "mock") == "mock" and not _settings_match(connection, event):
        status = "cancelled"
    elif row["last_error_code"]:
        status = "failed"
    else:
        status = "pending"
    return {**event, "status": status, "attempt_count": row["attempt_count"],
            "queued_at": row["created_at"], "sent_at": row["dispatched_at"], "attempts": attempts}


def invoice_mail_history(connection, entity_ids, simulation, *, number="", start=None, end=None,
                         status="", offset=0, limit=30):
    if not entity_ids:
        return [], False
    marks = ",".join("?" for _ in entity_ids)
    filters = [f"i.revenue_entity_id IN ({marks})"]
    params = [ATTEMPTED, QUEUED, *ISSUE_ACTIONS, int(simulation), *entity_ids]
    if number:
        filters.append("instr(upper(i.invoice_number), upper(?)) > 0")
        params.append(number)
    melbourne = ZoneInfo("Australia/Melbourne")
    if start:
        filters.append("history_at >= ?")
        params.append(datetime.combine(start, datetime.min.time(), melbourne).astimezone(timezone.utc).isoformat())
    if end:
        filters.append("history_at < ?")
        params.append(datetime.combine(end + timedelta(days=1), datetime.min.time(), melbourne)
                      .astimezone(timezone.utc).isoformat())
    matches = connection.execute(
        """WITH history AS (
            SELECT i.uid,i.invoice_number,i.revenue_entity_id,e.legal_name,
                   coalesce(o.dispatched_at,
                       (SELECT a.occurred_at FROM revenue_audit_events a
                         WHERE a.aggregate_uid=i.uid AND a.action=? ORDER BY a.id DESC LIMIT 1),
                       o.created_at) AS history_at
              FROM revenue_outbox_events o JOIN revenue_invoices i ON i.uid=o.aggregate_uid
              JOIN revenue_entities e ON e.id=i.revenue_entity_id
             WHERE o.event_type=? AND EXISTS (
                   SELECT 1 FROM revenue_audit_events a WHERE a.aggregate_uid=i.uid
                     AND a.action IN (?,?)
                     AND json_extract(a.after_json,'$.simulation')=?)
        ) SELECT * FROM history i WHERE """ + " AND ".join(filters) + " ORDER BY history_at DESC,uid DESC",
        params,
    )
    entries = []
    for row in matches:
        record = mail_status(connection, row["uid"])
        if status and record["status"] != status:
            continue
        if offset:
            offset -= 1
            continue
        if len(entries) == limit:
            return entries, True
        entries.append({**record, "school": row["legal_name"], "history_at": row["history_at"]})
    return entries, False


def retry_mail(repository, invoice_uid, entity_id, actor, key, attempt_count):
    payload = dict(uid=invoice_uid, entity_id=entity_id, attempt_count=attempt_count, actor=actor)

    def operation():
        event = mail_status(repository.connection, invoice_uid)
        if (not event or event["entity_id"] != entity_id or event["status"] != "failed"
                or event["attempt_count"] != attempt_count or not _settings_match(repository.connection, event)):
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


def _message_fields(event, sender=None):
    number = event["invoice_number"]
    amount = f"{event['gross_cents'] // 100:,}.{event['gross_cents'] % 100:02d}"
    production = event.get("transport") == "production"
    payer_name = event.get("payer_name")
    if production:
        subject = f"Commission invoice {number} | {payer_name or 'Toorak College'}"
    elif payer_name:
        subject = f"TEST ONLY | Commission invoice {number} | {payer_name}"
    elif sender:
        subject = f"TEST ONLY | CRM Queue | Toorak | AUD {amount} | {number}"
    else:
        subject = f"TEST ONLY | Commission invoice {number}"
    notice = "" if production else "TEST ONLY - simulated school commission invoice.\n\n"
    body = (
        notice + "Dear Accounts Payable team,\n\n"
        f"Please find attached commission invoice {number} for AUD {amount}.\n"
        "Please acknowledge receipt and quote the invoice number in your remittance advice.\n\n"
        "Kind regards,\nAAIE Accounts Receivable\n"
    )
    return {
        "sender": sender or "AAIE Accounts Receivable <invoices@aaie.example.test>",
        "subject": subject, "body": body, "attachment_name": f"{number}.pdf",
    }


def _message(event, created_at, pdf, sender=None):
    fields = event.get("message") or _message_fields(event, sender)
    message = EmailMessage(policy=policy.SMTP)
    message["From"] = fields["sender"]
    message["To"] = event["recipient"]
    if event.get("transport") in {"mock", "production"} and event.get("cc"):
        message["Cc"] = event["cc"]
    message["Subject"] = fields["subject"]
    message["Date"] = format_datetime(datetime.fromisoformat(created_at))
    message["Message-ID"] = _message_id(event, sender)
    message.set_content(fields["body"])
    message.add_attachment(pdf, maintype="application", subtype="pdf", filename=fields["attachment_name"])
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
    if not document or event.get("attachment_sha256", document["sha256"]) != document["sha256"]:
        raise StorageUnavailable()
    def query_all(sql, params=()):
        return connection.execute(sql, params).fetchall()

    path = resolve_location(document, query_all)
    if str(path).endswith(SUFFIX):
        return read_invoice_pdf(path,document["sha256"],document["size_bytes"])
    if root is None:
        raise StorageUnavailable()
    return read_secure(root, path, document["sha256"], document["size_bytes"])


def _deliver(connection, root, row, event, authorized, *, mock_writable_mount=None):
    if root is None:
        return ("failed", "simulation_mailbox_unavailable", None) if authorized else (
            "needs_review", "simulation_mailbox_needs_review", None
        )
    mailbox = root / "school-invoice-test-mailbox"
    filename = event["uid"] + ".eml"
    if not authorized and not lexists(mailbox / filename):
        return "cancelled", "settings_changed", None
    digest = None
    try:
        pdf = _verified_pdf(connection, root, event)
        digest = hashlib.sha256(pdf).hexdigest()
        content = _message(event, row["created_at"], pdf)
        storage = {"writable_mount":mock_writable_mount} if mock_writable_mount else {}
        if authorized:
            write_immutable(mailbox, filename, content, **storage)
        else:
            # Recover a prior durable local write before applying a setting change.
            read_secure(root, mailbox / filename, hashlib.sha256(content).hexdigest(),
                        len(content), max_bytes=len(content), **storage)
    except (OSError, StorageUnavailable):
        if not authorized or lexists(mailbox / filename):
            return "needs_review", "simulation_mailbox_needs_review", digest
        return "failed", "simulation_mailbox_unavailable", digest
    return "sent", None, digest


def _dispatch(repository, root, row, event, *, mock_writable_mount=None):
    connection = repository.connection
    authorized = _settings_match(connection, event)
    if event.get("routing") == "payer":
        authorized = authorized and payer_invoice_approved(connection, event, True)
    storage = {"mock_writable_mount":mock_writable_mount} if mock_writable_mount else {}
    result, error, pdf_digest = _deliver(connection, root, row, event, authorized, **storage)
    attempt = row["attempt_count"] + int(authorized or result == "sent")
    now = repository.now()
    connection.execute(
        """UPDATE revenue_outbox_events SET dispatched_at=?,attempt_count=?,last_error_code=? WHERE id=?""",
        (now if result == "sent" else None, attempt, error, row["id"]),
    )
    detail = dict(result=result, attempt=attempt, error=error, recipient=event["recipient"],
                  cc=event.get("cc"), message_id=_message_id(event, None), pdf_sha256=pdf_digest,
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
            and settings.get("transport", "mock") == event.get("transport", "mock")
            and settings.get("routing") == event.get("routing")
            and (event.get("transport") != "production" or event.get("cc") == FINANCE_COPY_ADDRESS))


def _smtp_audit(repository, event, attempt, result, **detail):
    repository.connection.execute(
        """INSERT INTO revenue_audit_events(uid,action,aggregate_type,aggregate_uid,after_json,actor_user_id,occurred_at)
           VALUES(?,?,'invoice',?,?,?,?)""",
        (new_uid("audit"), ATTEMPTED, event["uid"], canonical_json({
            "result": result, "attempt": attempt, "recipient": event["recipient"], "cc": event.get("cc"),
            "document_uid": event["document_uid"], "transport": event.get("transport","smtp"),
            "settings_revision":event["settings_revision"], **detail,
        }), event["actor"], repository.now()),
    )


def payer_invoice_approved(connection, event, simulation):
    """Require the immutable issuance audit and the current payer binding."""
    if event.get("routing") != "payer" or event.get("cc") != FINANCE_COPY_ADDRESS:
        return False
    row = connection.execute(
        """SELECT i.id,i.revenue_entity_id,a.action
             FROM revenue_invoices i JOIN revenue_documents d ON d.id=i.document_id
             JOIN revenue_entities e ON e.id=i.revenue_entity_id
             JOIN revenue_audit_events a ON a.aggregate_uid=i.uid
            WHERE i.uid=? AND i.revenue_entity_id=? AND i.status='issued' AND e.status='active'
              AND d.uid=? AND d.document_kind=? AND d.sha256=?
              AND a.action IN (?,?) AND json_extract(a.after_json,'$.simulation')=?""",
        (event["uid"], event["entity_id"], event["document_uid"], FORMAL_INVOICE_KIND,
         event["attachment_sha256"], *ISSUE_ACTIONS, int(simulation)),
    ).fetchone()
    if not row:
        return False
    try:
        payer_mail_addresses(event["recipient"], simulation=simulation)
    except ValueError:
        return False
    if row["action"] == ISSUE_ACTIONS[1]:
        return bool(connection.execute(
            """SELECT 1 FROM revenue_authority_invoices a JOIN revenue_authority_payers p
                   ON p.state_code=a.state_code AND p.revenue_entity_id=?
                WHERE a.invoice_id=? AND NOT EXISTS (
                    SELECT 1 FROM revenue_audit_events t
                    WHERE t.action='institution_commission_terms_confirmed'
                      AND t.aggregate_uid=a.contract_draft_uid
                      AND json_extract(t.after_json,'$.payment_mode')='rcti_quarterly'
                )""", (event["entity_id"], row["id"]),
        ).fetchone())
    return bool(connection.execute(
        """SELECT 1 FROM revenue_invoice_lines l JOIN revenue_claims c ON c.uid=l.source_uid
            WHERE l.invoice_id=? AND l.source_kind='claim' AND c.status='issued'
              AND c.revenue_entity_id=?
              AND NOT EXISTS (SELECT 1 FROM revenue_invoice_lines bad
                  LEFT JOIN revenue_claims bc ON bc.uid=bad.source_uid
                  WHERE bad.invoice_id=l.invoice_id AND (bad.source_kind!='claim'
                    OR bc.id IS NULL OR bc.revenue_entity_id!=c.revenue_entity_id OR bc.status!='issued'))""",
        (row["id"], event["entity_id"]),
    ).fetchone())


def _smtp_invoice_approved(connection, event, transport):
    if transport.recipient is None:
        return payer_invoice_approved(connection, event, transport.simulation)
    if event.get("routing") == "payer":
        return False
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
    if (not _settings_match(connection, event)
            or (transport.recipient is not None and event["recipient"] != transport.recipient)):
        error = "settings_changed"
    elif not _smtp_invoice_approved(connection,event,transport):
        error = "smtp_invoice_not_approved"
    elif transport.limit is not None and prior[0] >= transport.limit and not prior[1]:
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
    accepted_at = repository.now() if result == "sent" else None
    attempt = row["attempt_count"]+1
    for retry in range(5):
        try:
            with repository.transaction():
                update = connection.execute(
                    """UPDATE revenue_outbox_events SET last_error_code=?,dispatched_at=?
                       WHERE id=? AND attempt_count=? AND last_error_code='smtp_inflight'""",
                    (error,accepted_at,row["id"],attempt),
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


def dispatch_next(repository, root, eligible, *, smtp=None, mock_writable_mount=None):
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
                storage = {"mock_writable_mount":mock_writable_mount} if mock_writable_mount else {}
                result = _dispatch(repository, root, row, event, **storage)
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
