"""Forms for confirming institution commission terms and producing draft invoices."""

from html import escape
from urllib.parse import quote, urlencode

from revenue.state_school_commission import STATE_AUTHORITIES
from revenue import vic_rcti
from revenue.institution_commission_draft import FEE_PERIODS, GST_MODES, NSW_CLAIMS_ONLY, TIER_LABELS, _digest
from revenue.school_contract_extract import NSW_SCHEDULE_PROFILE


ROOT = "/revenue/institution-commission-drafts"


def _hidden(name, value):
    return f'<input type="hidden" name="{name}" value="{escape(str(value),quote=True)}">'


def _options(options, value, *, allow_blank=True):
    placeholder = '<option value="">待确认</option>' if allow_blank else ""
    return placeholder + "".join(
        f'<option value="{key}"{" selected" if key==value else ""}>{escape(label)}</option>'
        for key,label in options.items()
    )


def _aud(cents):
    return f"AUD {cents // 100:,}.{cents % 100:02d}"


def _student_school(row):
    return f' · {escape(row["school_name"])}' if row.get("school_name") else ""


def _settlement_panel(invoice, settlement, action, csrf):
    if settlement is None:
        return ""
    security = _hidden("csrf", csrf)
    state = escape(STATE_AUTHORITIES[invoice["target"][1]])
    labels = {line["uid"]: escape(line["student_name"] or line["student_id"]) for line in invoice["lines"]}
    payments = []
    for item in settlement["payments"]:
        distribution = "、".join(f"{labels[key]} {_aud(cents)}" for key, cents in item["allocations"].items())
        payments.append(
            f'<li>{escape(item["received_on"])} · {escape(item["reference"])} · {_aud(item["amount_cents"])}'
            f' · {distribution} · 凭证 {escape(item["evidence_name"])}</li>'
        )
    statuses = {"unpaid": "未到账", "partial": "部分到账", "awaiting_reconciliation": "待核销", "reconciled": "已核销"}
    status = statuses[settlement["status"]]
    section = (f'<h3>到账与核销 · 隔离演练</h3><p>{state} · {status} · '
               f'累计到账 {_aud(settlement["received_cents"])} · 未收 {_aud(settlement["outstanding_cents"])}</p>'
               f'<ul>{"".join(payments)}</ul>')
    if record := settlement["reconciliation"]:
        return section + (f'<p>核销记录 {escape(record["uid"])} · 管理员 #{int(record["confirmed_by"])}'
                          f' · {escape(record["confirmed_at"])} · {_aud(record["amount_cents"])}</p>')
    if not settlement["outstanding_cents"]:
        return section + f'''<form method="post" action="{action}">
          {security}{_hidden("operation", "authority-reconcile")}
          <label><input type="checkbox" name="reviewed" value="1" required>已核对全部银行流水和分配</label>
          <button type="submit">确认核销</button></form>'''
    states = "".join(f'<option value="{key}">{escape(label)}</option>' for key, label in STATE_AUTHORITIES.items())
    allocations = ""
    if len(invoice["lines"]) > 1:
        allocations = "".join(
            f'<label>{labels[line["uid"]]}（未收 {_aud(settlement["line_balances"][line["uid"]])}）'
            f'<input name="allocation_{index}" inputmode="decimal"></label>'
            for index, line in enumerate(invoice["lines"])
        )
    return section + f'''<form method="post" action="{action}" enctype="multipart/form-data" class="revenue-action-form">
      {security}{_hidden("operation", "authority-payment")}
      <label>银行流水付款方所属州<select name="payer_state" required><option value="">请选择</option>{states}</select></label>
      <label>银行流水号<input name="bank_reference" maxlength="100" required></label>
      <label>到账日期<input name="received_on" type="date" required></label>
      <label>到账金额（AUD）<input name="received_amount" inputmode="decimal" required></label>
      {allocations}
      <label>模拟银行流水 PDF<input type="file" name="bank_proof" accept=".pdf,application/pdf" required></label>
      <button type="submit">登记到账</button></form>'''


