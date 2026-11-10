"""CRM views for VIC RCTI comparison and receipt records."""
from html import escape
from urllib.parse import quote

from revenue.money import aud_cents
from revenue.vic_rcti_reconciliation import HEADER, URL
from revenue.views import _hidden_security


LABELS = dict(issuer="出具方", abn="出具方 ABN", number="RCTI 单号", date="出具日期",
              net="未税金额（AUD）",
              gst="GST（AUD）", gross="含税总额（AUD）")
STEPS = (("上传单据", "拖入 RCTI PDF，另有学生明细时一起上传 CSV。"),
         ("记录到账并核对", "填写银行实收，核对 CRM 预计佣金和差额。"))


def _text(value):
    return escape(str(value if value is not None else ""), quote=True)


def _money(cents):
    return f"AUD {cents/100:,.2f}"


def _hidden(name, value):
    return f'<input type="hidden" name="{_text(name)}" value="{_text(value)}">'


def _progress(step):
    rows = []
    for i, (name, help_text) in enumerate(STEPS, 1):
        active = ' aria-current="step"' if i == step else ""
        state = "已完成" if i < step else "当前步骤" if i == step else "待处理"
        rows.append(f'<li{active}><strong>{i}. {name}</strong> · {state}<p>{help_text}</p></li>')
    return '<nav aria-label="RCTI 对账步骤"><ol>' + "".join(rows) + "</ol></nav>"


def _state(row):
    return "已记录到账" if row["bank"] else "待记录到账"


OUTCOMES = {"crm": "已修正 CRM 资料", "crm_amount": "已更正 CRM 预计佣金",
            "reissued": "已收到教育局更正的 RCTI", "explained": "已核实差异原因"}


def _student_label(student):
    name = student.get("student_name")
    return name if name and name != "姓名待关联" else student.get("student_id") or student.get("student_number", "")


def _highlight(value, pending):
    name = _text(value)
    return f'<mark class="vic-rcti-difference-student">{name}</mark>' if pending else name


