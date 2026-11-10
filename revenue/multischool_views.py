"""Desktop forms for setting up a school's commission contract and permissions."""

from html import escape
import json
from urllib.parse import quote

from revenue.school_commission_pilot import FORMAL_CONTRACT, STAGES, school_pilot_scope
from revenue.views import _aud_money, _hidden_security


ROOT = "/revenue/contracts"
ACTIONS = ROOT + "/school"
STAGE_LABELS = {
    "contracts": "合同及预计佣金",
    "preview": "仅预览发票",
    "invoicing": "申领及开票",
    "settlement": "到账及核销",
    "paused": "暂停",
}


def school_configuration_data(connection, base, school_id, draft_uid):
    schools = connection.execute(
        "SELECT id,school_name FROM schools WHERE deleted_at IS NULL ORDER BY school_name,id"
    ).fetchall()
    if school_id not in {row[0] for row in schools}:
        school_id = None
    scope = school_pilot_scope(connection,base,school_id=school_id) if school_id else None
    data = {"schools":schools,"school_id":school_id,"scope":scope,"base":base}
    if not scope:
        return data
    data["payer"] = connection.execute(
        "SELECT legal_name FROM revenue_entities WHERE id=?",(scope.entity_id,),
    ).fetchone()[0]
    data["drafts"] = connection.execute(
        """SELECT s.uid,d.uid FROM revenue_rule_snapshots s
           JOIN revenue_documents d ON d.uid=s.source_rule_uid
           WHERE d.document_kind=? AND json_extract(s.canonical_json,'$.toorak_draft')=1
             AND json_extract(s.canonical_json,'$.school_commission_configurable')=1
             AND json_extract(s.canonical_json,'$.school_id')=?
             AND json_extract(s.canonical_json,'$.pilot_entity_id')=?
             AND json_extract(s.canonical_json,'$.pilot_simulation')=?
             AND NOT EXISTS (SELECT 1 FROM revenue_contract_versions v WHERE v.document_id=d.id)
           ORDER BY s.id DESC""",(FORMAL_CONTRACT,school_id,scope.entity_id,scope.simulation),
    ).fetchall()
    data["draft"] = next((row for row in data["drafts"] if row[0]==draft_uid),None)
    data["contracts"] = connection.execute(
        """SELECT d.uid,v.effective_from,v.effective_to,v.status FROM revenue_contract_versions v
           JOIN revenue_contract_groups g ON g.id=v.contract_group_id
           JOIN revenue_documents d ON d.id=v.document_id
           JOIN revenue_rule_snapshots draft ON draft.source_rule_uid=d.uid
           WHERE g.revenue_entity_id=? AND d.document_kind=?
             AND json_extract(draft.canonical_json,'$.school_id')=?
             AND json_extract(draft.canonical_json,'$.pilot_simulation')=? ORDER BY v.id DESC""",
        (scope.entity_id,FORMAL_CONTRACT,school_id,scope.simulation),
    ).fetchall()
    data["cases"] = connection.execute(
        """SELECT DISTINCT c.uid,c.student_id FROM revenue_commercial_cases c
           JOIN revenue_enrolment_components ec ON ec.commercial_case_id=c.id
           JOIN cycle_final_acceptances a ON a.id=ec.final_acceptance_id
           WHERE a.school_id=? AND a.acceptance_status='active' AND ec.component_kind='school'
             AND ec.status='active' ORDER BY c.student_id,c.uid LIMIT 50""",(school_id,),
    ).fetchall()
    return data


def _hidden(name, value):
    return f'<input type="hidden" name="{name}" value="{escape(str(value),quote=True)}">'


def _form(action, csrf, token, school_id, fields, button, *, upload=False):
    encoding = ' enctype="multipart/form-data"' if upload else ""
    return f'''<form method="post" action="{ACTIONS}/{action}"{encoding} class="revenue-action-form">
      {_hidden_security(csrf,token,"multi-school-"+action)}{_hidden("school_id",school_id)}
      {fields}<button type="submit">{button}</button></form>'''


def _tier_inputs(suffix, required):
    mandatory = " required" if required else ""
    return f'''<label>佣金比例（%）<input name="rate_percent{suffix}" inputmode="decimal"{mandatory}></label>
      <label>首年学费类别<select name="tuition_kind{suffix}"><option value="any">不限</option>
      <option value="local">本地</option><option value="international">国际</option></select></label>
      <label>适用签证子类（逗号分隔，留空不限）<input name="visa_subclasses{suffix}" maxlength="120"></label>'''