def _formal_authority_panel(formal, action, csrf):
    if not formal:
        return ""

    def controls(operation, button, fields="", upload=False):
        encoding = ' enctype="multipart/form-data"' if upload else ""
        security = _hidden("csrf", csrf) + _hidden("operation", "authority-formal-" + operation)
        return (f'<form action="{action}" method="post"{encoding} class="revenue-action-form">'
                f'{security}{fields}<button type="submit">{button}</button></form>')

    simulation = formal["simulation"]
    school = formal.get("school", False)
    label = "学校佣金发票" if school else "教育局正式单据"
    header = f"<h3>{label}" + (" · 仅隔离验收" if simulation else "") + "</h3>"
    detail = formal["detail"]
    reviewed = '<label><input type="checkbox" name="reviewed" value="1" required>已核对</label>'
    if not detail:
        preview = f'<p><a href="{action}&amp;pdf=formal-preview&amp;view=html">预览正式版式及财务资料</a></p>'
        if school:
            from revenue.school_invoice_release import FINANCE_COPY_ADDRESS

            preview += (f'<p>收件人 {escape(formal["recipient"] or "未填写")} · '
                        f'Cc {escape(", ".join(filter(None, (FINANCE_COPY_ADDRESS, formal["cc"]))))} · '
                        f'Bcc {escape(formal["bcc"] or "未填写")}</p>')
            if not formal["verified"]:
                return header + preview + "<p>请先保存开票资料并核对学校收票邮箱。</p>"
        button = "确认签发测试 COM 发票" if simulation else "确认签发 COM 发票"
        if school and formal["auto_mail"]:
            button += "并发送"
        return header + preview + (controls(
            "issue", button, _hidden("preview_token",formal["preview_token"])+reviewed,
        ) if formal.get("can_issue", simulation) else "")
    summary = detail["settlement"]
    status = {"unpaid":"未到账","partial":"部分到账","awaiting_reconciliation":"待核销","reconciled":"已核销"}
    html = (f'{header}<p>{escape(detail["invoice_number"])} · {status[summary["status"]]} · '
            f'应收 {_aud(detail["gross_cents"])} · '
            f'已收 {_aud(summary["received_cents"])} · 未收 {_aud(summary["outstanding_cents"])}</p>'
            f'<p><a href="{action}&amp;pdf=formal" target="_blank" rel="noopener">查看签发原版 PDF</a></p>')
    mail = formal["mail"]
    if not mail:
        html += "<p>未安排发信，请核对签发时的收票邮箱和发信设置。</p>"
    else:
        from revenue.school_invoice_views import render_school_mail_status
        html += render_school_mail_status(detail["uid"], mail, csrf, "", False,
                                          attachment_href=action + "&pdf=formal")
        if simulation and mail["status"] == "pending":
            html += controls("dispatch","投递到隔离邮箱")
        if mail["status"] == "failed" and formal.get("can_issue", simulation):
            html += controls("retry","重试发送",_hidden("attempt_count",mail["attempt_count"]))
    proof = '<label>隔离凭证 PDF<input name="bank_proof" type="file" accept=".pdf" required></label>'
    date = '<label>日期<input name="received_on" type="date" required></label>'
    delivery = summary["delivery"]
    can_settle = formal.get("can_settle", simulation)
    if summary.get("due_date"):
        html += f'<p>到期 {escape(summary["due_date"])}</p>'
    if reminder := formal.get("reminder"):
        html += (f'<details><summary>催款草稿 · 未收 {_aud(summary["outstanding_cents"])}</summary>'
                 f'<p>{escape(reminder["subject"])}</p><pre>{escape(reminder["body"])}</pre></details>')
    if not delivery:
        return html + (controls("delivery","确认学校收件日" if school else "确认教育局收件日",
                                date+proof,upload=True) if can_settle else "")
    def evidence(document_uid):
        if not school or not document_uid:
            return ""
        link = f'{action}&amp;pdf=proof&amp;document={quote(document_uid, safe="")}'
        return f' · <a href="{link}" target="_blank" rel="noopener">查看凭证</a>'

    html += f'<p>学校收件 {escape(delivery["received_on"])}{evidence(delivery["document_uid"])}</p>'
    for row in summary["payments"]:
        html += (f'<p>{escape(row["received_date"])} · {escape(row["external_reference"])} · '
                 f'{_aud(row["invoice_cents"])} · 明细 {escape(row["invoice_line_uid"][-8:])}'
                 f'{evidence(row["document_uid"])}</p>')
    if record := summary["reconciliation"]:
        return html + f'<p>核销记录 {escape(record["uid"])} · {escape(record["confirmed_at"])}</p>'
    if not can_settle:
        return html
    if not summary["outstanding_cents"]:
        return html + controls("reconcile","确认正式核销",reviewed)
    payers = {detail["revenue_entity_id"]: detail["legal_name"]} if school else STATE_AUTHORITIES
    options = "".join(f'<option value="{key}">{escape(label)}</option>' for key,label in payers.items())
    payment = (f'<label>银行流水付款方<select name="payer_state" required><option value="">请选择</option>{options}</select></label>'
               '<label>银行流水号<input name="bank_reference" maxlength="100" required></label>'
               '<label>到账金额（AUD）<input name="received_amount" inputmode="decimal" required></label>')
    if len(detail["lines"]) > 1:
        for i, line in enumerate(detail["lines"]):
            payment += (f'<label>{escape(line["description"])}（未收 {_aud(line["outstanding_cents"])}）'
                        f'<input name="allocation_{i}" inputmode="decimal"></label>')
    return html + controls("payment","登记正式到账",payment+date+proof,upload=True)


