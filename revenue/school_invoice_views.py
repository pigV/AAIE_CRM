"""Screens for the isolated school-level commission invoice workflow."""

from datetime import datetime, timezone
from html import escape
import json
from zoneinfo import ZoneInfo
from urllib.parse import quote, urlencode

from revenue.views import _aud_money, _hidden_security, _toorak_settlement_controls
from ui import ASSET_VERSION


URL = "/revenue/school-commission-invoices"
STYLESHEET = f'<link rel="stylesheet" href="/assets/revenue.css?v={ASSET_VERSION}">'


def _hidden(name, value):
    return f'<input type="hidden" name="{name}" value="{escape(str(value), quote=True)}">'


def _claim_table(lines, *, selectable=False, selected=()):
    select_header = '<th scope="col">选择</th>' if selectable else ""
    rows = []
    for line in lines:
        checkbox = ""
        if selectable:
            uid = escape(line["claim_uid"], quote=True)
            label = escape("选择 " + (line.get("student_name", line["student_id"])), quote=True)
            checked = " checked" if line["claim_uid"] in selected else ""
            checkbox = (
                f'<td><input type="checkbox" name="claim_uids" value="{uid}" aria-label="{label}"'
                f' data-invoice-net="{int(line["net_cents"])}"'
                f' data-invoice-gst="{int(line["gst_cents"])}"{checked}></td>'
            )
        rows.append(
            f'<tr>{checkbox}<td>{escape(line.get("student_name", line["student_id"]))}</td>'
            f'<td>{escape(line["claim_number"] or "手工明细")}</td>'
            f'<td>{escape(str(line["rate"]) + "%" if line["claim_uid"] else " · ".join(dict.fromkeys(filter(None, (line["product"], line["description"])))))}</td><td>{_aud_money(line["net_cents"])}</td>'
            f'<td>{_aud_money(line["gst_cents"])}</td><td>{_aud_money(line["gross_cents"])}</td></tr>'
        )
    return (f'<table><thead><tr>{select_header}<th>学生</th><th>Claim</th><th>项目 / 佣金档位</th>'
            f'<th>未税金额</th><th>GST</th><th>含税金额</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>')


def _composer_lines(claims, selected_claims, students, saved_rows):
    from revenue.school_invoice_manual import MAX_LINES

    rows = []
    for line in claims:
        uid = escape(line["claim_uid"], quote=True)
        name = escape(line.get("student_name") or line["student_id"], quote=True)
        checked = " checked" if line["claim_uid"] in selected_claims else ""
        rows.append(f"""<tr>
          <td><label class="invoice-source invoice-source-claim"><input type="checkbox" name="claim_uids" value="{uid}" aria-label="选择 {name}"
            data-invoice-net="{int(line['net_cents'])}" data-invoice-gst="{int(line['gst_cents'])}"
            data-invoice-terms="{escape(str(line.get('payment_days', '')), quote=True)}"{checked}>学生佣金</label></td>
          <td>招生佣金</td><td>{escape(line["claim_number"])}</td><td>{name}</td>
          <td>1</td><td>{_aud_money(line["net_cents"])}</td><td>{_aud_money(line["net_cents"])}</td>
          <td>{_aud_money(line["gst_cents"])}</td><td class="invoice-line-gross">{_aud_money(line["gross_cents"])}</td><td></td></tr>""")

    def selected(value, current):
        return " selected" if value == current else ""

    if students is not None:
        for index in range(MAX_LINES):
            values = saved_rows[index] if index < len(saved_rows) else {}
            visibility = "" if index == 0 or values else " hidden"
            attributes = {key: escape(values.get(key, ""), quote=True)
                          for key in ("product", "description", "unit_price")}
            options = "".join(
                f'<option value="{escape(student_id, quote=True)}"{selected(student_id, values.get("student_id"))}>'
                f'{escape(name)}</option>'
                for student_id, name in students.items()
            )
            quantity = escape(values.get("quantity") or "1", quote=True)
            gst = values.get("gst")
            rows.append(f"""<tr data-invoice-manual-row{visibility}>
              <td><span class="invoice-source invoice-source-manual">手工服务</span></td>
              <td><div class="invoice-product-selector">
                <input aria-label="项目" name="manual_product_{index}" maxlength="100" value="{attributes['product']}" placeholder="输入产品或服务">
                <select aria-label="选择产品或服务" data-invoice-service-select hidden><option value="">选择服务</option></select>
              </div></td>
              <td><input aria-label="说明" name="manual_description_{index}" maxlength="300" value="{attributes['description']}"></td>
              <td><select aria-label="关联学生" name="manual_student_id_{index}"><option value="">不关联学生</option>{options}</select></td>
              <td><input aria-label="数量" type="number" name="manual_quantity_{index}" value="{quantity}" min="1" max="1000"></td>
              <td><input aria-label="未税单价（AUD）" type="number" name="manual_unit_price_{index}"
                value="{attributes['unit_price']}" min="0.01" max="1000000" step="0.01"></td>
              <td><output data-invoice-line-net>A$0.00</output></td>
              <td><select aria-label="GST" name="manual_gst_{index}"><option value="">请选择</option>
                <option value="taxable"{selected('taxable', gst)}>GST 10%</option>
                <option value="no_gst"{selected('no_gst', gst)}>不计 GST</option></select></td>
              <td class="invoice-line-gross"><output data-invoice-line-gross>A$0.00</output></td>
              <td><button type="button" data-invoice-manual-remove aria-label="移除此行">×</button></td>
              </tr>""")
    if not rows:
        rows.append('<tr><td colspan="10" class="invoice-line-empty">选择学校后可添加发票明细。</td></tr>')
    return f"""<div class="invoice-composer-table">
      <table class="invoice-line-table"><thead><tr>
      <th>来源</th><th>产品 / 服务</th><th>说明</th><th>关联学生</th>
      <th>数量</th><th>未税单价</th><th>未税金额</th><th>GST</th><th>含税金额</th><th></th>
      </tr></thead><tbody>{''.join(rows)}</tbody></table></div>"""