def render(workflow, service, csrf, token, *, record=None, error="", batch_complete=False):
    root = f"{URL}/{quote(workflow.uid, safe='')}"
    uid = quote(record["uid"], safe="") if record else ""
    url = f"{root}/{uid}" if record else root
    back = "/revenue/contracts?education_authority=VIC"
    def form(action, fields, button, files=False):
        enctype = ' enctype="multipart/form-data"' if files else ""
        return (f'<form class="revenue-action-form" action="{url}" method="post"{enctype}>'
                f'{_hidden_security(csrf,token,action)}{_hidden("operation",action)}{fields}'
                f'<button type="submit" class="primary">{button}</button></form>')

    if record:
        title = "RCTI " + _text(record["review"]["header"].get("number") or "待核对单号")
        back = root
        back_label = "返回 RCTI 列表"
        layout = " vic-rcti-record"
    else:
        title, back_label, layout = "维州教育局 · RCTI 对账", "返回维州教育局", ""
    body = (f'<link rel="stylesheet" href="/assets/revenue.css"><main class="revenue-workspace{layout}">'
            f'<header class="revenue-dashboard-heading"><h1>{title}</h1>'
            f'<a href="{back}">{back_label}</a></header>')
    if not record:
        body += '<section class="revenue-panel">' + _progress(1)
    if error:
        body += f'<p role="alert">{_text(error)}</p>'
    csv_input = '<label>学生明细 CSV<input type="file" name="students" accept=".csv,text/csv"></label>'
    if not record:
        items = []
        entries = sorted(service.list_differences(), key=lambda entry: not entry[2]["pending"])
        for item, model, difference in entries:
            number = _text(item["review"]["header"].get("number") or "待核对单号")
            href = f'{root}/{quote(item["uid"], safe="")}'
            student = model.get("number_student") or {}
            label = _highlight(_student_label(student), difference["pending"]) if student else ""
            status = "待人工核对" if difference["pending"] else "差异已确认" if difference["last"] else _state(item)
            items.append(f'<li><a href="{href}">{number}</a> · {label} · {status}</li>')
        if batch_complete:
            body += '<p role="status">本批文件已处理。请在下方打开对应 RCTI 查看自动对比。</p>'
        return (body + f'<h2>第 1 步 · 上传单据</h2><p>PDF 和 CSV 每批各可提交最多 20 份。批量上传 CSV 时，用完整 RCTI 单号命名文件，或在模板的 rcti_number 栏填写单号。<a href="{root}?file=template">下载学生明细 CSV 模板</a></p>'
                + form("upload", '<label>教育局 RCTI PDF<input type="file" name="pdf" accept=".pdf,application/pdf" multiple></label>'
                       + csv_input.replace('accept=', 'multiple accept='), "上传并对比", True)
                + f'<h2>已有 RCTI</h2>{("<ul>"+"".join(items)+"</ul>") if items else "<p>暂无记录。</p>"}</section></main>')

    header = record["review"]["header"]
    model = service.compare(record["uid"], current=True)
    difference_review = service.difference(record, model)
    actual_total = header.get("gross","")
    detail_pending = not record["details_confirmed"] and (not record["confirmed"] or record["csv_id"])
    body += '<section class="revenue-panel" aria-labelledby="vic-rcti-summary"><h2 id="vic-rcti-summary">对账结果</h2>'
    number_student = model.get("number_student")
    if number_student:
        if number_student["status"] == "matched":
            student = _highlight(_student_label(number_student), difference_review["pending"])
        elif number_student["status"] == "ambiguous":
            student = "有多个学生使用此 Offer 学生号"
        else:
            student = "尚未匹配学生"
        body += (f'<p class="vic-rcti-meta"><strong>{student}</strong>'
                 f'<span>Offer 学生号 {_text(number_student["student_number"])}</span>'
                 f'<span>{_text(number_student["term"])}</span></p>')
    try:
        actual_cents = aud_cents(actual_total)
        actual_display = _money(actual_cents)
    except ValueError:
        actual_cents, actual_display = None, "待核对"
    expected = model["expected_total"]
    expected_display = _money(expected) if expected is not None else "待核对"
    received = record["received_cents"]
    received_display = _money(received) if record["bank"] else "尚未记录"
    amounts = (("CRM 预计佣金", expected_display), ("银行累计实收", received_display), ("RCTI 含税金额", actual_display))
    body += '<dl class="vic-rcti-amounts">' + "".join(
        f'<div><dt>{label}</dt><dd>{value}</dd></div>' for label, value in amounts
    ) + '</dl>'
    if expected is not None and record["bank"]:
        difference = received - expected
        status = "一致" if difference == 0 else "待人工核对"
        tone = "match" if difference == 0 else "check"
        body += (f'<p class="vic-rcti-result"><span>银行实收 − CRM 预计</span>'
                 f'<strong>{_money(difference)}</strong><span class="vic-rcti-badge {tone}">{status}</span></p>')
    elif expected is None:
        body += '<p class="vic-rcti-meta">暂时找不到可比对的 CRM 预计佣金，请核对学生或佣金资料。</p>'
    else:
        body += '<p class="vic-rcti-meta">登记到账后显示实收与预计的差额。</p>'
    if actual_cents is not None and record["bank"]:
        body += (f'<p class="vic-rcti-reference">银行实收 − RCTI 金额 '
                 f'<strong>{_money(received - actual_cents)}</strong></p>')
    body += '</section>'
    if difference_review["pending"]:
        message = "RCTI 与 CRM 金额有差异，请核对资料或联系教育局查明原因。" if difference_review["mismatch"] else "比对资料已更新，请核对后确认处理结果。"
        body += f'<section class="revenue-panel vic-rcti-review" aria-label="差异处理"><h2>差异待确认</h2><p role="status">{message}</p>'
        if actual_cents is not None and expected is not None:
            body += f'<p>RCTI 金额 − CRM 预计 <strong>{_money(actual_cents - expected)}</strong></p>'
        signature = _hidden("signature", difference_review["signature"])
        if model["expected_basis"]:
            body += form("resolve-difference", signature + _hidden("outcome", "crm_amount")
                         + '<label>更正后的 CRM 预计佣金（含 GST，AUD）'
                           '<input type="number" name="corrected_amount" min="0" max="999999999.99" step="0.01" required></label>'
                           '<label>更正原因<textarea name="note" maxlength="1000" required></textarea></label>',
                         "保存更正并确认")
            body += '<details><summary>其他处理方式</summary>'
        else:
            crm_url = "/revenue/institution-commission-drafts/" + quote(workflow.uid, safe="")
            body += f'<p><a href="{crm_url}">打开 CRM 佣金资料</a></p>'
        options = "".join(f'<option value="{key}">{value}</option>' for key, value in OUTCOMES.items()
                          if key != "crm_amount" and (key != "reissued" or record["review"].get("previous_versions")))
        body += form("resolve-difference", signature
                     + f'<label>处理方式<select name="outcome" required><option value="">请选择</option>{options}</select></label>'
                     + '<label>核对结果<textarea name="note" maxlength="1000" required></textarea></label>',
                     "确认处理结果")
        if model["expected_basis"]:
            body += '</details>'
        body += '</section>'
    body += '<section class="revenue-panel" aria-labelledby="vic-rcti-payments"><h2 id="vic-rcti-payments">到账记录</h2>'
    if record["bank"]:
        body += '<ul class="vic-rcti-payments">'
        for item in record["bank"]:
            body += f'<li><time>{_text(item["received_on"])}</time><strong>{_money(item["cents"])}</strong>'
            if item["receipt_id"]:
                payment = quote(item["uid"], safe="")
                body += f'<a href="{url}?file=receipt&amp;payment={payment}">查看收据</a>'
            body += '</li>'
        body += '</ul>'
    expanded = "" if record["bank"] or difference_review["pending"] else " open"
    body += f'<details{expanded}><summary>登记到账</summary>'
    body += form("bank", '<label>到账日期<input type="date" name="received_on" required></label>'
                 '<label>到账金额（AUD）<input name="amount" inputmode="decimal" required></label>'
                 '<label>上传收据（选填）<input type="file" name="receipt" '
                 'accept=".pdf,.png,.jpg,.jpeg,application/pdf,image/png,image/jpeg"></label>'
                 '<label><input type="checkbox" name="reviewed" value="1" required>已核对银行流水与维州付款方</label>',
                 "记录这笔到账", True)
    body += '</details></section>'

    body += ('<section class="revenue-panel" aria-labelledby="vic-rcti-documents">'
             '<h2 id="vic-rcti-documents">单据与核对记录</h2>'
             f'<p><a href="{url}?file=pdf">查看原版 RCTI PDF</a>'
             + (f' · <a href="{url}?file=csv">下载原学生明细</a>' if record["csv_id"] else "")
             + '</p>')
    last = difference_review["last"]
    if last:
        body += ('<details><summary>查看最近一次确认</summary>'
                 f'<p>{_text(OUTCOMES[last["outcome"]])} · {_text(last["at"][:10])}</p>'
                 f'<p>{_text(last["note"])}</p>')
        if last.get("correction"):
            change = last["correction"]
            body += f'<p>更正前 {_money(change["before_cents"])} · 更正后 {_money(change["after_cents"])}</p>'
        body += '</details>'
    if not record["csv_id"]:
        body += '<details><summary>补传学生明细</summary>'
        body += form("add-csv", csv_input.replace('accept=', 'required accept='), "补传学生明细", True)
        body += '</details>'
    if not record["confirmed"]:
        body += '<details><summary>上传教育局重发的 RCTI</summary>'
        body += form("replace-rcti", '<label>重发 RCTI PDF<input type="file" name="replacement" '
                     'accept=".pdf,application/pdf" required></label>', "保存重发版本", True)
        body += '</details>'
    versions = record["review"].get("previous_versions", ())
    if versions:
        links = []
        for version in reversed(versions):
            version_id = version["pdf_id"]
            number = _text(version["header"].get("number") or "原单据")
            label = f'{number} · {_text(version["replaced_at"][:10])}'
            links.append(f'<li><a href="{url}?file=pdf&amp;version={version_id}">{label} · 查看 PDF</a>'
                         + (f' · <a href="{url}?file=csv&amp;version={version_id}">原学生明细</a>'
                            if version["csv_id"] else "") + '</li>')
        body += '<details><summary>历史 RCTI 版本</summary><ul>' + "".join(links) + '</ul></details>'
    body += '<details><summary>查看 RCTI 原件资料</summary><dl>' + "".join(
        f'<dt>{LABELS[key]}</dt><dd>{_text(header.get(key) or "未识别")}</dd>' for key in HEADER
    ) + '</dl></details>'
    differences = []
    choice_inputs = []
    for i, line in enumerate(model["lines"]):
        actual, expected = line["actual"], line["expected"]
        label = {"match":"已匹配","different":"金额差异","unmatched":"RCTI 有、CRM 未匹配"}[line["status"]]
        student = _student_label(expected) if expected else actual["student_id"]
        highlighted = _highlight(student, difference_review["pending"] and line["status"] == "different")
        differences.append(f'<tr><td>{highlighted}</td><td>{label}</td>'
                           f'<td>{_money(expected["gross_cents"]) if expected else "—"}</td>'
                           f'<td>{_money(actual["gross_cents"])}</td>'
                           f'<td>{_money(line["difference"]) if expected else "—"}</td></tr>')
        if detail_pending and line["status"] == "unmatched" and not number_student:
            choice_inputs.append(f'<label>为 {_text(actual["student_id"])} 选入学项目'
                                 f'<input name="match_{i}" list="vic-rcti-candidates" value="{_text(record["review"]["choices"].get(str(i)))}"></label>')
    if differences:
        body += ('<details><summary>查看学生明细对比</summary><div class="vic-rcti-table"><table><thead><tr><th>学生标识</th><th>对比结果</th><th>预计含税</th><th>RCTI 含税</th>'
                 '<th>差额（RCTI − 预计）</th></tr></thead><tbody>'+"".join(differences)+"</tbody></table></div>")
    if model["lines"]:
        t = model["totals"]
        body += f'<p>明细合计 · 未税 {_money(t["net_cents"])} · GST {_money(t["gst_cents"])} · 含税 {_money(t["gross_cents"])}</p>'
    if choice_inputs:
        candidates = "".join(f'<option value="{_text(row["component_uid"])}">{_text(row["student_id"])} · '
                             f'{_text(row.get("student_name"))}</option>' for row in model["estimates"])
        body += form("match-students", "".join(choice_inputs)
                     + f'<datalist id="vic-rcti-candidates">{candidates}</datalist>', "保存学生匹配")
    if differences:
        body += '</details>'
    return body + "</section></main>"
