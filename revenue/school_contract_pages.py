"""Separate the confirmed school contracts from contract intake."""

import json
from html import escape
from urllib.parse import quote, urlencode

from revenue import vic_rcti
from revenue.institution_commission_draft import (
    CONFIRMED, FEE_PERIODS, GST_MODES, NSW_CLAIMS_ONLY, NSW_RATE_LABELS, NSW_REVIEW_SAVED, TIER_LABELS, InstitutionCommissionDraft,
)
from revenue.institution_commission_draft_views import ROOT as DRAFT_ROOT
from revenue.multischool_views import _render_active_contract_rules
from revenue.repository import RevenueRepository
from revenue.school_commission_pilot import FORMAL_CONTRACT
from revenue.school_contract_extract import TUITION_PERIODS
from revenue.school_contract_review import review_state
from revenue.school_invoice_release import REHEARSAL_RECIPIENT
from revenue.school_invoice_reminders import EMAIL
from revenue.state_school_commission import DISCARDED_CONTRACT_UPLOAD
from revenue.views import _hidden_security

CONFIRMED_ROOT = "/revenue/contracts"
UPLOAD_ROOT = CONFIRMED_ROOT + "/upload-school"
CONFIRMED_TITLE = "已确认学校佣金"
UPLOAD_TITLE = "新增／上传学校合同"
TARGET_FIELDS = {"school": "school_id", "provider": "higher_education_provider_id",
                 "authority": "education_authority"}
TARGET_LABELS = {"school": "中小学", "provider": "高等教育机构", "authority": "州／领地教育局"}


def confirmed_url(target):
    kind, key = target
    return CONFIRMED_ROOT + "?" + urlencode({TARGET_FIELDS[kind]: key})


def billing_url(target):
    return confirmed_url(target) + "&step=billing#school-invoice-mail-recipient"


def uploaded_contract_targets(connection, simulation):
    rows = connection.execute(
        """SELECT DISTINCT json_extract(s.canonical_json,'$.school_id'),
                           json_extract(s.canonical_json,'$.higher_education_provider_id'),
                           json_extract(s.canonical_json,'$.education_authority')
             FROM revenue_rule_snapshots s
             JOIN revenue_documents d ON d.uid=s.source_rule_uid
            WHERE s.rule_kind='commission' AND d.document_kind=?
              AND json_extract(s.canonical_json,'$.signed_confirmed')=1
              AND json_extract(s.canonical_json,'$.pilot_simulation')=?
              AND NOT EXISTS (SELECT 1 FROM revenue_audit_events removed
                               WHERE removed.action=? AND removed.aggregate_uid=s.uid)""",
        (FORMAL_CONTRACT, int(simulation), DISCARDED_CONTRACT_UPLOAD),
    )
    return {(kind, key) for row in rows for kind, key in zip(TARGET_FIELDS, row) if key is not None}


def reviewed_school_terms(connection, uid, profile):
    state = review_state(connection, uid)
    analysis, review = state["analysis"], state["review"]
    if not (analysis and review and analysis["run"] == review["run"]):
        return None
    fields = {key: field["value"] for key, field in review["fields"].items()}
    rates = {"本地档": "local_rate", "国际档": "international_rate"} if profile == "toorak" else {"统一比例": "rate"}
    required = ("school_name", "start", "end", "tuition_period", "conditions", *rates.values())
    if not all(fields.get(key) for key in required) or fields.get("gst") not in GST_MODES:
        return None
    conditions = " ".join(fields[key] for key in ("conditions", "local_scope", "international_scope",
                          "agency_application", "direct_exclusion", "invoice_condition") if fields.get(key))
    return (
        ("合同付款主体", fields["school_name"]),
        ("佣金比例", " · ".join(f"{label} {fields[key].rstrip('%')}%" for label, key in rates.items())),
        ("合同有效期", f'{fields["start"]} 至 {fields["end"]}'),
        ("计佣学费周期", TUITION_PERIODS[fields["tuition_period"]]),
        ("GST", GST_MODES[fields["gst"]]), ("申领条件", conditions),
        ("有效发票收件后付款期限", f'{fields["payment_days"]} 天' if fields.get("payment_days") else "未确认"),
    )