def _payment_terms_field(payment_terms):
    from revenue.school_invoice_manual import DEFAULT_PAYMENT_TERMS

    current = payment_terms or str(DEFAULT_PAYMENT_TERMS)
    options = "".join(
        f'<option value="{days}"{" selected" if days == current else ""}>{label}</option>'
        for days, label in (("0", "开票当天"), ("7", "Net 7"), ("15", "Net 15"), ("30", "Net 30"))
    )
    return f'<label>付款期限<select name="manual_terms">{options}</select></label>'


def render_school_invoice_account(school_id, name, billing, csrf, token, *, authority=""):
    def field(key, label, value, required=False):
        return (f'<label>{label}<input name="{key}" value="{escape(value or "", quote=True)}"'
                + (' required' if required else '') + '></label>')

    fields = (
        field("billing_name", "发票抬头", billing.get("name") or name, True)
        + f'<label>账单地址<textarea name="billing_address" rows="2" required>{escape(billing.get("address") or "")}</textarea></label>'
        + field("billing_phone", "联系电话", billing.get("phone"))
        + field("recipient", "学校收票邮箱", billing.get("recipient"), True)
        + field("cc", "抄送", billing.get("cc"))
        + field("bcc", "密送", billing.get("bcc"))
    )
    return f"""{STYLESHEET}<section class="invoice-composer">
      <header class="invoice-composer-title"><h1>补齐开票资料</h1></header>
      <form class="invoice-composer-card" method="post" action="{URL}/account">
        <h2>{escape(name)}</h2>
        {_authority_label(authority)}
        {_hidden_security(csrf,token,"school-invoice-account")}{_hidden("school_id",school_id)}
        {_hidden("billing_revision",billing.get("revision",""))}
        {_hidden("settings_revision",billing.get("settings_revision",""))}
        {fields}
        <label><input type="checkbox" name="verified" value="1" {"checked" if billing.get("verified") else ""}>已核对学校财务邮箱</label>
        <label><input type="checkbox" name="enabled" value="1" {"checked" if billing.get("enabled") else ""}>允许从 CRM 发送发票</label>
        <div class="invoice-composer-actions"><a href="{URL}">返回学校选择</a><button class="primary">保存并继续</button></div>
      </form></section>"""


def _authority_label(authority):
    return f'<span class="invoice-school-status">{escape(authority)}</span>' if authority else ""


def _contract_badge(contracted, authority):
    if authority:
        status = "教育局合同已确认" if contracted else "教育局合同未确认"
    else:
        status = "有佣金合同" if contracted else "无佣金合同"
    value = "contract" if contracted else "no-contract"
    return f'<span class="invoice-school-status {value}">{status}</span>'


