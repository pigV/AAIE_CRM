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
from revenue.state_school_commission import DISCARDED_CONTRACT_UPLOAD, STATE_AUTHORITIES, school_commission_assignment
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
        schools[("school", key)] = {
            "name": school["school_name"], "active": school["active_contract_rules"], "confirmed": [],
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






def render_confirmed_contracts(schools, target=None, *, rcti_uid=None):
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
    if school["active"]:
        content += _render_active_contract_rules(school["active"], include_claim_terms=True)
    for uid, fields, _ in school["confirmed"]:
        rows = "".join(f'<div><dt>{label}</dt><dd>{escape(value)}</dd></div>' for label, value in fields)
        content += f'''<article class="revenue-school-contract-summary">
          <h3>已确认条款</h3><dl>{rows}</dl>
          <a href="/revenue/documents/{quote(uid, safe="")}/view">查看原合同 PDF</a></article>'''
    if target[0] == "authority":
        content += f'<p><a href="{UPLOAD_ROOT}?education_authority={quote(target[1], safe="")}">上传续约合同</a></p>'
    if rcti_uid and target == ("authority", "VIC"):
        uid = quote(rcti_uid, safe="")
        content += (f'<p><a class="button-link primary" href="/revenue/vic-rcti/{uid}">RCTI 对账</a> '
                    f'<a class="secondary button-link" href="{DRAFT_ROOT}/{uid}">核对学生凭证与预计佣金</a></p>')
    return _page(CONFIRMED_TITLE, content, back_href=CONFIRMED_ROOT, back_label="返回已确认学校佣金")


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
    form = f'''<form method="post" action="{UPLOAD_ROOT}"
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


def available_contract_targets(connection, scope):
    uploaded = uploaded_contract_targets(connection, scope.simulation)
    assignments = school_commission_assignment(connection)
    schools = connection.execute(
        """SELECT s.id,s.school_name FROM schools s WHERE s.deleted_at IS NULL
           AND NOT EXISTS (SELECT 1 FROM revenue_entity_provider_links l
                           WHERE l.school_id=s.id AND l.effective_to IS NULL)"""
    )
    choices = []
    for key, name in schools:
        sector, state = assignments.get(key, ("", ""))
        if ("school", key) not in uploaded and (sector != "public" or state not in scope.education_authorities):
            choices.append((key, name))
    providers = connection.execute(
        """SELECT p.id,p.provider_name FROM higher_education_providers p
           WHERE p.active_status='Active' AND p.review_status IN ('Pending','Approved')
             AND NOT EXISTS (SELECT 1 FROM revenue_entity_provider_links l
                             WHERE l.higher_education_provider_id=p.id AND l.effective_to IS NULL)"""
    )
    provider_choices = [(key, name) for key, name in providers if ("provider", key) not in uploaded]
    authorities = [(state, STATE_AUTHORITIES[state]) for state in scope.education_authorities
                   if ("authority", state) not in uploaded]
    return choices, provider_choices, authorities


def active_bound_school_contract(connection, scope):
    rows = connection.execute(
        """SELECT d.uid,v.version_no,v.effective_from,v.effective_to,s.canonical_json
           FROM revenue_contract_versions v
           JOIN revenue_contract_groups g ON g.id=v.contract_group_id
           JOIN revenue_documents d ON d.id=v.document_id
           JOIN revenue_commission_rule_versions r ON r.contract_version_id=v.id AND r.status='active'
           JOIN revenue_rule_snapshots s ON s.id=r.rule_snapshot_id
           WHERE g.revenue_entity_id=? AND g.contract_kind='direct_commission'
             AND v.status='active' AND d.document_kind=?
             AND json_extract(s.canonical_json,'$.school_id')=?
             AND json_extract(s.canonical_json,'$.pilot_simulation')=?
           ORDER BY v.effective_from DESC,v.id DESC,r.id DESC""",
        (scope.entity_id, FORMAL_CONTRACT, scope.school_id, scope.simulation),
    ).fetchall()
    school = connection.execute("SELECT school_name FROM schools WHERE id=? AND deleted_at IS NULL", (scope.school_id,)).fetchone()
    return {scope.school_id: {"school_name": school[0], "active_contract_rules": rows}} if school and rows else {}