def render_multischool_configuration(data, csrf, token):
    school_id = data["school_id"]
    options = "".join(
        f'<option value="{key}"{" selected" if key==school_id else ""}>{escape(name)}</option>'
        for key,name in data["schools"]
    )
    chooser = f'''<p><a href="/schools/new">新增学校</a></p>
      <form method="get" action="{ROOT}" class="revenue-action-form">
      <label>学校<select name="school_id" required><option value="">请选择学校</option>{options}</select></label>
      <button type="submit">打开学校配置</button></form>'''
    body = ""
    scope = data["scope"] if school_id else None
    if school_id and not scope:
        body = _form("bind",csrf,token,school_id,'''
            <label>签约及付款主体<input name="legal_name" required maxlength="200"></label>
            <label>绑定生效日期<input name="effective_from" type="date" required></label>''',"关联付款主体")
    elif scope:
        current = scope.stage if scope.enabled else "paused"
        allowed_stages = (*STAGES[:STAGES.index(data["base"].stage)+1],"paused")
        stages = "".join(
            f'<option value="{stage}"{" selected" if stage==current else ""}>{STAGE_LABELS[stage]}</option>'
            for stage in allowed_stages
        )
        body = f'<p>付款主体 {escape(data["payer"])} · 当前阶段 {STAGE_LABELS[current]}</p>'
        body += _form("stage",csrf,token,school_id,
                      f'<label>佣金阶段<select name="stage">{stages}</select></label>',"保存学校阶段")
        entity = scope.entity_id
        body += f'<p><a href="/revenue/school-commission-invoices?entity_id={entity}">查看该学校的佣金发票</a>'
        if scope.enabled and STAGES.index(scope.stage)>=STAGES.index("invoicing"):
            body += f' · <a href="/revenue/commission-invoice-templates?entity_id={entity}">配置该校发票模板和财务资料</a>'
        body += '</p>'
        if scope.enabled:
            body += _form("configurable-draft",csrf,token,school_id,'''
              <label>签署合同 PDF<input type="file" name="contract_file" accept=".pdf,application/pdf" required></label>
              <label><input type="checkbox" name="signed_confirmed" value="1" required>已核对签署合同</label>''',
              "登记合同草稿",upload=True)
            for uid,_ in data["drafts"]:
                body += f'<p><a href="{ROOT}?school_id={school_id}&amp;contract={quote(uid,safe="")}">配置合同草稿 {escape(uid[-10:])}</a></p>'
            if data["draft"]:
                uid,document = data["draft"]
                terms = f'''{_hidden("draft_uid",uid)}
                  <p><a href="/revenue/documents/{quote(document,safe="")}/download">查看签署合同</a></p>
                  <label>合同生效日期<input type="date" name="effective_from" required></label>
                  <label>合同到期日期<input type="date" name="effective_to" required></label>
                  <label>样本首年学费（AUD）<input name="sample_tuition" inputmode="decimal" required></label>
                  {_tier_inputs("",True)}
                  <details><summary>添加第二档</summary>{_tier_inputs("_2",False)}</details>
                  <label>GST<select name="gst_mode"><option value="none">零 GST</option>
                  <option value="exclusive">另加 10% GST</option><option value="inclusive">含 10% GST</option></select></label>
                  <label>开学后满多少天可申领<input name="commencement_days" type="number" min="0" max="365" required></label>
                  <label>需要注册凭证<select name="registration_required"><option value="1">需要</option>
                  <option value="0">不需要</option></select></label>
                  <label>有效发票收件后付款期限<select name="payment_days">
                  <option value="0">当天</option><option value="7">7 天</option>
                  <option value="15">15 天</option><option value="30" selected>30 天</option></select></label>
                  <label>合同条款出处<input name="clause_reference" maxlength="100" required></label>'''
                body += _form("configurable-preview",csrf,token,school_id,terms,"预览佣金规则")
        body += "".join(
            f'<p><a href="/revenue/documents/{quote(uid,safe="")}/download">查看合同</a>'
            f' · {escape(start)} 至 {escape(end)} · {escape(status)}</p>'
            for uid,start,end,status in data["contracts"]
        )
        body += "".join(
            f'<p><a href="/revenue/cases/{quote(uid,safe="")}">学生档案 {escape(student)}</a></p>'
            for uid,student in data["cases"]
        )
    return f'<section class="revenue-panel"><h2>多学校佣金配置</h2>{chooser}{body}</section>'