def _school_picker(schools, selected=None, institutions=()):
    rows = []
    for school_id, name, _, contracted, authority in schools:
        value = "contract" if contracted else "no-contract"
        rows.append(
            f'<li data-school-row data-contract="{value}"><a href="{URL}?school_id={school_id}">'
            f'{escape(name)} <span>{_authority_label(authority)} {_contract_badge(contracted, authority)}</span></a></li>'
        )
    for name, contract_uid in institutions:
        destination = f"/revenue/institution-commission-drafts/{quote(contract_uid, safe='')}"
        rows.append(
            f'<li data-school-row data-contract="contract"><a href="{destination}">'
            f'{escape(name)} <span>{_authority_label("高等教育机构")} '
            '<span class="invoice-school-status contract">合同已确认</span></span></a></li>'
        )
    caption = "选择开票学校或机构"
    if selected:
        _, name, _, contracted, authority = selected
        caption = f"{escape(name)} {_authority_label(authority)} {_contract_badge(contracted, authority)}"
    return f"""<details data-school-picker>
      <summary aria-label="选择开票学校或机构"><span>{caption}</span><span aria-hidden="true">⌄</span></summary>
      <div class="invoice-school-popover">
      <label>搜索学校或机构名称<input type="search" data-school-search></label>
      <label>合同状态<select data-school-contract>
        <option value="">全部</option><option value="contract">有合同</option><option value="no-contract">无合同</option>
      </select></label>
      <ul class="invoice-school-list">{"".join(rows)}</ul>
      <p data-school-no-results hidden>未找到匹配的学校或机构。</p></div>
      </details>"""


def _invoice_service_directory(services, csrf, token, allow_manage):
    config = json.dumps(services, ensure_ascii=False).replace("<", r"\u003c")
    picker = f'<script type="application/json" data-invoice-services>{config}</script>'
    if not allow_manage:
        return picker
    return picker + f"""<dialog class="invoice-service-directory" aria-labelledby="invoice-service-title">
      <header class="invoice-composer-heading"><div><h2 id="invoice-service-title">产品／服务管理</h2>
        <p>目录可在任何学校的发票上搜索和选用。</p></div>
        <button type="button" data-invoice-service-close>返回开票</button></header>
      <div class="invoice-composer-heading">
        <label>搜索产品或服务<input type="search" data-invoice-service-filter></label>
        <button type="button" data-invoice-service-new>＋ 新增产品／服务</button>
      </div>
      <table class="invoice-service-table"><thead><tr><th>名称</th><th>常用说明</th><th>操作</th></tr></thead>
        <tbody data-invoice-service-rows></tbody></table>
      <form method="post" action="{URL}/services" data-invoice-service-form hidden>
        {_hidden_security(csrf,token,"school-invoice-service")}
        <input type="hidden" name="service_uid"><input type="hidden" name="service_revision">
        <div class="invoice-service-fields">
          <label>产品／服务名称<input name="service_name" maxlength="100" required></label>
          <label>常用说明（可选）<input name="service_description" maxlength="300"></label>
        </div>
        <button type="submit" class="primary">保存到通用目录</button>
      </form>
      <p role="status" data-invoice-service-status></p>
    </dialog>"""