def render_invoice_preview(url, invoice_uid, pdf_kind, *, image_uri="", page=1, page_count=1):
    def href(**params):
        return escape(f"{url}?{urlencode({'invoice': invoice_uid, **params})}", quote=True)

    back = href()
    download = href(pdf=pdf_kind, view="pdf")
    controls = (f'<p><a href="{back}">返回发票草稿</a> · '
                f'<a href="{download}">下载原 PDF</a></p>')
    if not image_uri:
        content = '<p role="alert">暂时无法显示网页预览，可下载原 PDF。</p>'
    else:
        navigation = []
        for number, label in ((page - 1, "上一页"), (page + 1, "下一页")):
            if 1 <= number <= page_count:
                link = href(pdf=pdf_kind, view="html", page=number)
                navigation.append(f'<a href="{link}">{label}</a>')
        pages = f'<p>第 {page} / {page_count} 页 {" · ".join(navigation)}</p>' if page_count > 1 else ""
        content = (
            pages + f'<img src="{escape(image_uri, quote=True)}" alt="发票草稿第 {page} 页" '
            'style="display:block;width:100%;max-width:850px;margin:0 auto">'
        )
    return '<section class="revenue-panel"><h1>发票草稿预览</h1>' + controls + content + '</section>'


def _finding_source(finding, document):
    evidence = "".join(
        f'<li><a href="/revenue/documents/{document}/view#page={proof["page"]}" target="_blank" rel="noopener">'
        f'第 {proof["page"]} 页 · {escape(proof["clause"])}</a><blockquote lang="en">{escape(proof["text"])}</blockquote></li>'
        for proof in finding.get("sources", [])
    )
    return f'<details><summary>核对合同原文</summary><ul>{evidence}</ul></details>' if evidence else ""