def _institution_fields(terms):
    rcti = terms.get("payment_mode") == vic_rcti.PAYMENT
    nsw = terms.get('payment_mode') == NSW_CLAIMS_ONLY
    labels = {**TIER_LABELS, **NSW_RATE_LABELS} if nsw else vic_rcti.LABELS if rcti else TIER_LABELS
    rates = " · ".join(f"{labels[key]} {basis_points / 100:g}%" for key, basis_points in terms["rates"].items())
    return (
        ("合同付款主体", terms["payer"]), ("佣金比例", rates),
        ("合同有效期", f'{terms["start"]} 至 {terms["end"]}'),
        ("计佣学费周期", "普通课程首年，短于 12 个月按全程；IEC 另计，最多两学期、20 周"
         if nsw else FEE_PERIODS[terms["period"]]), ("GST", GST_MODES[terms["gst"]]),
        ("申领条件", terms["conditions"]),
        *((("结算方式", "按季度自动付款 · 教育局出具 RCTI"),) if rcti else (
            ("有效发票收件后付款期限", f'{terms["payment_days"]} 天' if terms["payment_days"] is not None else "未约定"),
        )),
    )


def confirmed_contracts(connection, scope, actor, *, active_schools=None):
    schools = {}
    for key, school in (active_schools or {}).items():
        reviewed = []
        for uid, document, profile in school.get("drafts", ()):
            if profile and (terms := reviewed_school_terms(connection, uid, profile)):
                reviewed.append((document, terms, None))
        if school["active_contract_rules"] or reviewed:
            schools[("school", key)] = {
                "name": school["school_name"], "active": school["active_contract_rules"], "confirmed": reviewed,
            }
    confirmed = connection.execute(
        """SELECT a.aggregate_uid,a.after_json FROM revenue_audit_events a
            WHERE a.action=? AND a.aggregate_type='institution_commission_terms'
              AND a.id=(SELECT max(b.id) FROM revenue_audit_events b
                        WHERE b.action=a.action AND b.aggregate_uid=a.aggregate_uid)
            ORDER BY a.id DESC""",
        (CONFIRMED,),
    ).fetchall()
    repository = RevenueRepository(connection)

    for uid, payload in confirmed:
        try:
            workflow = InstitutionCommissionDraft(repository, scope, uid, actor)
        except (LookupError, PermissionError):
            continue
        school = schools.setdefault(workflow.target, {"name": workflow.name, "active": [], "confirmed": []})
        terms = json.loads(payload)
        school["confirmed"].append((workflow.document["uid"], _institution_fields(terms), uid))
        if terms.get("payment_mode") == NSW_CLAIMS_ONLY:
            school.setdefault("nsw_claims", set()).add(uid)
    return schools


def reviewed_nsw_contracts(connection, scope, actor):
    """Find saved NSW reviews for display without treating them as confirmed terms."""
    if "NSW" not in scope.education_authorities:
        return []
    rows = connection.execute(
        """SELECT aggregate_uid FROM revenue_audit_events
           WHERE action=? AND aggregate_type='nsw_commission_review'
           GROUP BY aggregate_uid ORDER BY max(id) DESC""",
        (NSW_REVIEW_SAVED,),
    ).fetchall()
    repository = RevenueRepository(connection)
    reviewed = []
    for (uid,) in rows:
        try:
            workflow = InstitutionCommissionDraft(repository, scope, uid, actor)
        except (LookupError, PermissionError):
            continue
        if workflow.nsw_review() and not workflow.terms():
            reviewed.append((workflow.name, uid))
    return reviewed


def _page(title, body, *, back_href="/revenue", back_label="返回收入与佣金"):
    return f'''<link rel="stylesheet" href="/assets/revenue.css">
      <main class="revenue-workspace">
        <header class="revenue-dashboard-heading"><h1>{title}</h1>
          <a class="revenue-back-link" href="{back_href}">{back_label}</a></header>
        <section class="revenue-panel">{body}</section>
      </main>'''


def render_school_template_selection(action, settings, csrf, token):
    versions = settings["versions"]
    names = {row["uid"]: row["name"] or ("版本 " + row["uid"][-8:]) for row in versions}
    default_name = names.get(settings["active_uid"], "尚无已发布版本")
    if not versions:
        return f'<p>发票模板版本：{escape(default_name)}</p>'
    selected = settings["selected_uid"]
    options = [f'<option value="">跟随当前已发布版本 · {escape(default_name)}</option>']
    for row in versions:
        uid = row["uid"]
        status = ("当前已发布" if uid == settings["active_uid"] else
                  "曾发布" if uid in settings["published"] else "草稿")
        options.append(f'<option value="{escape(uid, quote=True)}"{" selected" if uid == selected else ""}>'
                       f'{escape(names[uid])} · {status}</option>')
    current = names.get(settings["effective_uid"], default_name)
    return f'''<form method="post" action="{escape(action, quote=True)}" class="revenue-action-form">
        {_hidden_security(csrf, token, "school-template-selection")}
        <input type="hidden" name="billing_operation" value="template-selection">
        <input type="hidden" name="template_revision" value="{escape(settings["revision"], quote=True)}">
        <label>发票模板版本<select name="template_uid">{"".join(options)}</select></label>
        <p>本校当前使用：{escape(current)}</p>
        <button type="submit">保存本校模板</button>
      </form>'''


