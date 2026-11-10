"""Forms for reviewing, confirming, and discarding signed contract uploads."""

from html import escape
from urllib.parse import quote

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

def render_workflow(workflow, csrf, token, *, analysis=None, terms=None, error="", form=None):
    url = f"{ROOT}/{quote(workflow.uid, safe='')}?contract_only=1"
    document = quote(workflow.document["uid"], safe="")
    base = _hidden("csrf", csrf)
    def security(suffix):
        return base + _hidden("idempotency_key", f"institution-commission:{token}:{suffix}")
    header = (f'<section class="revenue-panel"><h2>{escape(workflow.name)} · 核对佣金合同</h2>'
              '<p><a href="/revenue/contracts/upload-school">返回上传页</a>'
              f' · <a href="/revenue/documents/{document}/view" target="_blank" rel="noopener">查看签署 PDF</a></p>'
              f'{f"<p role=alert>{escape(error)}</p>" if error else ""}')
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
            <button class="primary" type="submit">确认佣金信息</button></form></section>'''
    return header + "<p>合同条款已确认。</p></section>"


def _aud(cents):
    return f"AUD {cents // 100:,}.{cents % 100:02d}"

def _student_school(row):
    return f' · {escape(row["school_name"])}' if row.get("school_name") else ""

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

def render_vic_estimates(workflow, terms, csrf, token):
    url = f"{ROOT}/{quote(workflow.uid, safe='')}"
    def security(suffix):
        return _hidden("csrf", csrf) + _hidden("idempotency_key", f"institution-commission:{token}:{suffix}")
    return ('<section class="revenue-panel"><p><a href="/revenue/contracts?education_authority=VIC">返回维州合同</a></p>'
            + _vic_estimate_panel(workflow.candidates(terms), terms, url, security) + "</section>")