def _render_nsw_review(analysis, review, editing, form, document, url, security):
    fields = analysis["fields"]
    saved = review["values"] if review else {}
    gst_modes = {**GST_MODES, "exclusive": "未含 GST（按适用税额另计）"}
    labels = {
        "school_name": "合同付款主体", "start": "合同生效日", "end": "合同到期日",
        "standard": "基础佣金（%）", "iec_rate": "IEC 佣金（%）", "gst": "GST",
        "payment_days": "收到有效发票后付款天数", "conditions": "申领条件",
    }
    bases = {
        "standard": "首 12 个月学费，课程不足 12 个月按全程学费。",
        "iec_rate": "符合条件时按 IEC 学费另计，最多两个学期。",
    }
    rows = []
    for name, label in labels.items():
        finding = fields.get(name, {})
        extracted = finding.get("value", "")
        value = saved.get(name, extracted)
        if editing and form and isinstance(form.get(name), str):
            value = form[name]
        if editing and name == "gst":
            control = f'<select name="gst" required>{_options(gst_modes, value)}</select>'
        elif name == "conditions":
            attributes = 'maxlength="6000" required' if editing else "readonly"
            control = f'<textarea name="{name}" rows="4" {attributes}>{escape(value)}</textarea>'
        else:
            kind = "date" if editing and name in ("start", "end") else "text"
            if name == "gst":
                value = gst_modes.get(value, value)
            placeholder = "可留空" if name == "payment_days" and editing else (
                "合同未识别出固定天数" if name == "payment_days" else "待核对"
            )
            attributes = "readonly"
            if editing:
                attributes = {"payment_days": "", "school_name": 'maxlength="200"'}.get(name, "required")
            control = (f'<input type="{kind}" name="{name}" value="{escape(value, quote=True)}" '
                       f'placeholder="{placeholder}" {attributes}>')
        basis = f'<small>{bases[name]}</small>' if name in bases else ""
        comparison = ""
        if name in saved and saved[name] != extracted:
            original = gst_modes.get(extracted, extracted) if name == "gst" else extracted
            comparison = f'<p>系统提取：{escape(original) if original else "空白"}</p>'
        rows.append(f'<div><label>{label}{control}{basis}</label>{comparison}{_finding_source(finding, document)}</div>')
    heading = '<h3>核对 NSW 佣金条款</h3>'
    status = '<p role="status">已保存核对结果</p>' if review and not editing else ""
    safe_url = escape(url, quote=True)
    if not editing:
        edit_url = escape(url + ("&" if "?" in url else "?") + "edit=1", quote=True)
        enable = ""
        if review:
            enable = (f'<form method="post" action="{safe_url}">{security("nsw-enable")}'
                      f'{_hidden("operation", "nsw-enable")}{_hidden("review_revision", review["revision"])}'
                      '<button class="primary" type="submit">启用申领</button></form>')
        return (heading + status + f'<p><a class="secondary button-link" href="{edit_url}">修改条款</a></p>'
                + "".join(rows) + enable)
    hidden = (security("nsw-review") + _hidden("operation", "nsw-review")
              + _hidden("analysis_revision", _digest(analysis))
              + _hidden("review_revision", review["revision"] if review else ""))
    return (heading + f'<form method="post" action="{safe_url}" class="revenue-action-form">'
            + hidden + "".join(rows) + '<button class="primary" type="submit">保存核对结果</button>'
            + f' <a href="{safe_url}">取消</a></form>')


