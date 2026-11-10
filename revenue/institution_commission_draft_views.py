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