def render_school_invoice_list(entities, entity_id, claims, invoices, csrf, token, *,
                               allow_invoicing=True, simulation=True, overdue_only=False, auto_mail_settings=None, smtp_recipient=None,
                               allow_preview=None, mail_release=None, show_mail_history=False, manual_students=None,
                               candidate_href=None, selected_claims=(), manual_draft=(), manual_terms="", draft_note="", billing=None, schools=None, school_id=None, institutions=(), services=None):
    options = "".join(
        f'<option value="{value}"{" selected" if value == entity_id else ""}>{escape(name)}</option>'
        for value, name in entities
    )
    select = f"""<form method="get" action="{URL}" class="revenue-action-form invoice-composer-school">
      <label>学校 / 开票主体<select name="entity_id" required><option value="">请选择学校</option>{options}</select></label>
      <button type="submit">继续开票</button></form>"""
    billing_edit = ""
    if schools is not None:
        selected_school = next((row for row in schools if row[0] == school_id), None)
        select = _school_picker(schools, selected_school, institutions)
        if selected_school:
            billing_edit = f'<a href="{URL}?school_id={school_id}&amp;edit_billing=1">编辑开票资料</a>'
    selection = ""
    if allow_preview is None:
        allow_preview = allow_invoicing
    awaiting_school = not entity_id and allow_preview
    if allow_preview:
        if awaiting_school or claims or manual_students is not None:
            candidate_link = (f'<a href="{escape(candidate_href, quote=True)}">查看学生和申领条件</a>'
                              if candidate_href else "")
            lines = _composer_lines(claims, selected_claims, manual_students, manual_draft)
            manual = manual_students is not None
            claim_button = ('<button type="button" data-invoice-select-claim>＋ 添加待开票学生佣金</button>' if claims else
                            '<button type="button" disabled title="这所学校暂无待开票的学生佣金">＋ 添加待开票学生佣金</button>')
            add_button = '<button type="button" data-invoice-manual-add>＋ 添加手工明细</button>' if manual else ""
            clear_button = '<button type="button" data-invoice-clear>清除明细</button>' if manual else ""
            if awaiting_school:
                claim_button = '<button type="button" disabled>＋ 添加待开票学生佣金</button>'
                add_button = '<button type="button" disabled>＋ 添加手工明细</button>'
            disabled = " disabled" if awaiting_school else ""
            issue_buttons = (f'<button type="submit" name="preview_action" value="issue"{disabled}>仅签发</button>'
                             f'<button type="submit" name="preview_action" value="send"{disabled}>签发并发送</button>'
                             if allow_invoicing else "")
            bill = billing or {}
            issuer_date = datetime.now(ZoneInfo("Australia/Melbourne")).date()
            bill_to = f'{escape(bill.get("name") or "未填写")}<br>{escape(bill.get("address") or "").replace(chr(10), "<br>")}'
            recipient = escape(bill.get("recipient") or ("—" if awaiting_school else "未填写"))
            copies = (f'<p>抄送 {escape(bill.get("cc") or "—")}</p>'
                      f'<p>密送 {escape(bill.get("bcc") or "—")}</p>')
            inline_picker = select if awaiting_school or schools is not None else ""
            pending_message = (
                "选好学校后，可以添加手工明细或勾选待开票学生佣金。"
                if awaiting_school else "只有从待开票学生佣金添加的明细，才会在签发时更新申领状态。"
            )
            due_label = "—" if awaiting_school else "以预览为准"
            bill_details = "" if awaiting_school else f"""<details class="invoice-layout-bill-details">
                  <summary>展开开票资料</summary>
                  <div><span class="invoice-field-label">发票抬头和地址</span><p>{bill_to}</p>{copies}{billing_edit}</div>
                </details>"""
            school_control = inline_picker or f'<output>{escape(next((name for value, name in entities if value == entity_id), ""))}</output>'
            details = f"""<section class="invoice-composer-card invoice-layout-billing">
              <div class="invoice-layout-school">
                <div><span class="invoice-field-label">开票学校</span>{school_control}</div>
                <div><span class="invoice-field-label">到期日</span><output data-invoice-due>{due_label}</output></div>
              </div>
              <div class="invoice-layout-fields">
                <div><span class="invoice-field-label">收票邮箱</span><output>{recipient}</output></div>
                <div><span class="invoice-field-label">开票日期</span><output data-invoice-date="{issuer_date.isoformat()}">{issuer_date:%d/%m/%Y}</output></div>
                <div>{_payment_terms_field(manual_terms) if manual else '<span class="invoice-field-label">付款期限</span><output>—</output>'}
                    <output data-invoice-claim-terms hidden></output></div>
                {bill_details}
              </div>
            </section>"""
            if inline_picker:
                select = ""
            if awaiting_school:
                start, end = '<section aria-label="发票工作台">', '</section>'
            else:
                start = (f'<form method="post" action="{URL}/preview" data-invoice-composer>'
                         f'{_hidden_security(csrf, token, "toorak-school-preview")}{_hidden("entity_id", entity_id)}')
                end = '</form>'
            selection = f"""{start}
              {details}
              <section class="invoice-composer-card">
                <header class="invoice-composer-heading"><h2>发票明细</h2>
                  <div class="invoice-composer-buttons">{claim_button}{add_button}</div></header>
                {lines}
                <div class="invoice-line-actions">{clear_button}{candidate_link}</div>
                <p>{pending_message}</p>
              </section>
              <section class="invoice-composer-card invoice-composer-summary">
                <label>给学校的备注（选填）<textarea name="customer_note" maxlength="500" rows="2"{disabled}>{escape(draft_note)}</textarea></label>
                <dl data-invoice-totals aria-label="发票金额">
                  <div><dt>未税小计</dt><dd data-invoice-total-net>A$0.00</dd></div>
                  <div><dt>GST</dt><dd data-invoice-total-gst>A$0.00</dd></div>
                  <div><dt>应收合计</dt><dd data-invoice-total-gross>A$0.00</dd></div>
                </dl>
              </section>
              <div class="invoice-composer-actions"><a href="/revenue">‹ 返回收入与佣金</a>
                <div class="invoice-composer-buttons">
                  <button type="submit"{disabled}>预览 Invoice</button>
                  <button type="submit" class="primary" name="preview_action" value="pdf" formtarget="_blank"{disabled}>预览 PDF</button>
                  {f'<span class="invoice-layout-issue">{issue_buttons}</span>' if issue_buttons else ""}
                </div></div>{end}"""
        elif entity_id:
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
    instruction = "选好学校后，可以添加手工明细或勾选待开票学生佣金。" if allow_invoicing else "开票已暂停，可查看已签发发票。"
    if allow_preview and not allow_invoicing:
        instruction = "当前只开放预览。"
    mail = ""
    if auto_mail_settings is not None:
        mail = (render_school_mail_release(entity_id,auto_mail_settings,csrf,token,claims,simulation)
                if mail_release else render_school_mail_settings(entity_id,auto_mail_settings,csrf,token,smtp_recipient))
    mail_history = ""
    if show_mail_history:
        suffix = f"?entity_id={entity_id}" if entity_id else ""
        mail_history = f'<p><a href="{URL}/mail-history{suffix}">发票发送历史</a></p>'
    picker_panel = f'<div class="invoice-composer-card"><h2>开票资料</h2>{select}{billing_edit}<p>{mode}{instruction}</p></div>' if select else ""
    return f"""{STYLESHEET}<section class="invoice-composer invoice-layout-v4">
      <header class="invoice-composer-title">
        <div><p>收入与佣金 / 学校佣金开票</p><h1>开学校 Invoice</h1></div>{mail_history}
      </header>
      {picker_panel}
      {mail}{selection}{history}
      {_invoice_service_directory(services, csrf, token, allow_invoicing) if services is not None else ""}
      <script src="/assets/school-invoice-composer.js?v={ASSET_VERSION}" defer></script></section>"""