def render_workflow(workflow, csrf, token, *, analysis=None, terms=None, invoice=None, error="", form=None,
                    contract_only=False, settlement=None, formal_authority=None, nsw_review=None, edit_nsw=False):
    url = f"{ROOT}/{quote(workflow.uid,safe='')}"
    if contract_only:
        url += "?contract_only=1"
    document = quote(workflow.document["uid"],safe="")
    base = _hidden("csrf",csrf)
    security = lambda suffix: base + _hidden("idempotency_key",f"institution-commission:{token}:{suffix}")
    nsw_claims = (terms or {}).get("payment_mode") == NSW_CLAIMS_ONLY
    title = "NSW 学生与预计佣金" if nsw_claims else "核对佣金合同" if contract_only else "佣金合同与发票草稿"
    back_href = "/revenue/contracts/upload-school" if contract_only else "/revenue/contracts"
    back_label = "返回上传页" if contract_only else "返回学校佣金合同"
    header = (f'<section class="revenue-panel"><h2>{escape(workflow.name)} · {title}</h2>'
              f'<p><a href="{back_href}">{back_label}</a>'
              f' · <a href="/revenue/documents/{document}/view" target="_blank" rel="noopener">查看签署 PDF</a></p>'
              f'{f"<p role=alert>{escape(error)}</p>" if error else ""}')
    if invoice:
        if workflow.draft_needs_repreview(invoice, terms):
            action = f'{url}?invoice={quote(invoice["uid"], safe="")}'
            return (header + '<p role="status">合同条款已更新，请重新预览此草稿。</p>'
                    + f'<form method="post" action="{action}">{security("invoice-repreview")}'
                    + _hidden("operation", "invoice-repreview") + _hidden("terms_revision", terms["revision"])
                    + '<button type="submit">按最新条款重新预览</button></form></section>')
        lines = "".join(
            f'<li>{escape(row["student_name"])}{_student_school(row)} · {escape(row["academic_period"])} · {_aud(row["gross_cents"])}</li>'
            for row in invoice["lines"]
        )
        gst_control = ""
        if workflow.target[0] in {"school", "provider"}:
            mode = invoice.get("gst_mode", terms["gst"])
            if not workflow.invoice_issued(invoice["uid"]):
                action = f'{url}?invoice={quote(invoice["uid"], safe="")}'
                gst_control = (
                    f'<form method="post" action="{action}" class="revenue-action-form">'
                    f'{security("invoice-gst")}{_hidden("operation", "invoice-gst")}'
                    f'{_hidden("draft_revision", _digest(invoice))}'
                    f'<label>发票 GST<select name="gst_mode" required>{_options(GST_MODES, mode, allow_blank=False)}</select></label>'
                    '<button type="submit">更新 GST 并重新预览</button></form>'
                )
            gst_control += f'<p>GST 方式：{GST_MODES[mode]} · 未税 {_aud(invoice["net_cents"])} · GST {_aud(invoice["gst_cents"])}</p>'
        return (header + f'<h3>发票草稿已生成</h3><p>付款主体 {escape(invoice["payer"])}</p><ul>{lines}</ul>'
                + gst_control + f'<p>含税合计 {_aud(invoice["gross_cents"])}</p>'
                f'<p><a href="{url}?invoice={quote(invoice["uid"],safe="")}&amp;pdf=1&amp;view=html">'
                '预览发票草稿 PDF</a></p>'
                + _settlement_panel(invoice, settlement, f'{url}?invoice={quote(invoice["uid"], safe="")}', csrf)
                + _formal_authority_panel(formal_authority, f'{url}?invoice={quote(invoice["uid"], safe="")}', csrf)
                + '</section>')
    if not terms:
        header += f'<details><summary>删除本次上传</summary><form method="post" action="{url}">{security("discard")}{_hidden("operation", "discard")}<p>删除后可重新上传正确的 PDF。</p><button type="submit">确认删除本次上传</button></form></details>'
        if analysis["profile"] == "unsupported":
            return header + '<p>合同已保存。检测到尚未支持的多档佣金，请查看 PDF 原文核对各档规则。</p></section>'
        if analysis["profile"] == NSW_SCHEDULE_PROFILE:
            return header + _render_nsw_review(analysis, nsw_review, edit_nsw, form, document, url, security) + "</section>"
        labels = {
            "school_name":"合同付款主体", "start":"合同生效日", "end":"合同到期日",
            "conditions":"申领条件", "payment_days":"收到有效发票后付款天数",
            "gst":"GST", "tuition_period":"计佣学费周期",
            **{name:label+"佣金（%）" for name,label in TIER_LABELS.items()},
        }
        fields = analysis["fields"]
        rcti = analysis["profile"] == vic_rcti.PROFILE
        names = ["school_name","start","end","tuition_period","gst"]
        if rcti:
            labels.update(standard="基础佣金（%）", english_bonus="强化英语课程额外奖励（%）", payment_mode="结算方式")
            names += ["standard", "english_bonus", "payment_mode", "conditions"]
        else:
            names += ["payment_days", "conditions"]
            names += [key for key in TIER_LABELS if (key == "standard") == (analysis["profile"] == "standard")]
        rows = []
        for name in names:
            finding = fields.get(name) or {}
            value = finding.get("value","")
            if form and isinstance(form.get(name), str):
                value = form[name]
            if name == "payment_mode":
                control = f'<p>{escape(value)}</p>'
            elif name in {"gst","tuition_period"}:
                control = f'<select name="{name}" required>{_options(GST_MODES if name=="gst" else FEE_PERIODS,value)}</select>'
            elif name == "conditions":
                control = f'<textarea name="{name}" rows="5" maxlength="6000" required>{escape(value)}</textarea>'
            else:
                kind = "date" if name in {"start","end"} else "text"
                control = f'<input type="{kind}" name="{name}" value="{escape(value,quote=True)}" placeholder="待确认" required>'
            rows.append(f'<div><label>{labels[name]}{control}</label>{_finding_source(finding, document)}</div>')
        notice = "已提取条款，请对照原文核对，空白内容需要填写。"
        if analysis["status"] != "read":
            notice += " 部分 PDF 内容未能识别。"
        return header + f'''<h3>核对佣金条款</h3><p>{notice}</p>
          <form method="post" action="{url}" class="revenue-action-form">
            {security("terms")}{_hidden("operation","terms")}{_hidden("analysis_revision",_digest(analysis))}
            {"".join(rows)}
            <label><input type="checkbox" name="reviewed" value="1" required>已核对签署合同与以上条款</label>
            <button class="primary" type="submit">{"确认佣金信息" if contract_only else "确认佣金信息并查看学生"}</button></form></section>'''
    candidates = list(workflow.candidates(terms))
    if terms.get("payment_mode") == vic_rcti.PAYMENT:
        return header + _vic_estimate_panel(candidates, terms, url, security) + "</section>"
    if nsw_claims:
        return header + _nsw_estimate_panel(candidates, terms, url, security) + "</section>"
    items, blockers = [], []
    if workflow.target[0] == "school":
        from revenue.read_models import RevenueReadModels

        model = RevenueReadModels(workflow.db)
        payer = workflow._billing_payer()
        linked, _ = model.school_commission_student_history(
            workflow.target[1], payer["id"] if payer else None, candidates,
        ) if {"school_id", "student_id", "acceptance_status"} <= model._columns("cycle_final_acceptances") else ([], [])
        blockers.extend(f'<li>{escape(row["student_name"] or row["student_id"])} · '
                        '暂无可申领的入学及佣金记录</li>' for row in linked)
    for row in candidates:
        name = row["student_name"]
        student = escape(name if name and name != "姓名待关联" else row["student_id"]) + _student_school(row)
        if row.get("uid"):
            items.append(f'<label><input type="checkbox" name="selection" value="{escape(row["uid"],quote=True)}">'
                         f'{student} · {escape(row["academic_period"])} · {_aud(row["gross_cents"])}</label>')
            continue
        evidence_form = ""
        if row.get("needs_evidence"):
            tiers = {key:TIER_LABELS[key] for key in terms["rates"]}
            evidence_form = f'''<details><summary>核对申领凭证</summary><p>{escape(terms["conditions"])}</p>
              <form action="{url}" method="post" class="revenue-action-form">
                {security("evidence:"+row["component_uid"])}{_hidden("operation","evidence")}
                {_hidden("component",row["component_uid"])}{_hidden("terms_revision",terms["revision"])}
                <label>计佣档位<select name="tier" required>{_options(tiers,"standard" if "standard" in tiers else "")}</select></label>
                <label>凭证编号或来源<input name="reference" maxlength="240" required></label>
                <label><input type="checkbox" name="eligible" value="1" required>已核对该学生满足合同的全部申领条件</label>
                <button type="submit">保存凭证核对结果</button></form></details>'''
        blockers.append(f'<li>{student} · {escape(row["reason"])}{evidence_form}</li>')
    selectable = ""
    if items:
        revision = _digest(sorted(row["revision"] for row in candidates if row.get("uid")))
        selectable = f'''<form method="post" action="{url}" class="revenue-action-form">
           {security("invoice")}{_hidden("operation","invoice")}{_hidden("terms_revision",terms["revision"])}
           {_hidden("selection_revision",revision)}{"".join(items)}
           {'<label>客户备注（选填）<textarea name="customer_note" maxlength="500" rows="2"></textarea></label>'
            if workflow.target[0] in {"school", "provider"} else ""}
           <button class="primary" type="submit">生成发票草稿</button></form>'''
    else:
        selectable = "<p>暂无可勾选的学生。</p>" if candidates else "<p>CRM 暂无与该机构关联的学生入学资料。</p>"
    history = "".join(f'<li><a href="{url}?invoice={quote(row["uid"],safe="")}">{escape(row["created_on"])} · {_aud(row["gross_cents"])} · 发票草稿</a></li>'
                      for row in workflow._drafts_for_target() if row["contract_draft_uid"]==workflow.uid)
    return (header + f'<h3>合同条款已确认</h3><p>{escape(terms["start"])} 至 {escape(terms["end"])} · '
            f'{FEE_PERIODS[terms["period"]]} · {GST_MODES[terms["gst"]]}</p><h3>可申领学生</h3>{selectable}'
            f'<h3>待补资料或已开草稿</h3><ul>{"".join(blockers)}</ul><h3>已生成的发票草稿</h3><ul>{history}</ul></section>')