def render_configurable_preview(preview, csrf, token):
    values = preview["confirmation_fields"]
    school_id = values["school_id"]
    rows = "".join(
        f'<li>{row["rate_basis_points"]/100:g}% · 净额 {_aud_money(row["net_cents"])}'
        f' · GST {_aud_money(row["gst_cents"])} · 合计 {_aud_money(row["total_cents"])}</li>'
        for row in preview["rows"]
    )
    claim = preview["configuration"]["claim"]
    fields = "".join(_hidden(key,value) for key,value in values.items() if key!="school_id")
    fields += _hidden("preview_digest",preview["digest"])
    return f'''<section class="revenue-panel"><h2>{escape(preview["school_name"])} · 佣金规则预览</h2>
      <p>付款主体 {escape(preview["legal_name"])} · {escape(preview["effective_from"])} 至 {escape(preview["effective_to"])}</p>
      <p>开学满 {claim["commencement_days"]} 天 · 注册凭证 {"必需" if claim["registration_required"] else "无需"}
      · 发票收件后 {claim["payment_days_from_valid_invoice"]} 天付款</p><ul>{rows}</ul>
      {_form("configurable-activate",csrf,token,school_id,fields,"确认并启用规则")}
      <a href="{ROOT}?school_id={school_id}&amp;contract={quote(values["draft_uid"],safe="")}">返回合同草稿</a></section>'''


def _render_active_contract_rules(rows, *, include_claim_terms=False):
    gst_labels = {"exclusive": "另加 10% GST", "inclusive": "已含 10% GST", "none": "不加 GST"}
    tuition_labels = {"local": "本地档", "international": "国际档", "any": "统一比例"}
    cards = []
    for uid, version, start, end, raw in rows:
        rule = json.loads(raw)
        tiers = rule.get("tiers") or []
        if tiers:
            rates = " · ".join(
                f'{tuition_labels.get(tier["tuition_kind"], "佣金")} {tier["rate_basis_points"] / 100:g}%'
                for tier in tiers
            )
            period = "按首年学费一次性计佣"
        else:
            instalments = rule.get("instalments") or []
            rates = " · ".join(f'{item["rate_basis_points"] / 100:g}%' for item in instalments)
            single = len(instalments) == 1 and instalments[0].get("academic_period") == "single"
            period = "按学费一次性计佣" if single else "按合同期次"
        gst = gst_labels.get(rule.get("gst_mode"), "未单独设置")
        email = rule.get("invoice_email")
        recipient = f'<div><dt>合同收票邮箱建议</dt><dd>{escape(email)}</dd></div>' if email else ""
        claim = rule.get("claim") or {}
        claim_rows = ""
        if include_claim_terms and claim:
            days = claim.get("commencement_days")
            conditions = f"开学后满 {days} 天" if days else "已实际开学"
            if claim.get("registration_required"):
                conditions += "，并提供注册凭证"
            deadline = claim.get("payment_days_from_valid_invoice")
            claim_rows = f'<div><dt>申领条件</dt><dd>{conditions}</dd></div>'
            if deadline is not None:
                claim_rows += f'<div><dt>有效发票收件后付款期限</dt><dd>{int(deadline)} 天</dd></div>'
        cards.append(f"""<article class="revenue-school-contract-summary">
          <h4>合同 V{int(version)} · 已启用</h4>
          <dl>
            <div><dt>佣金比例</dt><dd>{rates or "未配置"}</dd></div>
            <div><dt>合同有效期</dt><dd>{escape(start)} 至 {escape(end or "未登记")}</dd></div>
            <div><dt>计佣周期</dt><dd>{period}</dd></div>
            <div><dt>默认 GST</dt><dd>{gst}</dd></div>{recipient}{claim_rows}
          </dl>
          <a href="/revenue/documents/{quote(uid, safe="")}/download">查看合同 PDF</a>
        </article>""")
    content = "".join(cards) or '<p class="revenue-empty">该校暂无已启用的佣金合同规则。</p>'
    return f'<section aria-labelledby="active-school-contract-heading"><h3 id="active-school-contract-heading">已启用的学校佣金合同</h3>{content}</section>'