def _invoice_draft_fields(data):
    hidden = "".join(_hidden(k,v) for k,v in (
        ("entity_id",data["entity_id"]),("claim_uids",",".join(data["claim_uids"])),("preview_digest",data["digest"]),
        ("customer_note",data.get("customer_note","")),
    ))
    if data.get("manual_lines"):
        hidden += _hidden("manual_lines", json.dumps(data["manual_lines"], ensure_ascii=False))
        hidden += _hidden("manual_terms", data.get("manual_terms") if data.get("manual_terms") is not None else "")
    return hidden


def render_school_invoice_pdf_view(data, csrf, token, *, image, page_number, page_count):
    previous = f'<button type="submit" name="draft_page" value="{page_number - 1}">上一页</button>' if page_number > 1 else ""
    following = f'<button type="submit" name="draft_page" value="{page_number + 1}">下一页</button>' if page_number < page_count else ""
    if image:
        preview = (f'<p class="invoice-pdf-page-number">第 {page_number} 页，共 {page_count} 页</p>'
                   f'<img class="invoice-pdf-page" src="data:image/png;base64,{image}" alt="发票草稿第 {page_number} 页">')
    else:
        preview = '<p role="status">票面暂时无法显示。你可以重试，或下载 PDF 草稿查看。</p>'
        previous = '<button type="submit">重试预览</button>'
        following = ""
    return f"""{STYLESHEET}<section class="invoice-composer invoice-pdf-view">
      <header class="invoice-composer-title"><div><p>{escape(data["bill_to"])}</p><h1>PDF 草稿预览</h1></div></header>
      <form method="post" action="{URL}/pdf" class="invoice-composer-actions invoice-pdf-actions">
        {_hidden_security(csrf,token,"toorak-school-pdf")}{_invoice_draft_fields(data)}
        <div class="invoice-composer-buttons">{previous}{following}</div>
        <button type="submit" class="primary" name="draft_format" value="download" formtarget="_blank">下载 PDF 草稿</button>
      </form>
      {preview}
      </section>"""