def _nsw_estimate_panel(rows, terms, url, security):
    entries = []
    for row in rows:
        component = row["component_uid"]
        body = ""
        if "gross_cents" in row:
            body = (f'<p>基础佣金 {_aud(row["standard_cents"])} · IEC 佣金 {_aud(row["iec_cents"])}'
                    f' · GST {_aud(row["gst_cents"])} · 含税合计 <strong>{_aud(row["gross_cents"])}</strong></p>')
        elif row.get("reason"):
            body = f'<p>{escape(row["reason"])}</p>'
        if row.get("needs_evidence") or "gross_cents" in row:
            body += f"""<details><summary>核对学费与申领凭证</summary>
              <form method="post" action="{url}" class="revenue-action-form">
                {security(component)}{_hidden("operation", "evidence")}
                {_hidden("component", component)}{_hidden("terms_revision", terms["revision"])}
                <label>普通课程计佣周期<select name="base_period" required>
                  <option value="">请选择</option><option value="first_year">首年（课程不少于 12 个月）</option>
                  <option value="whole_course">全程（课程短于 12 个月）</option></select></label>
                <label>另计 IEC 佣金<select name="iec" required>
                  <option value="">请选择</option><option value="no">否</option><option value="yes">是</option>
                </select></label>
                <fieldset><legend>符合 IEC 条件时填写</legend>
                  <label><input type="checkbox" name="iec_followed_secondary" value="1">已开始 IEC，并衔接 NSW 公立中学课程</label>
                  <label>IEC 实际开学日<input type="date" name="iec_commenced_on"></label>
                  <label>合资格 IEC 学费（AUD）<input type="text" inputmode="decimal" name="iec_tuition"></label>
                  <label>IEC 学期数<input type="number" name="iec_terms" min="1" max="2"></label>
                  <label>IEC 周数<input type="number" name="iec_weeks" min="1" max="20"></label>
                </fieldset>
                <label>凭证或核对记录<input name="reference" required maxlength="240"></label>
                <label><input type="checkbox" name="eligible" value="1" required>已核对合同申领条件和凭证</label>
                <button type="submit">保存并计算</button>
              </form></details>"""
        entries.append(f'<article><h3>{escape(row["student_name"])}{_student_school(row)}</h3>{body}</article>')
    return '<p role="status">申领已启用</p>' + ("".join(entries) or "<p>暂无该教育局关联学生。</p>")