def render_payer_billing_details(action, fields, settings, csrf, token, *, simulation, allow_auto_mail=True,
                                 preview_href="", error="", template_form="", preview_label="预览 Invoice 1762 样张"):
    checked = " checked" if settings["enabled"] else ""
    verification_checked = " checked" if settings["verified"] else ""
    verified = "已核对" if settings["verified"] else "未核对"
    mode = "模拟投递" if simulation else "发送"
    def hidden_input(name, value):
        return f'<input type="hidden" name="{escape(name, quote=True)}" value="{escape(str(value), quote=True)}">'
    hidden = "".join(hidden_input(name, value) for name, value in fields.items())

    auto_mail_control = (
        f'<label><input name="enabled" type="checkbox" value="1"{checked}>签发后自动{mode}</label>'
        if allow_auto_mail else ""
    )
    from revenue.invoice_billing_details import ADDRESS_FIELDS

    parts = settings.get("address_parts")
    address = settings["address"]
    address_fields = "".join(
        f'<label>{label}<input name="{field}" maxlength="{limit}"'
        f'{" required" if field == "address_line_1" else ""} '
        f'value="{escape((parts or {}).get(field, ""), quote=True)}"></label>'
        for field, label, limit in ADDRESS_FIELDS
    )
    if address and not parts:
        address_fields = (
            f'<label>现有账单地址<textarea name="billing_address" maxlength="300" rows="3">'
            f'{escape(address)}</textarea></label>'
            f'<label><input type="checkbox" data-use-address-parts>改用分栏地址</label>'
            f'<fieldset data-address-parts hidden disabled><legend>账单地址</legend>{address_fields}</fieldset>'
        )
    else:
        address_fields = f'<fieldset><legend>账单地址</legend>{address_fields}</fieldset>'
    copy_fields = "".join(
        f'<label>{field.title()}（选填，逗号分隔）<input name="{field}" inputmode="email" maxlength="2048" '
        f'data-payer-email-pattern="{escape(EMAIL.pattern, quote=True)}" '
        f'data-payer-test-recipient="{escape(REHEARSAL_RECIPIENT.casefold(), quote=True)}" '
        f'aria-describedby="payer-{field}-error" value="{escape(settings.get(field, ""), quote=True)}">'
        f'<span id="payer-{field}-error" class="notice error" aria-live="polite" hidden></span></label>'
        for field in ("cc", "bcc")
    )
    form = f'''
      <form method="post" action="{escape(action, quote=True)}" class="revenue-action-form">
        {_hidden_security(csrf, token, "payer-billing-details")}{hidden}
        {hidden_input("settings_revision", settings["settings_revision"])}
        {hidden_input("billing_revision", settings["revision"])}
        <label>发票显示名称<input name="billing_name" maxlength="180" required
               value="{escape(settings['name'], quote=True)}"></label>
        {address_fields}
        <label>联系电话（选填）<input name="billing_phone" type="tel" maxlength="60"
               value="{escape(settings['phone'], quote=True)}"></label>
        <label>收票邮箱（多个地址用逗号分隔）<input name="recipient" type="text" inputmode="email"
               spellcheck="false" maxlength="2048" required aria-describedby="payer-email-error"
               data-payer-email-pattern="{escape(EMAIL.pattern, quote=True)}"
               data-payer-test-recipient="{escape(REHEARSAL_RECIPIENT.casefold(), quote=True)}"
               value="{escape(settings["recipient"], quote=True)}">
          <span id="payer-email-error" class="notice error" aria-live="polite" hidden></span>
        </label>
        {copy_fields}
        <label><input name="verified" type="checkbox" value="1"{verification_checked}>我已核对收票邮箱及抄送地址</label>
        {auto_mail_control}
        <button type="submit">保存开票资料</button>
      </form>'''

    summary = ""
    if settings["revision"]:
        values = (
            ("发票显示名称", escape(settings["name"])),
            ("账单地址", "<br>".join(map(escape, settings["address"].splitlines()))),
            ("联系电话", escape(settings["phone"])),
            ("收票邮箱", "<br>".join(escape(address.strip()) for address in settings["recipient"].split(","))),
            ("Cc", escape(settings.get("cc", ""))),
            ("Bcc", escape(settings.get("bcc", ""))),
        )
        rows = "".join(f"<div><dt>{label}</dt><dd>{value or '未填写'}</dd></div>" for label, value in values)
        summary = f'''<div class="revenue-school-contract-summary revenue-school-billing-summary">
          <h4>当前已保存的开票资料</h4><dl>{rows}</dl></div>'''
        form = f'''<details class="revenue-school-billing-editor"{' open' if error else ''}>
          <summary>修改</summary>
          {form}
        </details>'''
    preview = (f'<p><a class="button-link secondary" href="{escape(preview_href, quote=True)}" '
               f'target="_blank" rel="noopener">{escape(preview_label)}</a></p>' if preview_href else "")
    feedback = f'<p role="alert">{escape(error)}</p>' if error else ""
    return f'''<section id="school-invoice-mail-recipient">
      <h3>学校开票资料</h3><p>以下资料会显示在发票 Invoice To 区块。{verified} · 发信抄送 accounts@aaie.com.hk</p>
      {feedback}{template_form}{summary}{preview}
      {form}
      <script src="/assets/school-invoice-billing.js" defer></script>
      </section>'''


