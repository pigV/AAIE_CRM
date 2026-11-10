"""Screens for the isolated school-level commission invoice workflow."""

from datetime import datetime, timezone
from html import escape
from zoneinfo import ZoneInfo
from urllib.parse import quote, urlencode

from revenue.views import _aud_money, _hidden_security, _toorak_settlement_controls


URL = "/revenue/school-commission-invoices"


def _hidden(name, value):
    return f'<input type="hidden" name="{name}" value="{escape(str(value), quote=True)}">'


def _claim_table(lines, *, selectable=False):
    select_header = '<th scope="col">选择</th>' if selectable else ""
    rows = []
    for line in lines:
        checkbox = ""
        if selectable:
            uid = escape(line["claim_uid"], quote=True)
            label = escape("选择 " + line["student_id"], quote=True)
            checkbox = f'<td><input type="checkbox" name="claim_uids" value="{uid}" aria-label="{label}"></td>'
        rows.append(
            f'<tr>{checkbox}<td>{escape(line["student_id"])}</td><td>{escape(line["claim_number"])}</td>'
            f'<td>{line["rate"]}%</td><td>{_aud_money(line["net_cents"])}</td>'
            f'<td>{_aud_money(line["gst_cents"])}</td><td>{_aud_money(line["gross_cents"])}</td></tr>'
        )
    return (f'<table><thead><tr>{select_header}<th>学生</th><th>Claim</th><th>佣金档位</th>'
            f'<th>未税金额</th><th>GST</th><th>含税金额</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>')


def render_school_invoice_list(entities, entity_id, claims, invoices, csrf, token, *,
                               allow_invoicing=True, simulation=True, overdue_only=False, auto_mail_settings=None, smtp_recipient=None,
                               allow_preview=None, mail_release=None):
    options = "".join(
        f'<option value="{value}"{" selected" if value == entity_id else ""}>{escape(name)}</option>'
        for value, name in entities
    )
    select = f"""<form method="get" action="{URL}" class="revenue-action-form">
      <label>学校付款主体<select name="entity_id" required><option value="">请选择学校</option>{options}</select></label>
      <button type="submit">查看可开票学生</button></form>"""
    selection = ""
    if allow_preview is None:
        allow_preview = allow_invoicing
    if entity_id and allow_preview:
        if claims:
            selection = f"""<form method="post" action="{URL}/preview">
              {_hidden_security(csrf,token,"toorak-school-preview")}{_hidden("entity_id",entity_id)}
              {_claim_table(claims,selectable=True)}
              <button type="submit" class="primary">预览所选发票</button></form>"""
        else:
            selection = '<p>该付款主体目前没有可开票的 Claim。</p>'
    history = "".join(
        f'<li><a href="{URL}?invoice={quote(invoice["uid"],safe="")}">{escape(invoice["invoice_number"])}</a>'
        f' · {escape(invoice["issue_date"])} · {_aud_money(invoice["gross_cents"])}'
        f'{_invoice_payment_label(invoice.get("settlement"))}</li>'
        for invoice in invoices
    )
    if entity_id:
        filters = (
            f'<a href="{URL}?entity_id={entity_id}">全部</a> · '
            f'<a href="{URL}?entity_id={entity_id}&amp;overdue=1">仅逾期</a>'
        )
        heading = "逾期佣金发票" if overdue_only else "已签发佣金发票"
        history = f'<h2>{heading}</h2><p>{filters}</p>' + (f'<ul>{history}</ul>' if history else '<p>暂无匹配发票。</p>')
    mode = "隔离测试 · " if simulation else ""
    instruction = "选学校后勾选一笔或多笔已审核、尚未开票的 Claim。" if allow_invoicing else "开票已暂停，可查看已签发发票。"
    if allow_preview and not allow_invoicing:
        instruction = "当前只开放预览。"
    mail = ""
    if auto_mail_settings is not None:
        mail = (render_school_mail_release(entity_id,auto_mail_settings,csrf,token,claims,simulation)
                if mail_release else render_school_mail_settings(entity_id,auto_mail_settings,csrf,token,smtp_recipient))
    return f"""<section class="revenue-panel">
      <h1>学校佣金开票</h1><p>{mode}{instruction}</p>
      {select}{mail}{selection}{history}</section>"""