def _vic_estimate_panel(rows, terms, url, security):
    lines = []
    for row in rows:
        student = escape(row["student_name"] or row["student_id"]) + _student_school(row)
        status = escape(row.get("reason", ""))
        if "standard_cents" in row:
            status = (f'基础佣金 {_aud(row["standard_cents"])} · 英语奖励 {_aud(row["english_bonus_cents"])}'
                      f' · GST {_aud(row["gst_cents"])} · 预计合计 <strong>{_aud(row["gross_cents"])}</strong>')
        controls = ""
        if row.get("needs_evidence") or "standard_cents" in row:
            controls = f'''<details><summary>核对录取与实付凭证</summary>
              <form action="{url}" method="post" class="revenue-action-form">
                {security("evidence:"+row["component_uid"])}{_hidden("operation","evidence")}
                {_hidden("component",row["component_uid"])}{_hidden("terms_revision",terms["revision"])}
                <label>教育局首次录取函日期<input type="date" name="offer_on" required></label>
                <label>凭证编号或来源<input name="reference" maxlength="240" required></label>
                <label>签证<select name="visa" required><option value="">请选择</option><option value="500">500 学生签证</option></select></label>
                <label>学校课程期限<select name="duration" required><option value="">请选择</option>
                    <option value="full">至少 12 个月</option><option value="short">不足 12 个月</option></select></label>
                <label>首年已付学费（AUD）<input name="paid_tuition" inputmode="decimal"></label>
                <label>符合两学期强化英语课程<select name="english" required><option value="">请选择</option>
                    <option value="no">否</option><option value="yes">是</option></select></label>
                <label>英语课程已付学费（AUD）<input name="english_paid" inputmode="decimal"></label>
                <label><input type="checkbox" name="studying" value="1">已核对实际就读至少四周</label>
                <label><input type="checkbox" name="eligible" value="1" required>已核对本公司招募并收到首次录取函、学生无欠费及其他适用条件</label>
                <button type="submit">保存凭证并更新预计佣金</button></form></details>'''
        lines.append(f"<li>{student} · {status}{controls}</li>")
    content = "<ul>" + "".join(lines) + "</ul>" if lines else "<p>CRM 暂无与维州公校关联的学生入学资料。</p>"
    return ("<h3>维州预计佣金</h3><p>" + vic_rcti.BLOCK_INVOICE
            + "</p><p>基础按首年已付学费，奖励按符合条件的英语课程实付学费分别计算。</p>" + content)