def render_authority_finance(state, terms, payer, csrf, token):
    if payer:
        href = "/revenue/commission-invoice-templates?" + urlencode({"education_authority": state})
        return (f'<section><h3>正式发票配置</h3><p>付款主体 {escape(payer["legal_name"])}</p>'
                f'<a class="secondary button-link" href="{escape(href, quote=True)}">'
                '正式模板与财务资料</a></section>')
    action = DRAFT_ROOT + "/" + quote(terms["uid"], safe="")
    return f'''<section><h3>正式发票配置</h3>
      <form method="post" action="{escape(action, quote=True)}" class="revenue-action-form">
        {_hidden_security(csrf, token, "authority-payer")}
        <input type="hidden" name="operation" value="authority-payer">
        <p>合同付款主体 {escape(terms["payer"])}</p>
        <label><input type="checkbox" name="reviewed" value="1" required>已核对法定付款主体</label>
        <button type="submit">登记教育局付款主体</button>
      </form></section>'''


def _billing_recipient_label(settings):
    if settings is None:
        return "暂无法读取"
    recipient = settings["recipient"]
    if not recipient:
        return "尚未填写"
    status = "已核对" if settings["verified"] else "待核对"
    return f"{recipient} · {status}"


def render_confirmed_contracts(schools, target=None, *, mail_form="", authority_finance="", claim_href="",
                               title=CONFIRMED_TITLE, reviewed_nsw=(), can_edit=False, billing_href="",
                               billing_settings=None):
    if target is None:
        ordered = sorted(schools.items(), key=lambda pair: (pair[1]["name"].casefold(), pair[0]))
        entries = "".join(
            f'<li><a href="{escape(confirmed_url(key), quote=True)}">{escape(school["name"])}</a>'
            f' · {TARGET_LABELS[key[0]]}</li>'
            for key, school in ordered
        )
        entries += "".join(
            f'<li><a href="{DRAFT_ROOT}/{quote(uid, safe="")}?contract_only=1">'
            f'{escape(name)} · 已核对，待启用</a></li>'
            for name, uid in reviewed_nsw
        )
        return _page(CONFIRMED_TITLE, f"<ul>{entries}</ul>" if entries else "<p>暂无已确认的学校佣金合同。</p>")

    school = schools[target]
    billing_recipient = _billing_recipient_label(billing_settings)
    content = f'<h2>{escape(school["name"])}</h2>'
    if billing_href:
        label = "返回学校佣金" if mail_form else "学校开票资料"
        content += f'<p><a class="button-link secondary" href="{escape(billing_href, quote=True)}">{label}</a></p>'
    if claim_href:
        content += f'<p><a class="button-link primary" href="{escape(claim_href, quote=True)}">选学生并生成发票草稿</a></p>'
    if school["active"]:
        edit_links = {uid: confirmed_url(target) + "&edit_commission=" + quote(uid, safe="")
                      for uid, *_ in school["active"]} if can_edit else {}
        content += _render_active_contract_rules(school["active"], include_claim_terms=True, edit_links=edit_links,
                                                 billing_recipient=billing_recipient)
    for uid, fields, workflow_uid in school["confirmed"]:
        label = ("查看 NSW 学生与预计佣金" if workflow_uid in school.get("nsw_claims", ())
                 else "查看可申领学生和发票草稿")
        workflow_link = (
            f'<p><a href="{DRAFT_ROOT}/{quote(workflow_uid, safe="")}">{label}</a></p>'
            if workflow_uid else ""
        )
        edit_url = confirmed_url(target) + "&edit_commission=" + quote(workflow_uid, safe="") if can_edit and workflow_uid else ""
        edit_link = f'<a class="button-link secondary" href="{escape(edit_url, quote=True)}">编辑佣金条款</a>' if edit_url else ""
        rows = "".join(f'<div><dt>{label}</dt><dd>{escape(value)}</dd></div>'
                       for label, value in (*fields, ("学校收票邮箱", billing_recipient)))
        content += f'''<article class="revenue-school-contract-summary">
          <h3>已确认条款</h3><dl>{rows}</dl>
          <a href="/revenue/documents/{quote(uid, safe="")}/view">查看原合同 PDF</a> {edit_link}{workflow_link}</article>'''
    if target[0] == "authority":
        content += f'<p><a href="{UPLOAD_ROOT}?education_authority={quote(target[1], safe="")}">上传续约合同</a></p>'
    return _page(title, content + authority_finance + mail_form, back_href=CONFIRMED_ROOT, back_label="返回已确认学校佣金")