def render_school_invoice_preview(data, csrf, token, *, simulation=True, allow_issue=True, allow_send=False, intent=""):
    hidden = _invoice_draft_fields(data)
    if data.get("manual_lines"):
        hidden += _hidden("draft_token", token)
    mode = "TEST ONLY · " if simulation else ""
    issue = '<button type="submit" class="primary">确认签发佣金发票</button>' if allow_issue else ""
    if allow_issue and allow_send:
        issue = (
            '<button type="submit">仅签发</button>'
            '<button type="submit" class="primary" name="send_invoice" value="1">签发并发送</button>'
        )
    if intent == "issue" and allow_issue:
        issue = '<button type="submit" class="primary">确认仅签发</button>'
    elif intent == "send":
        issue = ('<button type="submit" class="primary" name="send_invoice" value="1">确认签发并发送</button>'
                 if allow_issue and allow_send else '<p>当前无法发送，请核对学校邮件设置。</p>')
    action = "issue" if allow_issue else "pdf"
    terms = ""
    if data.get("due_date"):
        terms = f'<p>开票日 {escape(data["issue_date"])} · 到期日 {escape(data["due_date"])}</p>'
    if copies := data.get("mail_preview"):
        from revenue.school_invoice_release import FINANCE_COPY_ADDRESS

        terms += (f'<p>收件人 {escape(copies["recipient"] or "未填写")} · '
                  f'Cc {escape(", ".join(filter(None, (FINANCE_COPY_ADDRESS, copies["cc"]))))} · '
                  f'Bcc {escape(copies["bcc"] or "未填写")}</p>')
        if allow_send and (message := copies.get("message")):
            terms += (
                '<details open><summary>邮件预览</summary>'
                f'<p>主题 {escape(message["subject"])}</p>'
                f'<p>{escape(message["body"]).replace(chr(10), "<br>")}</p>'
                '<p>附件为发票 PDF。签发时会将 DRAFT 换成正式发票编号。</p></details>'
            )
    if note := data.get("customer_note"):
        terms += f'<p>客户备注 {escape(note)}</p>'
    return f"""{STYLESHEET}<section class="revenue-panel invoice-composer">
      <h1>学校佣金发票预览</h1>
      <p>{mode}{escape(data["bill_to"])} · 预览阶段尚未分配 COM 编号。</p>
      {_claim_table(data["lines"])}{terms}
      <p>未税 {_aud_money(data["net_cents"])} · GST {_aud_money(data["gst_cents"])}
      · 合计 <strong>{_aud_money(data["gross_cents"])}</strong></p>
      <form method="post" action="{URL}/{action}">
      {_hidden_security(csrf,token,"toorak-school-issue")}{hidden}
      <button type="submit" formaction="{URL}/edit">返回编辑</button>
      <button type="submit" formaction="{URL}/pdf" formtarget="_blank">查看 PDF 草稿</button>
      {issue}</form></section>"""


def render_school_invoice_detail(data, csrf, token, *, allow_settlement=True, simulation=True, reminder_records=(), auto_mail=None, show_mail=False, allow_mail_retry=False, reminder_direct=False, reminder_mails=()):
    number = escape(data["invoice_number"])
    document = quote(data["document_uid"],safe="")
    rows = "".join(
        f'<tr><td>{escape(line["student_id"])}</td><td>{escape(line["claim_number"])}</td><td>{escape(line["description"])}</td>'
        f'<td>{_aud_money(line["gross_cents"])}</td><td>{_aud_money(line["received_cents"])}</td>'
        f'<td>{_aud_money(line["outstanding_cents"])}</td></tr>'
        for line in data["lines"]
    )
    mode = "TEST ONLY · " if simulation else ""
    controls = _toorak_settlement_controls({**data,"invoice_uid":data["uid"]},URL,csrf,token,
                                          allow_actions=allow_settlement)
    reminder = ""
    if allow_settlement and data["settlement"]["overdue_days"]:
        recipient_input = ("" if reminder_direct else
                           '<label>已核对的学校财务邮箱<input type="email" name="recipient" maxlength="254" required></label>')
        reminder = f"""<form method="post" action="{URL}/reminder" class="revenue-action-form">
          {_hidden_security(csrf,token,"reminder-preview")}{_hidden("invoice_uid",data["uid"])}
          {recipient_input}
          <button type="submit">提醒学校 · 预览邮件</button></form>"""
    history = "".join(
        f'<li>{escape(record["occurred_at"])} · {escape(record["recipient"])}'
        f' · 当时未收 {_aud_money(record["outstanding_cents"])}'
        f' · {"模拟已发送" if record["simulation"] else "人工登记已发送"}</li>'
        for record in reminder_records
    )
    if history:
        history = f'<h2>催款记录</h2><ul>{history}</ul>'
    history += render_reminder_mail_records(
        data["uid"], reminder_mails, csrf, token, retry_action=URL + "/reminder-retry" if reminder_direct else "",
    )
    mail = render_school_mail_status(data["uid"],auto_mail,csrf,token,allow_mail_retry) if show_mail else ""
    return f"""<section class="revenue-panel">
      <h1>学校佣金发票 {number}</h1>
      <p>{mode}学校付款主体 {escape(data["legal_name"])} · <a href="/revenue/documents/{document}/download">下载 {number}</a>
      · <a href="{URL}?entity_id={data["revenue_entity_id"]}">返回学校开票</a></p>
      <table><thead><tr><th>学生</th><th>Claim / 来源</th><th>明细</th><th>含税应收</th><th>已分配</th><th>未收</th></tr></thead>
      <tbody>{rows}</tbody></table><p>{_invoice_payment_label(data["settlement"])}</p>{mail}{reminder}{history}{controls}</section>"""


