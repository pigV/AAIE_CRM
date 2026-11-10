"""Separate the confirmed school contracts from contract intake."""

import json
from html import escape
from urllib.parse import quote, urlencode

from revenue import vic_rcti
from revenue.institution_commission_draft import CONFIRMED, FEE_PERIODS, GST_MODES, TIER_LABELS, InstitutionCommissionDraft
from revenue.institution_commission_draft_views import ROOT as DRAFT_ROOT
from revenue.multischool_views import _render_active_contract_rules
from revenue.repository import RevenueRepository
from revenue.school_commission_pilot import FORMAL_CONTRACT
from revenue.school_contract_extract import TUITION_PERIODS
from revenue.school_contract_review import review_state
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
        ("学校收票邮箱", fields.get("invoice_email") or "未确认"),
    )


def _institution_fields(terms):
    rcti = terms.get("payment_mode") == vic_rcti.PAYMENT
    labels = vic_rcti.LABELS if rcti else TIER_LABELS
    rates = " · ".join(f"{labels[key]} {basis_points / 100:g}%" for key, basis_points in terms["rates"].items())
    return (
        ("合同付款主体", terms["payer"]), ("佣金比例", rates),
        ("合同有效期", f'{terms["start"]} 至 {terms["end"]}'),
        ("计佣学费周期", FEE_PERIODS[terms["period"]]), ("GST", GST_MODES[terms["gst"]]),
        ("申领条件", terms["conditions"]),
        *((("结算方式", "按季度自动付款 · 教育局出具 RCTI"),) if rcti else (
            ("有效发票收件后付款期限", f'{terms["payment_days"]} 天'),
            ("学校收票邮箱", terms.get("invoice_email") or "未确认"),
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
        school["confirmed"].append((workflow.document["uid"], _institution_fields(json.loads(payload)), uid))
    return schools


def _page(title, body, *, back_href="/revenue", back_label="返回收入与佣金"):
    return f'''<link rel="stylesheet" href="/assets/revenue.css">
      <main class="revenue-workspace">
        <header class="revenue-dashboard-heading"><h1>{title}</h1>
          <a class="revenue-back-link" href="{back_href}">{back_label}</a></header>
        <section class="revenue-panel">{body}</section>
      </main>'''


def render_payer_billing_details(action, fields, settings, csrf, token, *, simulation):
    checked = " checked" if settings["enabled"] and settings.get("routing") == "payer" else ""
    verified = "已核对" if settings["verified"] else "未核对"
    mode = "模拟投递" if simulation else "发送"
    def hidden_input(name, value):
        return f'<input type="hidden" name="{escape(name, quote=True)}" value="{escape(str(value), quote=True)}">'
    hidden = "".join(hidden_input(name, value) for name, value in fields.items())
    return f'''<section id="school-invoice-mail-recipient">
      <h3>学校开票资料</h3><p>以下资料会显示在发票 Invoice To 区块。{verified} · 发信抄送 accounts@aaie.com.hk</p>
      <form method="post" action="{escape(action, quote=True)}" class="revenue-action-form">
        {_hidden_security(csrf, token, "payer-billing-details")}{hidden}
        {hidden_input("settings_revision", settings["settings_revision"])}
        {hidden_input("billing_revision", settings["revision"])}
        <label>发票显示名称<input name="billing_name" maxlength="180" required
               value="{escape(settings['name'], quote=True)}"></label>
        <label>账单地址<textarea name="billing_address" maxlength="300" rows="2" required>{escape(settings['address'])}</textarea></label>
        <label>联系电话<input name="billing_phone" type="tel" maxlength="60" required
               value="{escape(settings['phone'], quote=True)}"></label>
        <label>收票邮箱（多个地址用逗号分隔）<input name="recipient" type="email" multiple maxlength="2048" required
               value="{escape(settings["recipient"], quote=True)}"></label>
        <label><input name="verified" type="checkbox" value="1">我已核对收票邮箱</label>
        <label><input name="enabled" type="checkbox" value="1"{checked}>签发后自动{mode}</label>
        <button type="submit">保存开票资料</button>
      </form></section>'''


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


def render_confirmed_contracts(schools, target=None, *, mail_form="", authority_finance="", claim_href=""):
    if target is None:
        ordered = sorted(schools.items(), key=lambda pair: (pair[1]["name"].casefold(), pair[0]))
        entries = "".join(
            f'<li><a href="{escape(confirmed_url(key), quote=True)}">{escape(school["name"])}</a>'
            f' · {TARGET_LABELS[key[0]]}</li>'
            for key, school in ordered
        )
        return _page(CONFIRMED_TITLE, f"<ul>{entries}</ul>" if entries else "<p>暂无已确认的学校佣金合同。</p>")

    school = schools[target]
    content = f'<h2>{escape(school["name"])}</h2>'
    if claim_href:
        content += f'<p><a class="button-link primary" href="{escape(claim_href, quote=True)}">选学生并生成发票草稿</a></p>'
    if school["active"]:
        content += _render_active_contract_rules(school["active"], include_claim_terms=True)
    for uid, fields, workflow_uid in school["confirmed"]:
        workflow_link = (
            f'<p><a href="{DRAFT_ROOT}/{quote(workflow_uid, safe="")}">查看可申领学生和发票草稿</a></p>'
            if workflow_uid else ""
        )
        rows = "".join(f'<div><dt>{label}</dt><dd>{escape(value)}</dd></div>' for label, value in fields)
        content += f'''<article class="revenue-school-contract-summary">
          <h3>已确认条款</h3><dl>{rows}</dl>
          <a href="/revenue/documents/{quote(uid, safe="")}/view">查看原合同 PDF</a>{workflow_link}</article>'''
    if target[0] == "authority":
        content += f'<p><a href="{UPLOAD_ROOT}?education_authority={quote(target[1], safe="")}">上传续约合同</a></p>'
    return _page(CONFIRMED_TITLE, content + authority_finance + mail_form, back_href=CONFIRMED_ROOT, back_label="返回已确认学校佣金")


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