def render_contract_upload(schools, providers, csrf, token, *, authorities=(), renewal=False, pending=()):
    groups = []
    renewal_input = '<input type="hidden" name="renewal" value="1">' if renewal else ""
    for kind, institutions in (("school", schools), ("provider", providers), ("authority", authorities)):
        options = "".join(
            f'<option value="{kind}:{key}">{escape(name)}</option>'
            for key, name in sorted(institutions, key=lambda item: (item[1].casefold(), item[0]))
        )
        if options:
            groups.append(f'<optgroup label="{TARGET_LABELS[kind]}">{options}</optgroup>')
    form = f'''<form method="post" action="{CONFIRMED_ROOT}/school/configurable-draft"
                  enctype="multipart/form-data" class="revenue-action-form" data-school-contract-intake>
          {_hidden_security(csrf, token, "school-contract-intake")}{renewal_input}
          <label>搜索学校或教育局<input type="search" data-school-search placeholder="输入校名或州别关键词"></label>
          <label>从学校资料库选择学校或教育局<select name="institution" required>
            <option value="">请选择学校</option>{"".join(groups)}</select></label>
          <p data-school-no-results class="revenue-action-form__wide" role="status" hidden>没有符合条件的学校。</p>
          <fieldset class="revenue-action-form__wide" data-contract-upload-fields hidden disabled>
            <legend>上传签署合同</legend>
            <label class="global-file-drop-zone" data-file-drop-zone data-school-contract-drop>
              拖入签署合同 PDF，或点击选择
              <input name="contract_file" type="file" accept=".pdf,application/pdf" required>
              <small data-file-drop-summary role="status"></small>
            </label>
            <button class="primary" type="submit">上传并分析合同</button>
          </fieldset>
        </form><script src="/assets/school-contract-intake.js" defer></script>''' if groups else "<p>CRM 资料库中暂无尚未上传佣金合同的可选学校。</p>"
    pending_links = "".join(
        f'<li><a href="{DRAFT_ROOT}/{quote(uid, safe="")}?contract_only=1">继续核对 {escape(label)} 合同</a></li>'
        for label, uid in pending
    )
    if pending_links:
        form += f'<h2>已上传，待核对</h2><ul>{pending_links}</ul>'
    return _page("上传教育局续约合同" if renewal else UPLOAD_TITLE, form)


def pending_contracts(connection, scope, actor):
    """List every in-scope pending institution upload, including ordinary schools."""
    drafts = connection.execute(
        """SELECT s.uid FROM revenue_rule_snapshots s
           JOIN revenue_documents d ON d.uid=s.source_rule_uid
           WHERE s.rule_kind='commission' AND d.document_kind=?
             AND json_extract(s.canonical_json,'$.pilot_entity_id')=0
             AND json_extract(s.canonical_json,'$.pilot_simulation')=?
             AND NOT EXISTS (SELECT 1 FROM revenue_contract_versions v WHERE v.document_id=d.id)
             AND NOT EXISTS (SELECT 1 FROM revenue_audit_events a
                              WHERE a.aggregate_uid=s.uid AND a.action IN (?,?))
           ORDER BY s.id DESC""",
        (FORMAL_CONTRACT, int(scope.simulation), CONFIRMED, DISCARDED_CONTRACT_UPLOAD),
    ).fetchall()
    repository = RevenueRepository(connection)
    pending = []
    for (uid,) in drafts:
        try:
            workflow = InstitutionCommissionDraft(repository, scope, uid, actor)
        except (LookupError, PermissionError):
            continue
        pending.append((workflow.name, uid))
    return pending