def render_school_invoice_preview(data, csrf, token, *, simulation=True, allow_issue=True):
    hidden = "".join(_hidden(k,v) for k,v in (
        ("entity_id",data["entity_id"]),("claim_uids",",".join(data["claim_uids"])),("preview_digest",data["digest"]),
    ))
    mode = "TEST ONLY · " if simulation else ""
    issue = '<button type="submit" class="primary">确认签发佣金发票</button>' if allow_issue else ""
    action = "issue" if allow_issue else "pdf"
    return f"""<section class="revenue-panel">
      <h1>学校佣金发票预览</h1>
      <p>{mode}{escape(data["bill_to"])} · 预览阶段尚未分配 COM 编号。</p>
      {_claim_table(data["lines"])}
      <p>未税 {_aud_money(data["net_cents"])} · GST {_aud_money(data["gst_cents"])}
      · 合计 <strong>{_aud_money(data["gross_cents"])}</strong></p>
      <form method="post" action="{URL}/{action}">
      {_hidden_security(csrf,token,"toorak-school-issue")}{hidden}
      <button type="submit" formaction="{URL}/pdf" formtarget="_blank">查看 PDF 草稿</button>
      {issue}</form>
      <a href="{URL}?entity_id={data["entity_id"]}">返回重新选择</a></section>"""


def render_school_invoice_detail(data, csrf, token, *, allow_settlement=True, simulation=True, reminder_records=(), auto_mail=None, show_mail=False, allow_mail_retry=False):
    number = escape(data["invoice_number"])
    document = quote(data["document_uid"],safe="")
    rows = "".join(
        f'<tr><td>{escape(line["student_id"])}</td><td>{escape(line["claim_number"])}</td>'
        f'<td>{_aud_money(line["gross_cents"])}</td><td>{_aud_money(line["received_cents"])}</td>'
        f'<td>{_aud_money(line["outstanding_cents"])}</td></tr>'
        for line in data["lines"]
    )
    mode = "TEST ONLY · " if simulation else ""
    controls = _toorak_settlement_controls({**data,"invoice_uid":data["uid"]},URL,csrf,token,
                                          allow_actions=allow_settlement)
    reminder = ""
    if allow_settlement and data["settlement"]["overdue_days"]:
        reminder = f"""<form method="post" action="{URL}/reminder" class="revenue-action-form">
          {_hidden_security(csrf,token,"reminder-preview")}{_hidden("invoice_uid",data["uid"])}
          <label>已核对的学校财务邮箱<input type="email" name="recipient" maxlength="254" required></label>
          <button type="submit">提醒学校 · 预览邮件</button></form>"""
    history = "".join(
        f'<li>{escape(record["occurred_at"])} · {escape(record["recipient"])}'
        f' · 当时未收 {_aud_money(record["outstanding_cents"])}'
        f' · {"模拟已发送" if record["simulation"] else "人工登记已发送"}</li>'
        for record in reminder_records
    )
    if history:
        history = f'<h2>催款记录</h2><ul>{history}</ul>'
    mail = render_school_mail_status(data["uid"],auto_mail,csrf,token,allow_mail_retry) if show_mail else ""
    return f"""<section class="revenue-panel">
      <h1>学校佣金发票 {number}</h1>
      <p>{mode}学校付款主体 {escape(data["legal_name"])} · <a href="/revenue/documents/{document}/download">下载 {number}</a>
      · <a href="{URL}?entity_id={data["revenue_entity_id"]}">返回学校开票</a></p>
      <table><thead><tr><th>学生</th><th>Claim</th><th>含税应收</th><th>已分配</th><th>未收</th></tr></thead>
      <tbody>{rows}</tbody></table><p>{_invoice_payment_label(data["settlement"])}</p>{mail}{reminder}{history}{controls}</section>"""


def _invoice_payment_label(settlement):
    if not settlement:
        return ""
    outstanding = _aud_money(settlement["outstanding_cents"])
    delivery = settlement["delivery"]
    if not delivery:
        return f" · 待确认学校收件 · 未收 {outstanding}"
    due = escape(delivery["due_date"])
    days = settlement["overdue_days"]
    overdue = f" · <strong>逾期 {days} 天</strong>" if days else ""
    return f" · 到期 {due} · 未收 {outstanding}{overdue}"