def _invoice_payment_label(settlement):
    if not settlement:
        return ""
    outstanding = _aud_money(settlement["outstanding_cents"])
    delivery = settlement["delivery"]
    due_date = settlement.get("due_date") or (delivery["due_date"] if delivery else None)
    if not due_date:
        return f" · 待确认学校收件 · 未收 {outstanding}"
    due = escape(due_date)
    days = settlement["overdue_days"]
    overdue = f" · <strong>逾期 {days} 天</strong>" if days else ""
    return f" · 到期 {due} · 未收 {outstanding}{overdue}"


def render_school_reminder_center(invoices):
    if not invoices:
        return ""
    rows = []
    for invoice in invoices:
        href = invoice.get("href") or f'{URL}?invoice={quote(invoice["uid"], safe="")}'
        rows.append(f'<li>{escape(invoice["legal_name"])} · '
                    f'<a href="{escape(href, quote=True)}">{escape(invoice["invoice_number"])}</a>'
                    f'{_invoice_payment_label(invoice["settlement"])}</li>')
    rows = "".join(rows)
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


def render_queued_reminder_preview(draft, csrf, token, send_action, back_href, *, operation="", attachment_href=None):
    message = draft["message"]
    document = quote(draft["document_uid"], safe="")
    title = "TEST ONLY · 催款邮件预览" if draft["transport"] == "mock" else "催款邮件预览"
    return f"""<section class="revenue-panel">
      <h1>{title}</h1>
      <p>发票 {escape(draft["invoice_number"])} · 到期 {escape(draft["due_date"])}
        · 未收 {_aud_money(draft["outstanding_cents"])}</p>
      <p>收件人 {escape(draft["recipient"])} · 抄送 {escape(draft["cc"])}</p>
      <p>密送 {escape(draft["bcc"] or "—")}</p>
      <p>主题 {escape(message["subject"])}</p><pre>{escape(message["body"])}</pre>
      <p>附件 <a href="{escape(attachment_href or f'/revenue/documents/{document}/download', quote=True)}">{escape(message["attachment_name"])}</a></p>
      <form method="post" action="{escape(send_action, quote=True)}">
        {_hidden_security(csrf,token,"reminder-send")}{_hidden("operation",operation)}
        {_hidden("invoice_uid",draft["invoice_uid"])}{_hidden("digest",draft["digest"])}
        <button type="submit">确认发送</button>
      </form>
      <a href="{escape(back_href, quote=True)}">返回发票</a>
      </section>"""


def render_reminder_mail_records(invoice_uid, records, csrf, token, *, retry_action="", operation="",
                                 attachment_href=None):
    sections = []
    for mail in records:
        content = render_school_mail_status(invoice_uid, mail, csrf, token, False, attachment_href=attachment_href)
        if retry_action and mail["status"] == "failed":
            content += f"""<form method="post" action="{escape(retry_action, quote=True)}">
              {_hidden_security(csrf,token,"reminder-retry-"+mail["uid"])}
              {_hidden("operation",operation)}{_hidden("invoice_uid",invoice_uid)}
              {_hidden("mail_uid",mail["uid"])}{_hidden("attempt_count",mail["attempt_count"])}
              <button type="submit">重试催款邮件</button></form>"""
        sections.append(content)
    return "".join(sections)


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


