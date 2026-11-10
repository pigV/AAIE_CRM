"""Forms for confirming institution commission terms and producing draft invoices."""

from html import escape
from urllib.parse import quote

from revenue.state_school_commission import STATE_AUTHORITIES
from revenue import vic_rcti
from revenue.institution_commission_draft import FEE_PERIODS, GST_MODES, TIER_LABELS, _digest


ROOT = "/revenue/institution-commission-drafts"


def _hidden(name, value):
    return f'<input type="hidden" name="{name}" value="{escape(str(value),quote=True)}">'


def _options(options, value):
    return '<option value="">待确认</option>' + "".join(
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
    header = "<h3>教育局正式单据" + (" · 仅隔离验收" if simulation else "") + "</h3>"
    detail = formal["detail"]
    reviewed = '<label><input type="checkbox" name="reviewed" value="1" required>已核对</label>'
    if not detail:
        preview = f'<p><a href="{action}&amp;pdf=formal-preview" target="_blank" rel="noopener">预览正式版式及财务资料</a></p>'
        return header + preview + (controls(
            "issue","确认签发测试 COM 发票" if simulation else "确认签发 COM 发票",_hidden("preview_token",formal["preview_token"])+reviewed,
        ) if formal.get("can_issue", simulation) else "")
    summary = detail["settlement"]
    status = {"unpaid":"未到账","partial":"部分到账","awaiting_reconciliation":"待核销","reconciled":"已核销"}
    html = (f'{header}<p>{escape(detail["invoice_number"])} · {status[summary["status"]]} · '
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
    if not delivery:
        return html + (controls("delivery","确认教育局收件日",date+proof,upload=True) if can_settle else "")
    html += f'<p>收件 {escape(delivery["received_on"])} · 到期 {escape(delivery["due_date"])}</p>'
    for row in summary["payments"]:
        html += (f'<p>{escape(row["received_date"])} · {escape(row["external_reference"])} · '
                 f'{_aud(row["invoice_cents"])} · 明细 {escape(row["invoice_line_uid"][-8:])}</p>')
    if record := summary["reconciliation"]:
        return html + f'<p>核销记录 {escape(record["uid"])} · {escape(record["confirmed_at"])}</p>'
    if not can_settle:
        return html
    if not summary["outstanding_cents"]:
        return html + controls("reconcile","确认正式核销",reviewed)
    options = "".join(f'<option value="{key}">{escape(label)}</option>' for key,label in STATE_AUTHORITIES.items())
    payment = (f'<label>银行流水付款方<select name="payer_state" required><option value="">请选择</option>{options}</select></label>'
               '<label>银行流水号<input name="bank_reference" maxlength="100" required></label>'
               '<label>到账金额（AUD）<input name="received_amount" inputmode="decimal" required></label>')
    if len(detail["lines"]) > 1:
        for i, line in enumerate(detail["lines"]):
            payment += (f'<label>{escape(line["description"])}（未收 {_aud(line["outstanding_cents"])}）'
                        f'<input name="allocation_{i}" inputmode="decimal"></label>')
    return html + controls("payment","登记正式到账",payment+date+proof,upload=True)


def render_workflow(workflow, csrf, token, *, analysis=None, terms=None, invoice=None, error="", form=None,
                    contract_only=False, settlement=None, formal_authority=None):
    url = f"{ROOT}/{quote(workflow.uid,safe='')}"
    if contract_only:
        url += "?contract_only=1"
    document = quote(workflow.document["uid"],safe="")
    base = _hidden("csrf",csrf)
    security = lambda suffix: base + _hidden("idempotency_key",f"institution-commission:{token}:{suffix}")
    title = "核对佣金合同" if contract_only else "佣金合同与发票草稿"
    back_href = "/revenue/contracts/upload-school" if contract_only else "/revenue/contracts"
    back_label = "返回上传页" if contract_only else "返回学校佣金合同"
    header = (f'<section class="revenue-panel"><h2>{escape(workflow.name)} · {title}</h2>'
              f'<p><a href="{back_href}">{back_label}</a>'
              f' · <a href="/revenue/documents/{document}/view" target="_blank" rel="noopener">查看签署 PDF</a></p>'
              f'{f"<p role=alert>{escape(error)}</p>" if error else ""}')
    if invoice:
        lines = "".join(
            f'<li>{escape(row["student_name"])}{_student_school(row)} · {escape(row["academic_period"])} · {_aud(row["gross_cents"])}</li>'
            for row in invoice["lines"]
        )
        return (header + f'<h3>发票草稿已生成</h3><p>付款主体 {escape(invoice["payer"])}</p><ul>{lines}</ul><p>含税合计 {_aud(invoice["gross_cents"])}</p>'
                f'<p><a href="{url}?invoice={quote(invoice["uid"],safe="")}&amp;pdf=1" target="_blank" rel="noopener">'
                '预览发票草稿 PDF</a></p>'
                + _settlement_panel(invoice, settlement, f'{url}?invoice={quote(invoice["uid"], safe="")}', csrf)
                + _formal_authority_panel(formal_authority, f'{url}?invoice={quote(invoice["uid"], safe="")}', csrf)
                + '</section>')
    if not terms:
        header += f'<details><summary>删除本次上传</summary><form method="post" action="{url}">{security("discard")}{_hidden("operation", "discard")}<p>删除后可重新上传正确的 PDF。</p><button type="submit">确认删除本次上传</button></form></details>'
        if analysis["profile"] == "unsupported":
            return header + '<p>合同已保存。检测到尚未支持的多档佣金，请查看 PDF 原文核对各档规则。</p></section>'
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
            evidence = "".join(
                f'<li><a href="/revenue/documents/{document}/view#page={proof["page"]}" target="_blank" rel="noopener">'
                f'第 {proof["page"]} 页 · {escape(proof["clause"])}</a><blockquote lang="en">{escape(proof["text"])}</blockquote></li>'
                for proof in finding.get("sources",[])
            )
            source = f'<details><summary>核对合同原文</summary><ul>{evidence}</ul></details>' if evidence else ""
            rows.append(f'<div><label>{labels[name]}{control}</label>{source}</div>')
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
    items, blockers = [], []
    for row in candidates:
        student = escape(row["student_name"] or row["student_id"]) + _student_school(row)
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
           <button class="primary" type="submit">生成发票草稿</button></form>'''
    else:
        selectable = "<p>暂无可勾选的学生。</p>" if candidates else "<p>CRM 暂无与该机构关联的学生入学资料。</p>"
    history = "".join(f'<li><a href="{url}?invoice={quote(row["uid"],safe="")}">{escape(row["created_on"])} · {_aud(row["gross_cents"])} · 发票草稿</a></li>'
                      for row in workflow._drafts_for_target() if row["contract_draft_uid"]==workflow.uid)
    return (header + f'<h3>合同条款已确认</h3><p>{escape(terms["start"])} 至 {escape(terms["end"])} · '
            f'{FEE_PERIODS[terms["period"]]} · {GST_MODES[terms["gst"]]}</p><h3>可申领学生</h3>{selectable}'
            f'<h3>待补资料或已开草稿</h3><ul>{"".join(blockers)}</ul><h3>已生成的发票草稿</h3><ul>{history}</ul></section>')



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