def render_school_reminder_center(invoices):
    if not invoices:
        return ""
    rows = "".join(
        f'<li>{escape(invoice["legal_name"])} · '
        f'<a href="{URL}?invoice={quote(invoice["uid"], safe="")}">{escape(invoice["invoice_number"])}</a>'
        f'{_invoice_payment_label(invoice["settlement"])}</li>'
        for invoice in invoices
    )
    return f'<section class="panel"><h2>学校佣金逾期提醒（{len(invoices)}）</h2><ul>{rows}</ul></section>'


def render_school_reminder_preview(invoice, draft, csrf, token, *, simulation):
    document = quote(draft["document_uid"], safe="")
    mail = ""
    if not simulation:
        recipient = quote(draft["recipient"], safe="@")
        query = escape(urlencode({"subject": draft["subject"], "body": draft["body"]}, quote_via=quote))
        mail = f'<p><a href="mailto:{recipient}?{query}">打开邮件客户端</a>，发送前请附上原发票 PDF。</p>'
    title = "TEST ONLY · 学校催款邮件预览" if simulation else "学校催款邮件预览"
    confirmation = "我已模拟发送" if simulation else "我已核对收件人并发送邮件"
    fields = "".join(_hidden(key, draft[key]) for key in ("invoice_uid", "recipient", "digest"))
    back = quote(invoice["uid"], safe="")
    return f"""<section class="revenue-panel">
      <h1>{title}</h1>
      <p>收件人 {escape(draft["recipient"])}</p>
      <p>主题 {escape(draft["subject"])}</p>
      <pre>{escape(draft["body"])}</pre>
      <p><a href="/revenue/documents/{document}/download">下载原发票 PDF</a></p>
      {mail}
      <form method="post" action="{URL}/reminder-sent">
        {_hidden_security(csrf,token,"reminder-sent")}{fields}
        <label><input type="checkbox" name="confirmed" value="1" required>{confirmation}</label>
        <button type="submit">登记催款记录</button>
      </form>
      <a href="{URL}?invoice={back}">返回发票</a>
      </section>"""


def render_school_mail_settings(entity_id, settings, csrf, token, smtp_recipient=None):
    checked = " checked" if settings["enabled"] else ""
    status = "已启用" if settings["enabled"] else "已关闭"
    channel = "网易 SMTP 实收演练" if smtp_recipient else "本机模拟邮箱"
    recipient = (f'<p>固定实收地址 {escape(smtp_recipient)}</p>{_hidden("recipient",smtp_recipient)}'
                 if smtp_recipient else
                 f'<label>学校模拟财务邮箱<input type="email" name="recipient" maxlength="254" value="{escape(settings["recipient"])}"></label>')
    verified_label = "我已核对测试收件邮箱" if smtp_recipient else "我已核对本校财务邮箱"
    return f"""<form method="post" action="{URL}/mail-settings" class="revenue-action-form">
      <h2>自动发送发票 · TEST ONLY</h2>
      <p>当前{status}。通道 {channel}。学校收件日仍需单独确认。</p>
      {_hidden_security(csrf,token,"mail-settings")}{_hidden("entity_id",entity_id)}{_hidden("settings_revision",settings["revision"])}
      {recipient}
      <label><input type="checkbox" name="verified" value="1">{verified_label}</label>
      <label><input type="checkbox" name="enabled" value="1"{checked}>签发后自动发送</label>
      <button type="submit">保存发信设置</button></form>"""


def _mail_time(value):
    if not value:
        return "—"
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return "—"
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(ZoneInfo("Australia/Melbourne")).strftime("%d/%m/%Y %H:%M:%S %Z")


MAIL_STATUS_LABELS = {
    "pending": "待发送", "sending": "发送中", "sent": "发送成功",
    "failed": "发送失败", "cancelled": "发送已取消", "needs_review": "发送结果待核对",
}


