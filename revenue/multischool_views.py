"""Desktop forms for setting up a school's commission contract and permissions."""

from html import escape
import json
from urllib.parse import quote

from revenue.school_commission_pilot import FORMAL_CONTRACT, STAGES, school_pilot_scope
from revenue.school_contracts import PROVIDER_REGISTRATION_ACTION, SCHOOL_REGISTRATION_ACTION
from revenue.school_invoice_mail import FINANCE_COPY_ADDRESS, mail_settings
from revenue.views import _aud_money, _hidden_security, render_contract_analysis


ROOT = "/revenue/contracts"
ACTIONS = ROOT + "/school"
STAGE_LABELS = {
    "contracts": "合同及预计佣金",
    "preview": "仅预览发票",
    "invoicing": "申领及开票",
    "settlement": "到账及核销",
    "paused": "暂停",
}


def school_configuration_data(connection, base, school_id, draft_uid, *, add_school=False, school_search="",
                              mock_mail=False):
    intake_only = base.contract_intake and not base.self_service
    can_register = intake_only or (base.self_service and base.simulation)
    rows = connection.execute(
        """WITH drafts AS (
             SELECT DISTINCT json_extract(canonical_json,'$.school_id') AS school_id
               FROM revenue_rule_snapshots
              WHERE ? AND rule_kind='commission' AND json_extract(canonical_json,'$.toorak_draft')=1
                AND json_extract(canonical_json,'$.pilot_entity_id')=0
                AND json_extract(canonical_json,'$.pilot_simulation')=?
           ), registrations AS (
             SELECT DISTINCT aggregate_uid FROM revenue_audit_events
              WHERE ? AND action=? AND aggregate_type='school_commission_school'
           ), bindings AS (
             SELECT DISTINCT l.school_id FROM revenue_entity_provider_links l
               JOIN revenue_entities e ON e.id=l.revenue_entity_id
              WHERE ? AND l.effective_to IS NULL AND e.entity_type='school' AND e.status='active'
           )
           SELECT s.id,s.school_name,(r.aggregate_uid IS NOT NULL AND d.school_id IS NULL AND b.school_id IS NULL)
             FROM schools s
             LEFT JOIN drafts d ON d.school_id=s.id
             LEFT JOIN registrations r ON r.aggregate_uid=(? || ':' || s.id)
             LEFT JOIN bindings b ON b.school_id=s.id
            WHERE s.deleted_at IS NULL
              AND (s.id=? OR b.school_id IS NOT NULL OR d.school_id IS NOT NULL OR r.aggregate_uid IS NOT NULL)
            ORDER BY s.school_name,s.id""",
        (can_register, base.simulation, can_register, SCHOOL_REGISTRATION_ACTION,
         base.self_service, int(base.simulation), base.school_id),
    ).fetchall()
    schools = [(key, name) for key, name, _ in rows]
    pending_upload_ids = {key for key, _, pending in rows if pending}
    managed_ids = {key for key, _ in schools}
    selected = next((row for row in schools if row[0] == school_id), None)
    if (base.self_service or intake_only) and school_id and selected is None:
        selected = connection.execute(
            "SELECT id,school_name FROM schools WHERE id=? AND deleted_at IS NULL", (school_id,),
        ).fetchone()
    school_id = selected[0] if selected else None
    scope = school_pilot_scope(connection,base,school_id=school_id) if school_id else None
    data = {"schools": schools, "school_id": school_id, "scope": scope, "base": base,
            "school_name": selected[1] if selected else "", "can_register": can_register,
            "pending_upload_ids": pending_upload_ids, "registered": school_id in managed_ids,
            "intake_only": intake_only}
    if (base.self_service or intake_only) and add_school:
        search = school_search.strip()[:120]
        data["school_search"] = search
        data["available_schools"] = [
            row for row in connection.execute(
                """SELECT s.id,s.school_name FROM schools s WHERE s.deleted_at IS NULL
                   AND instr(lower(s.school_name),lower(?))>0
                   AND (NOT ? OR NOT EXISTS (SELECT 1 FROM revenue_entity_provider_links l
                                               WHERE l.school_id=s.id AND l.effective_to IS NULL))
                   ORDER BY s.school_name,s.id""", (search,intake_only),
            ) if row[0] not in managed_ids
        ]
    if not scope and not (school_id and can_register):
        return data
    entity_id = scope.entity_id if scope else 0
    toorak_release = bool(scope and scope.toorak_draft_release)
    data["drafts"] = connection.execute(
        """SELECT s.uid,d.uid,CASE WHEN ? THEN 'toorak' ELSE json_extract(s.canonical_json,'$.analysis_profile') END
             FROM revenue_rule_snapshots s
           JOIN revenue_documents d ON d.uid=s.source_rule_uid
           WHERE d.document_kind=? AND json_extract(s.canonical_json,'$.toorak_draft')=1
             AND (? OR json_extract(s.canonical_json,'$.school_commission_configurable')=1)
             AND json_extract(s.canonical_json,'$.school_id')=?
             AND json_extract(s.canonical_json,'$.pilot_entity_id')=?
             AND json_extract(s.canonical_json,'$.pilot_simulation')=?
             AND NOT EXISTS (SELECT 1 FROM revenue_contract_versions v WHERE v.document_id=d.id)
           ORDER BY s.id DESC""",(toorak_release, FORMAL_CONTRACT, toorak_release,
                              school_id,entity_id,base.simulation),
    ).fetchall()
    data["draft"] = next((row for row in data["drafts"] if row[0]==draft_uid),None)
    if data["draft"] and data["draft"][2] and (base.simulation or toorak_release):
        from revenue.school_contract_review import review_state
        data["review_state"] = review_state(connection, draft_uid)
    if not scope:
        return data
    if mock_mail and scope.enabled and scope.simulation and scope.self_service:
        settings = mail_settings(connection, entity_id)
        if settings.get("transport", "mock") == "mock":
            data["invoice_mail_settings"] = settings
    data["payer"] = connection.execute(
        "SELECT legal_name FROM revenue_entities WHERE id=?",(scope.entity_id,),
    ).fetchone()[0]
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
    data["active_contract_rules"] = connection.execute(
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
        (scope.entity_id, FORMAL_CONTRACT, school_id, scope.simulation),
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


def _form(action, csrf, token, school_id, fields, button, *, upload=False, provider_id=None):
    encoding = ' enctype="multipart/form-data"' if upload else ""
    identity = _hidden("school_id", school_id) if provider_id is None else _hidden("higher_education_provider_id", provider_id)
    return f'''<form method="post" action="{ACTIONS}/{action}"{encoding} class="revenue-action-form">
      {_hidden_security(csrf,token,"multi-school-"+action)}{identity}
      {fields}<button type="submit">{button}</button></form>'''


def _tier_inputs(suffix, required):
    mandatory = " required" if required else ""
    return f'''<label>佣金比例（%）<input name="rate_percent{suffix}" inputmode="decimal"{mandatory}></label>
      <label>首年学费类别<select name="tuition_kind{suffix}"><option value="any">不限</option>
      <option value="local">本地</option><option value="international">国际</option></select></label>
      <label>适用签证子类（逗号分隔，留空不限）<input name="visa_subclasses{suffix}" maxlength="120"></label>'''


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


def _onboarding_contract(data, csrf, token):
    school_id = data["school_id"]
    school_name = escape(data["school_name"])
    body = f'<h3>{school_name} · {"待确认" if data["drafts"] else "待上传"}</h3>'
    if data["intake_only"]:
        body += '<p>当前阶段：合同</p>'
    if not data["drafts"]:
        return body + _form("configurable-draft", csrf, token, school_id, _hidden("onboarding", "1") + """
          <label>签署合同 PDF<input type="file" name="contract_file" accept=".pdf,application/pdf" required></label>
          <label><input type="checkbox" name="signed_confirmed" value="1" required>已核对签署合同</label>""",
          "上传合同", upload=True)
    for uid, _, _ in data["drafts"]:
        target = quote(uid, safe="")
        if data["intake_only"]:
            link = f"/revenue/institution-commission-drafts/{target}"
        else:
            link = f"{ROOT}?school_id={school_id}&amp;contract={target}#school-commission-contracts"
        body += f'<p><a href="{link}">继续核对合同 {escape(uid[-10:])}</a></p>'
    if data["draft"] and data["intake_only"]:
        document = quote(data["draft"][1], safe="")
        body += (f'<p>已保存签署 PDF，合同草稿待确认。'
                 f'<a href="/revenue/documents/{document}/view">查看签署 PDF</a></p>')
    if data["draft"] and not data["intake_only"]:
        from revenue.school_contract_review import confirmed_rule_inputs

        uid, document, profile = data["draft"]
        state = data["review_state"]
        body += render_contract_analysis({"uid": uid, "document_uid": document, **state},
                                         csrf, token, school_id=school_id, onboarding=True)
        review, analysis = state["review"], state["analysis"]
        if review and analysis and review["run"] == analysis["run"]:
            try:
                confirmed_rule_inputs(profile, review["fields"])
            except ValueError as error:
                body += f'<p>{escape(str(error))}</p>'
            else:
                fields = _hidden("onboarding", "1") + _hidden("draft_uid", uid)
                if not review["fields"].get("school_name", {}).get("value"):
                    fields += f'<label>合同上的付款主体<input name="payer_name" maxlength="200" value="{escape(data["school_name"], quote=True)}" required></label>'
                fields += _hidden("sample_tuition", "10000")
                body += _form("configurable-preview", csrf, token, school_id, fields, "预览并启用")
    return body


def provider_commission_intake_data(connection, simulation, provider_id, draft_uid, search):
    search = search.strip()[:120]
    rows = connection.execute(
        """SELECT p.id,p.provider_name,p.review_status,
                  EXISTS (SELECT 1 FROM revenue_audit_events a
                           WHERE a.action=? AND a.aggregate_type='school_commission_provider'
                             AND a.aggregate_uid=(? || ':' || p.id)) AS registered,
                  EXISTS (SELECT 1 FROM revenue_entity_provider_links l
                           WHERE l.higher_education_provider_id=p.id AND l.effective_to IS NULL) AS bound
             FROM higher_education_providers p
            WHERE p.active_status='Active' AND p.review_status IN ('Pending','Approved')
            ORDER BY p.provider_name,p.id""",
        (PROVIDER_REGISTRATION_ACTION, int(simulation)),
    ).fetchall()
    registered = [row for row in rows if row["registered"]]
    available = [(row["id"],row["provider_name"],row["review_status"]) for row in rows
                 if not row["registered"] and not row["bound"]
                 and search.casefold() in row["provider_name"].casefold()]
    selected = next((row for row in registered if row["id"] == provider_id), None)
    drafts = []
    if selected:
        drafts = connection.execute(
            """SELECT s.uid,d.uid AS document_uid FROM revenue_rule_snapshots s
                 JOIN revenue_documents d ON d.uid=s.source_rule_uid AND d.document_kind=?
                WHERE s.rule_kind='commission'
                  AND json_extract(s.canonical_json,'$.higher_education_provider_id')=?
                  AND json_extract(s.canonical_json,'$.pilot_simulation')=?
                  AND json_extract(s.canonical_json,'$.pilot_entity_id')=0
                ORDER BY s.id DESC""",
            (FORMAL_CONTRACT, provider_id, int(simulation)),
        ).fetchall()
    return {"search":search,"registered":registered,"available":available,"selected":selected,
            "drafts":drafts,"draft":next((row for row in drafts if row["uid"] == draft_uid), None)}


def _provider_intake(data, csrf, token):
    def provider_url(provider_id):
        return f"{ROOT}?higher_education_provider_id={provider_id}#school-commission-contracts"

    registered = "".join(
        f'<li><a href="{provider_url(row["id"])}">{escape(row["provider_name"])}</a></li>'
        for row in data["registered"]
    )
    options = "".join(
        f'<option value="{provider_id}">{escape(name)}{" · 待审核" if review=="Pending" else ""}</option>'
        for provider_id,name,review in data["available"]
    )
    register = "<p>没有符合条件的可添加机构。</p>"
    if options:
        register = f'''<form method="post" action="{ACTIONS}/register" class="revenue-action-form">
          {_hidden_security(csrf,token,"higher-education-register")}
          <label>资料库中的高等教育机构<select name="higher_education_provider_id" required>
          <option value="">请选择机构</option>{options}</select></label>
          <button type="submit">登记到佣金学校表并配置</button></form>'''
    body = f'''<section id="add-commission-provider">
      <h3>高等教育机构</h3>
      <form method="get" action="{ROOT}#add-commission-provider" class="revenue-action-form">
        {_hidden("provider_catalog","1")}
        <label>机构名称<input name="provider_search" type="search" maxlength="120" value="{escape(data["search"],quote=True)}"></label>
        <button type="submit">搜索机构</button>
      </form>{register}
      <h4>已登记的高等教育机构</h4><ul>{registered}</ul></section>'''
    provider = data["selected"]
    if not provider:
        return body
    body += f'<h3>{escape(provider["provider_name"])} · {"待确认草稿" if data["drafts"] else "待上传"}</h3><p>当前阶段：合同</p>'
    if provider["review_status"] == "Pending":
        body += '<p>机构档案待审核。可先保存合同草稿，机构审核与合同确认分别处理。</p>'
    if not data["drafts"] and not provider["bound"]:
        body += _form("configurable-draft",csrf,token,None,
                     '''<label>签署合同 PDF<input type="file" name="contract_file" accept=".pdf,application/pdf" required></label>
                     <label><input type="checkbox" name="signed_confirmed" value="1" required>已核对签署合同</label>''',
                     "上传合同",upload=True,provider_id=provider["id"])
    for draft in data["drafts"]:
        body += f'<p><a href="/revenue/institution-commission-drafts/{quote(draft["uid"],safe="")}">继续核对合同 {escape(draft["uid"][-10:])}</a></p>'
    if data["draft"]:
        document = quote(data["draft"]["document_uid"],safe="")
        body += f'<p>已保存签署 PDF，合同草稿待确认。<a href="/revenue/documents/{document}/view">查看签署 PDF</a></p>'
    return body


def render_multischool_configuration(data, csrf, token):
    school_id = data["school_id"]
    options = "".join(
        f'<option value="{key}"{" selected" if key==school_id else ""}>{escape(name)}'
        f'{" · 待上传合同" if key in data["pending_upload_ids"] else ""}</option>'
        for key,name in data["schools"]
    )
    can_register = data["can_register"]
    add_label = "从学校资料库添加" if can_register else "添加学校"
    add_button = (
        f'<p><a class="revenue-primary-link" href="{ROOT}?add_school=1#add-commission-school">{add_label}</a></p>'
        if data["base"].self_service or can_register else ""
    )
    chooser = f'''{add_button}
      <form method="get" action="{ROOT}#school-commission-contracts" class="revenue-action-form">
      <label>{'佣金学校' if can_register else '学校'}<select name="school_id" required><option value="">请选择学校</option>{options}</select></label>
      <button type="submit">打开学校配置</button></form>'''
    if data["intake_only"]:
        chooser += f'<p><a class="revenue-primary-link" href="{ROOT}?provider_catalog=1#add-commission-provider">高等教育机构</a></p>'
    if data.get("provider_intake"):
        chooser += _provider_intake(data["provider_intake"],csrf,token)
    if "available_schools" in data:
        search = escape(data["school_search"], quote=True)
        available = "".join(
            f'<option value="{key}">{escape(name)}</option>' for key,name in data["available_schools"]
        )
        select = "<p>没有符合条件的可添加学校。</p>"
        if available:
            method, target = ("post", f"{ACTIONS}/register") if can_register else ("get", f"{ROOT}#school-commission-contracts")
            security = _hidden_security(csrf, token, "multi-school-register") if can_register else ""
            button = "登记到佣金学校表并配置" if can_register else "继续添加"
            select = f'''<form method="{method}" action="{target}" class="revenue-action-form">
              {security}
              <label>资料库中的学校<select name="school_id" required><option value="">请选择学校</option>{available}</select></label>
              <button type="submit">{button}</button></form>'''
        chooser += f'''<section id="add-commission-school">
          <h3>从学校资料库添加</h3>
          <form method="get" action="{ROOT}#add-commission-school" class="revenue-action-form">
            {_hidden("add_school","1")}
            <label>学校名称<input name="school_search" type="search" maxlength="120" value="{search}"></label>
            <button type="submit">搜索学校</button>
          </form>{select}</section>'''
    body = ""
    scope = data["scope"] if school_id else None
    if school_id and not scope and can_register:
        if data["registered"]:
            body = _onboarding_contract(data, csrf, token)
        else:
            body = f'<h3>{escape(data["school_name"])}</h3>' + _form(
                "register", csrf, token, school_id, "", "登记到佣金学校表并配置",
            )
    elif school_id and not scope:
        body = f'<h3>添加 {escape(data["school_name"])}</h3>'
        body += _form("bind",csrf,token,school_id,'''
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
        body += _render_active_contract_rules(data["active_contract_rules"])
        if settings := data.get("invoice_mail_settings"):
            verification = "已核对" if settings["verified"] else "未核对"
            sending = "开启" if settings["enabled"] else "关闭"
            recipient = escape(settings["recipient"], quote=True)
            enabled = " checked" if settings["enabled"] else ""
            fields = _hidden("settings_revision", settings["revision"]) + f'''
              <label>学校发票收件邮箱（多个地址用逗号分隔）<input name="recipient" type="email" multiple value="{recipient}"
                     maxlength="2048" placeholder="finance@school.example.test"></label>
              <label><input type="checkbox" name="verified" value="1">我已核对所填收件邮箱</label>
              <label><input type="checkbox" name="enabled" value="1"{enabled}>签发后自动投递到模拟邮箱</label>'''
            body += f'''<section id="school-invoice-mail-recipient">
              <h3>学校发票收件邮箱</h3>
              <p>{verification} · 模拟发送{sending} · 抄送 {FINANCE_COPY_ADDRESS}</p>
              <p>填写 .test 地址，保存时勾选核对。更换地址后需重新核对。</p>
              {_form("mail-recipient", csrf, token, school_id, fields, "保存学校发票收件邮箱")}</section>'''
        if data["base"].self_service:
            body += _form("stage",csrf,token,school_id,
                          f'<label>佣金阶段<select name="stage">{stages}</select></label>',"保存学校阶段")
        entity = scope.entity_id
        body += f'<p><a href="/revenue/school-commission-invoices?entity_id={entity}">查看该学校的佣金发票</a>'
        if scope.enabled and STAGES.index(scope.stage)>=STAGES.index("invoicing"):
            body += f' · <a href="/revenue/commission-invoice-templates?entity_id={entity}">配置该校发票模板和财务资料</a>'
        body += '</p>'
        if scope.enabled:
            body += '<h3>上传新合同</h3>'
            body += _form("configurable-draft",csrf,token,school_id,'''
              <label>签署合同 PDF<input type="file" name="contract_file" accept=".pdf,application/pdf" required></label>
              <label><input type="checkbox" name="signed_confirmed" value="1" required>已核对签署合同</label>''',
              "登记合同草稿",upload=True)
            for uid,_,_ in data["drafts"]:
                body += f'<p><a href="{ROOT}?school_id={school_id}&amp;contract={quote(uid,safe="")}">配置合同草稿 {escape(uid[-10:])}</a></p>'
            if data["draft"]:
                uid, document, profile = data["draft"]
                if profile and (scope.simulation or scope.toorak_draft_release):
                    from revenue.school_contract_review import confirmed_rule_inputs

                    state = data["review_state"]
                    body += render_contract_analysis({"uid": uid, "document_uid": document, **state},
                                                     csrf, token, school_id=school_id)
                    review, analysis = state["review"], state["analysis"]
                    if review and analysis and review["run"] == analysis["run"]:
                        try:
                            confirmed_rule_inputs(profile, review["fields"])
                        except ValueError as error:
                            body += f'<p>{escape(str(error))}</p>'
                        else:
                            sample = f'''{_hidden("draft_uid",uid)}
                              <label>样本首年学费（AUD）<input name="sample_tuition" inputmode="decimal" required></label>'''
                            body += _form("configurable-preview", csrf, token, school_id, sample, "预览已确认规则")
                elif not profile:
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
        if scope.enabled and (scope.simulation or scope.toorak_draft_release) and data["contracts"]:
            body += f'<p><a href="/revenue/school-commission-candidates?school_id={school_id}">查看该校学生佣金清单</a></p>'
        body += "".join(
            f'<p><a href="/revenue/cases/{quote(uid,safe="")}">学生档案 {escape(student)}</a></p>'
            for uid,student in data["cases"]
        )
    title = "佣金学校" if can_register else "多学校佣金配置"
    return f'<section id="school-commission-contracts" class="revenue-panel"><h2>{title}</h2>{chooser}{body}</section>'


def render_configurable_preview(preview, csrf, token):
    values = preview["confirmation_fields"]
    school_id = values["school_id"]
    rows = "".join(
        f'<li>{row["rate_basis_points"]/100:g}% · 净额 {_aud_money(row["net_cents"])}'
        f' · GST {_aud_money(row["gst_cents"])} · 合计 {_aud_money(row["total_cents"])}</li>'
        for row in preview["rows"]
    )
    claim = preview["configuration"]["claim"]
    eligibility = ("两档签证互斥 · 代理署名申请、学校注册、课程费实收及实际开学"
                   if preview["configuration"].get("analysis_profile") == "toorak" else
                   f'开学满 {claim["commencement_days"]} 天 · 注册凭证 {"必需" if claim["registration_required"] else "无需"}')
    fields = "".join(_hidden(key,value) for key,value in values.items() if key!="school_id")
    fields += _hidden("preview_digest",preview["digest"])
    button = "确认付款主体并启用规则" if values.get("onboarding") else "确认并启用规则"
    email = preview["configuration"].get("invoice_email")
    recipient = f'<p>学校收票邮箱 {escape(email)}</p>' if email else ""
    sample = f'<p>以下按样本学费 {_aud_money(preview["rows"][0]["tuition_cents"])} 预览。</p>' if values.get("onboarding") else ""
    return f'''<section class="revenue-panel"><h2>{escape(preview["school_name"])} · 佣金规则预览</h2>
      <p>付款主体 {escape(preview["legal_name"])} · {escape(preview["effective_from"])} 至 {escape(preview["effective_to"])}</p>
      {recipient}{sample}<p>{eligibility} · 发票收件后 {claim["payment_days_from_valid_invoice"]} 天付款</p><ul>{rows}</ul>
      {_form("configurable-activate",csrf,token,school_id,fields,button)}
      <a href="{ROOT}?school_id={school_id}&amp;contract={quote(values["draft_uid"],safe="")}">返回合同草稿</a></section>'''