def render_school_mail_history(entities, entries, filters, page_number, has_next):
    route = URL + "/mail-history"
    options = "".join(
        f'<option value="{key}"{" selected" if str(key) == filters["entity_id"] else ""}>{escape(name)}</option>'
        for key, name in entities
    )
    statuses = "".join(
        f'<option value="{key}"{" selected" if key == filters["status"] else ""}>{name}</option>'
        for key, name in MAIL_STATUS_LABELS.items()
    )
    rows = []
    for entry in entries:
        name = escape(entry["invoice_number"])
        href = f'{URL}?invoice={quote(entry.get("invoice_uid", entry["uid"]), safe="")}#invoice-mail-history'
        attachment_href = None
        if draft := entry.get("authority_draft") or entry.get("institution_draft"):
            contract_uid, draft_uid = (quote(item, safe="") for item in draft)
            href = f'/revenue/institution-commission-drafts/{contract_uid}?invoice={draft_uid}'
            attachment_href = href + '&pdf=formal'
        status = MAIL_STATUS_LABELS[entry["status"]]
        if entry["status"] == "sent":
            status = "已写入模拟邮箱" if entry.get("transport", "mock") == "mock" else "邮件服务器已接受"
        detail = ""
        mail_type = "发票"
        if entry.get("mail_kind") == "reminder":
            mail_type = "催款"
            detail = ('<details><summary>查看催款邮件内容</summary>'
                      + render_school_mail_status(entry["invoice_uid"], entry, "", "", False, attachment_href=attachment_href)
                      + '</details>')
        rows.append(
            f'<tr><td>{escape(entry["school"])}</td><td><a href="{href}">{name}</a></td>'
            f'<td>{escape(entry["recipient"])}</td><td>{escape(entry.get("cc") or "—")}</td>'
            f'<td>{_mail_time(entry["sent_at"])}</td><td>{_mail_time(entry["history_at"])}</td>'
            f'<td>{status} · {mail_type}{detail}</td></tr>'
        )
    pages = []
    for target, label, available in ((page_number - 1, "上一页", page_number > 1),
                                     (page_number + 1, "下一页", has_next)):
        if available:
            href = route + "?" + urlencode({**filters, "page": target})
            pages.append(f'<a href="{escape(href, quote=True)}">{label}</a>')
    content = "".join(rows) or '<tr><td colspan="7">暂无符合条件的发票邮件。</td></tr>'
    return f"""<section class="revenue-panel">
      <h1>发票发送历史</h1><p>日期按墨尔本时间。发送时间以邮件服务器接受或模拟邮箱写入为准。</p>
      <p><a href="{URL}">返回学校佣金开票</a></p>
      <form method="get" action="{route}" class="revenue-action-form">
        <label>学校<select name="entity_id"><option value="">全部学校</option>{options}</select></label>
        <label>发票编号<input name="number" maxlength="40" value="{escape(filters["number"], quote=True)}"></label>
        <label>从<input name="start" type="date" value="{escape(filters["start"], quote=True)}"></label>
        <label>至<input name="end" type="date" value="{escape(filters["end"], quote=True)}"></label>
        <label>状态<select name="status"><option value="">全部状态</option>{statuses}</select></label>
        <button type="submit">查询</button>
      </form>
      <div class="revenue-table-scroll"><table><thead><tr><th>学校</th><th>发票</th><th>收件人</th><th>抄送</th>
        <th>发送时间</th><th>最近记录时间</th><th>状态</th></tr></thead><tbody>{content}</tbody></table></div>
      <nav aria-label="分页">第 {page_number} 页 · {" ".join(pages)}</nav>
      </section>"""


def render_school_mail_status(invoice_uid, mail, csrf, token, allow_retry, *, attachment_href=None):
    if not mail:
        return "<p>发票邮件 · 未安排自动发送。签发时需已启用本校自动发信并核对财务邮箱。</p>"
    labels = {"pending":"待模拟发送","sending":"发送中","sent":"已写入模拟邮箱","failed":"模拟发送失败","cancelled":"学校邮箱设置已变更，发送已取消","needs_review":"模拟邮箱记录待核对"}
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
    if mail.get("mail_kind") == "reminder" and any(
            record.get("error") == "reminder_changed" for record in mail["attempts"]):
        labels["cancelled"] = "余额、到期日或收票邮箱已变更，催款已取消"
    retry = ""
    if allow_retry and mail["status"] == "failed":
        retry = f"""<form method="post" action="{URL}/mail-retry">
          {_hidden_security(csrf,token,"mail-retry")}{_hidden("invoice_uid",invoice_uid)}
          {_hidden("attempt_count",mail["attempt_count"])}
          <button type="submit">重试发票邮件</button></form>"""
    reminder = mail.get("mail_kind") == "reminder"
    title = "催款邮件" if reminder else "发票邮件"
    if mail.get("transport") != "production":
        title += " · TEST ONLY"
    section_id = mail["uid"] if reminder else "invoice-mail-history"
    copy_line = f'<p>抄送 {escape(mail["cc"])}</p>' if mail.get("cc") else ""
    if mail.get("bcc"):
        copy_line += f'<p>密送 {escape(mail["bcc"])}</p>'
    archived = mail.get("message")
    content = "<p>邮件主题和正文未存档。</p>"
    if archived:
        state = "邮件内容存档" if mail["sent_at"] else "入队时保存的邮件内容"
        content = f"""<details open><summary>{state}</summary>
          <p>发件人 {escape(archived["sender"])}</p><p>主题 {escape(archived["subject"])}</p>
          <pre class="revenue-mail-body">{escape(archived["body"])}</pre></details>"""
    document_uid = quote(mail["document_uid"], safe="")
    attachment_name = archived["attachment_name"] if archived else f'{mail["invoice_number"]}.pdf'
    return f"""<section id="{escape(section_id, quote=True)}"><h2>{title}</h2>
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