def render_school_mail_status(invoice_uid, mail, csrf, token, allow_retry, *, attachment_href=None):
    if not mail:
        return "<p>发票邮件 · 未安排自动发送。签发时需已启用本校自动发信并核对财务邮箱。</p>"
    labels = {"pending":"待模拟发送","sent":"已写入模拟邮箱","failed":"模拟发送失败","cancelled":"学校邮箱设置已变更，发送已取消","needs_review":"模拟邮箱记录待核对"}
    if mail.get("transport") in {"smtp","candidate","production"}:
        labels.update(pending="待发送",sending="发送中",sent="网易已接受",failed="发信失败",
                      cancelled="发送已取消",needs_review="发送结果待核对")
    history = "".join(
        f'<li>{_mail_time(record["occurred_at"])} · 第 {int(record["attempt"])} 次 · '
        f'{labels.get(record["result"], "待核对")} · {escape(record["recipient"])}'
        f'{f" · {escape(record['error'])}" if record.get("error") else ""}'
        f' · {escape(record.get("message_id") or "")} · {escape(record.get("pdf_sha256") or "")}</li>'
        for record in mail["attempts"]
    )
    retry = ""
    if allow_retry and mail["status"] == "failed":
        retry = f"""<form method="post" action="{URL}/mail-retry">
          {_hidden_security(csrf,token,"mail-retry")}{_hidden("invoice_uid",invoice_uid)}
          {_hidden("attempt_count",mail["attempt_count"])}
          <button type="submit">重试发票邮件</button></form>"""
    title = "发票邮件" if mail.get("transport") == "production" else "发票邮件 · TEST ONLY"
    copy_line = f'<p>抄送 {escape(mail["cc"])}</p>' if mail.get("cc") else ""
    archived = mail.get("message")
    content = "<p>邮件主题和正文未存档。</p>"
    if archived:
        state = "邮件内容存档" if mail["sent_at"] else "入队时保存的邮件内容"
        content = f"""<details open><summary>{state}</summary>
          <p>发件人 {escape(archived["sender"])}</p><p>主题 {escape(archived["subject"])}</p>
          <pre class="revenue-mail-body">{escape(archived["body"])}</pre></details>"""
    document_uid = quote(mail["document_uid"], safe="")
    attachment_name = archived["attachment_name"] if archived else f'{mail["invoice_number"]}.pdf'
    return f"""<section id="invoice-mail-history"><h2>{title}</h2>
      <p>{labels[mail["status"]]} · 收件人 {escape(mail["recipient"])} · 尝试 {mail["attempt_count"]} 次</p>
      {copy_line}<p>入队时间 {_mail_time(mail["queued_at"])} · 发送时间 {_mail_time(mail["sent_at"])}</p>
      <p>附件 <a href="{escape(attachment_href or f'/revenue/documents/{document_uid}/download', quote=True)}">{escape(attachment_name)}</a></p>
      {content}<h3>发送尝试</h3>{f'<ul>{history}</ul>' if history else '<p>尚无发送尝试。</p>'}
      {retry}</section>"""


def render_school_mail_release(entity_id, settings, csrf, token, claims, simulation):
    mode = "candidate" if simulation else "production"
    current = settings if settings.get("transport") == mode else {}
    stage = current.get("stage","off")
    caption = {"off":"关闭","verified":"邮箱已核对，发送关闭","first":"已批准一张新发票"}[stage]
    options = "".join(
        f'<label><input type="checkbox" name="mail_claim_uids" value="{escape(claim["claim_uid"])}">'
        f'{escape(claim["student_id"])} · {escape(claim["claim_number"])}</label>' for claim in claims
    )
    return f"""<form method="post" action="{URL}/mail-settings" class="revenue-action-form">
      <h2>Toorak 发票邮件试点</h2><p>当前{caption}。收件日仍需单独确认。</p>
      <p>{"隔离演练仅投递到指定 Gmail。" if simulation else "使用已确认的学校财务邮箱。"}</p>
      {_hidden_security(csrf,token,"mail-release")}{_hidden("entity_id",entity_id)}
      {_hidden("settings_revision",settings["revision"])}
      <label>学校财务邮箱<input type="email" name="recipient" maxlength="254" value="{escape(current.get("recipient",""))}"></label>
      <label>核对依据<input name="evidence" maxlength="200" value="{escape(current.get("evidence",""))}"></label>
      <label>单独批准记录<input name="approval" maxlength="200"></label>
      <fieldset><legend>首张新发票对应的申领</legend>{options}</fieldset>
      <button type="submit" name="mail_stage" value="verified">保存已核对邮箱</button>
      <button type="submit" name="mail_stage" value="first">批准一张新发票</button>
      <button type="submit" name="mail_stage" value="off">关停并隔离待发邮件</button></form>"""
