"""Presentation-only Revenue views and read-model loader."""

from __future__ import annotations

from html import escape
from urllib.parse import quote
import sqlite3

from revenue.read_models import RevenueReadModels, exception_code_label
from revenue.urls import cases_url, revenue_home_url


def _money(cents: int) -> str:
    value = int(cents)
    sign = "-" if value < 0 else ""
    value = abs(value)
    return f"{sign}${value // 100:,}.{value % 100:02d}"


def _currency_money(currency_code: str, cents: int) -> str:
    value = int(cents)
    sign = "-" if value < 0 else ""
    value = abs(value)
    code = str(currency_code).upper()
    symbol = {"AUD": "$", "CNY": "¥"}.get(code, "")
    return f"{sign}{code} {symbol}{value // 100:,}.{value % 100:02d}"


def _aud_money(cents: int) -> str:
    return f"AUD {_money(cents)}"


def render_student_revenue_context(data: dict) -> str:
    stream_labels = {
        "school_education": "中小学",
        "higher_education": "高等教育",
    }
    rows = data.get("rows") or []
    if not rows:
        return (
            '<section class="student-revenue-context">'
            '<div class="empty">该学生暂未建立收费与佣金档案。</div></section>'
        )
    items = "".join(
        '<li class="student-revenue-context__item">'
        f'<a href="{escape(str(row["href"]), quote=True)}">'
        f'<strong>申请周期 #{int(row["application_cycle_id"])}</strong>'
        f'<span>{escape(stream_labels.get(str(row["education_stream"]), str(row["education_stream"])))}</span>'
        f'<span>周期状态：{escape(str(row["cycle_status"]))}</span>'
        f'<span>档案状态：{escape(str(row["case_status"]))}</span>'
        f'<small>更新：{escape(str(row["updated_at_display"]))}</small>'
        '</a></li>'
        for row in rows
    )
    return (
        '<section class="student-revenue-context">'
        '<header><h3>收费与佣金档案</h3>'
        '<p>按权威申请周期查看客户收费、学校佣金、发票、收款和对账。</p></header>'
        f'<ul>{items}</ul></section>'
    )


def _workspace_tabs(tabs: list[dict]) -> str:
    return "".join(
        f'<a class="revenue-tab{" is-active" if tab.get("active") else ""}" '
        f'href="{escape(str(tab["href"]), quote=True)}"'
        f'{" aria-current=\"page\"" if tab.get("active") else ""}>'
        f'{escape(str(tab["label"]))}</a>'
        for tab in tabs
    )


def _hidden_security(csrf: str, token: str, suffix: str) -> str:
    key = f"revenue-ui:{token}:{suffix}"
    return (
        f'<input type="hidden" name="csrf" value="{escape(str(csrf), quote=True)}">'
        f'<input type="hidden" name="idempotency_key" value="{escape(key, quote=True)}">'
    )


def _render_client_payment_form(
    payment_options: list[dict], payment_methods: list[dict],
    csrf: str, token: str, operator_badge: str,
    action_url: str = "/revenue/billing/payment",
) -> str:
    fee_options = "".join(
        f'<option value="{escape(str(item["fee_item_uid"]), quote=True)}"'
        f' data-outstanding-base-cents="{int(item["outstanding_base_cents"])}"'
        f'{" selected" if item.get("selected") else ""}>'
        f'{escape(str(item["label"]))} · 未收 {_money(int(item["outstanding_base_cents"]))}</option>'
        for item in payment_options
    )
    method_options = "".join(
        f'<option value="{escape(str(item["code"]), quote=True)}" '
        f'data-currency="{escape(str(item["currency_code"]), quote=True)}" '
        f'data-gst-basis-points="{int(item["gst_rate_basis_points"])}">'
        f'{escape(str(item["label"]))}</option>'
        for item in payment_methods
    )
    if not fee_options or not method_options:
        return ""
    return f"""<section class="revenue-action-panel"><div><span class="revenue-eyebrow">{operator_badge}</span>
      <h2>添加客户收款</h2><p>请记录本次实际收款金额并上传付款凭证，最终金额由服务器校验。</p></div>
      <form method="post" action="{escape(action_url, quote=True)}" enctype="multipart/form-data"
            class="revenue-action-form revenue-payment-form" data-revenue-payment-form>
        {_hidden_security(csrf, token, 'client-payment')}
        <label>收费项目<select name="fee_item_uid" required data-fee-item><option value="">请选择</option>{fee_options}</select></label>
        <label>付款方式<select name="payment_method_code" required data-payment-method><option value="">请选择</option>{method_options}</select></label>
        <p class="revenue-payment-method-help revenue-action-form__wide">支付宝 / 微信：实际收到人民币；澳洲账户：实际收到澳元。澳洲公司账号 GST 10%。</p>
        <label>实际到账币种<input name="actual_currency_code" value="" readonly required data-actual-currency></label>
        <label><span data-actual-amount-label>实际到账金额</span>
          <span class="revenue-money-input"><span data-actual-prefix>—</span><input type="text" inputmode="decimal" autocomplete="off" data-actual-major required></span>
          <input type="hidden" name="actual_amount_cents" data-actual-cents>
        </label>
        <label>本次服务费
          <span class="revenue-money-input"><span>AUD $</span><input type="text" inputmode="decimal" autocomplete="off" data-base-major required></span>
          <input type="hidden" name="allocated_base_cents" data-base-cents>
          <small data-outstanding-base>当前未收服务费：AUD $0.00</small>
        </label>
        <label>收款日期<input type="date" name="received_date" required></label>
        <label>交易参考（选填）<input name="external_reference" maxlength="120"></label>
        <section class="revenue-payment-preview revenue-action-form__wide" aria-live="polite" data-payment-preview>
          <div><span>基础服务费</span><strong data-preview-base>AUD $0.00</strong></div>
          <div><span data-preview-gst-label>GST</span><strong data-preview-gst>AUD $0.00</strong></div>
          <div><span>应收总额</span><strong data-preview-gross>AUD $0.00</strong></div>
        </section>
        <label class="revenue-action-form__wide">付款凭证 *（PNG、JPG/JPEG 或 PDF）
          <input type="file" name="payment_evidence" accept=".png,.jpg,.jpeg,.pdf,image/png,image/jpeg,application/pdf" required>
        </label>
        <p class="notice error revenue-action-form__wide" role="alert" aria-live="assertive" data-payment-error hidden></p>
        <button type="submit" class="primary">保存收款</button>
      </form></section>"""


def _render_workspace_action(data: dict, csrf: str, token: str) -> str:
    action = data.get("action")
    can_administer = bool(data.get("can_administer", True))
    operator_badge = "管理员操作" if can_administer else "授权操作"
    confirm_badge = "管理员确认" if can_administer else "授权确认"
    unavailable = escape(str(data.get("action_unavailable_message") or ""))
    if not action:
        return f'<aside id="revenue-action" class="revenue-action-note"><strong>当前无可执行操作</strong><p>{unavailable}</p></aside>'
    kind = action.get("kind")
    if kind == "billing_operations":
        payment_html = _render_client_payment_form(
            action.get("payment_options") or [],
            action.get("payment_methods") or [],
            csrf, token, operator_badge,
            str(action.get("payment_action") or "/revenue/billing/payment"),
        )
        receipt_forms = "".join(
            f"""<form method="post" action="{escape(str(action.get('receipt_action') or '/revenue/billing/receipt'), quote=True)}" class="revenue-action-form">
              {_hidden_security(csrf, token, 'client-receipt-' + str(item['payment_uid']))}
              <input type="hidden" name="payment_uid" value="{escape(str(item['payment_uid']), quote=True)}">
              <div class="revenue-action-form__wide"><strong>{escape(str(item['student_id']))} · {escape(str(item['description']))}</strong>
              <p>{escape(str(item['received_date']))} · {_currency_money(str(item['currency_code']), int(item['amount_cents']))}</p></div>
              <button type="submit" class="primary">生成收据</button>
            </form>"""
            for item in action.get("receipt_options") or []
        )
        receipt_html = (
            f'<section class="revenue-action-panel"><div><span class="revenue-eyebrow">{operator_badge}</span><h2>生成客户收据</h2><p>一笔收款只签发一份不可静默覆盖的收据。</p></div>{receipt_forms}</section>'
            if receipt_forms else ""
        )
        method_labels = {
            "alipay": "支付宝", "wechat": "微信",
            "au_personal_account": "澳洲私人账号", "au_company_account": "澳洲公司账号",
        }
        history_rows = "".join(
            "<tr>"
            f'<td>{escape(str(item["student_id"]))}</td>'
            f'<td>{escape(str(item["description"]))}</td>'
            f'<td>{escape(method_labels.get(str(item["method_code_snapshot"]), str(item["method_code_snapshot"])))}</td>'
            f'<td>{_currency_money(str(item["currency_code"]), int(item["amount_cents"]))}</td>'
            f'<td>{_money(int(item["allocated_base_cents"]))}</td>'
            f'<td>{_money(int(item["gst_cents"]))}</td>'
            f'<td>{escape(str(item["received_date"]))}</td>'
            f'<td>{("<a href=\"/revenue/documents/" + escape(str(item["evidence_document_uid"]), quote=True) + "/view\">查看凭证</a>") if item.get("evidence_document_uid") else "—"}</td>'
            f'<td>{("<a href=\"/revenue/documents/" + escape(str(item["receipt_document_uid"]), quote=True) + "/view\">" + escape(str(item["receipt_number"])) + "</a>") if item.get("receipt_document_uid") else "待生成"}</td>'
            "</tr>"
            for item in action.get("payment_history") or []
        )
        history_html = (
            '<section class="revenue-panel"><div class="revenue-panel-heading"><div><span class="revenue-eyebrow">不可变历史</span><h2>客户收款与收据</h2></div></div>'
            '<div class="revenue-table-wrap"><table class="revenue-table"><thead><tr><th>学生</th><th>收费项目</th><th>付款方式</th><th>实际到账</th><th>AUD 基础费</th><th>GST</th><th>收款日期</th><th>付款凭证</th><th>收据</th></tr></thead>'
            f'<tbody>{history_rows}</tbody></table></div></section>'
            if history_rows else ""
        )
        return f'<div id="revenue-action">{payment_html}{receipt_html}{history_html}</div>'
    if kind == "direct_operations":
        claim_html = ""
        if action.get("claims"):
            claim_html = _render_workspace_action(
                {**data, "action": {"kind": "direct_claim", "options": action["claims"]}},
                csrf, token,
            )
        invoice_options = "".join(
            f'<option value="{escape(str(item["uid"]), quote=True)}">{escape(str(item["claim_number"]))} · {_money(int(item["gross_cents"]))}</option>'
            for item in action.get("unbilled_claims") or []
        )
        invoice_html = f"""<section class="revenue-action-panel"><div><span class="revenue-eyebrow">{operator_badge}</span><h2>签发直接佣金发票</h2><p>仅显示已签发且尚未建立发票的 Claim。</p></div>
        <form method="post" action="/revenue/direct-commission/invoice" class="revenue-action-form">
          {_hidden_security(csrf, token, 'direct-invoice')}
          <label>佣金申领<select name="claim_uid" required>{invoice_options}</select></label>
          <label>签发日期<input type="date" name="issue_date" required></label>
          <label>到期日期<input type="date" name="due_date"></label>
          <button type="submit" class="primary">签发佣金发票</button>
        </form></section>""" if invoice_options else ""
        payment_options = "".join(
            f'<option value="{escape(str(item["commission_line_uid"]), quote=True)}|{escape(str(item["institution_entity_uid"]), quote=True)}">{escape(str(item["label"]))} · 待收 {_money(int(item["outstanding_cents"]))}</option>'
            for item in action.get("payments") or []
        )
        payment_html = f"""<section class="revenue-action-panel"><div><span class="revenue-eyebrow">{confirm_badge}</span><h2>记录机构佣金收款</h2><p>收款不得超过已签发 Claim 的未收余额。</p></div>
        <form method="post" action="/revenue/direct-commission/payment" class="revenue-action-form">
          {_hidden_security(csrf, token, 'direct-payment')}
          <label>佣金项目<select name="payment_target" required>{payment_options}</select></label>
          <label>收款金额（整数分）<input type="number" name="amount_cents" min="1" step="1" required></label>
          <label>收款日期<input type="date" name="received_date" required></label>
          <button type="submit" class="primary">确认收款与对账</button>
        </form></section>""" if payment_options else ""
        return f'<div id="revenue-action">{claim_html}{invoice_html}{payment_html}</div>'
    if kind == "master_agent_operations":
        import_html = ""
        if action.get("pairs"):
            import_html = _render_workspace_action(
                {**data, "action": {"kind": "statement_import", "pairs": action["pairs"]}},
                csrf, token,
            )
        confirm_forms = "".join(
            f"""<form method="post" action="/revenue/master-agent/match/confirm" class="revenue-action-form">
              {_hidden_security(csrf, token, 'ma-confirm-' + str(item['uid']))}
              <input type="hidden" name="suggestion_uid" value="{escape(str(item['uid']), quote=True)}">
              <div><strong>建议匹配 {_money(int(item['suggested_amount_cents']))}</strong></div>
              <button type="submit" class="primary">确认匹配</button>
            </form>"""
            for item in action.get("suggestions") or []
        )
        confirm_html = f'<section class="revenue-action-panel"><div><span class="revenue-eyebrow">{confirm_badge}</span><h2>确认匹配建议</h2><p>建议本身不产生财务权威，必须逐笔确认。</p></div><div>{confirm_forms}</div></section>' if confirm_forms else ""
        invoice_options = "".join(
            f'<option value="{escape(str(item["uid"]), quote=True)}">{escape(str(item.get("student_reference") or "未指定学生"))} · {_money(int(item["payable_cents"]))}</option>'
            for item in action.get("invoice_items") or []
        )
        invoice_html = f"""<section class="revenue-action-panel"><div><span class="revenue-eyebrow">{operator_badge}</span><h2>签发一级代理发票</h2><p>仅显示已确认匹配的对账单项目。</p></div>
        <form method="post" action="/revenue/master-agent/invoice" class="revenue-action-form">
          {_hidden_security(csrf, token, 'ma-invoice')}
          <label>对账项目<select name="statement_item_uid" required>{invoice_options}</select></label>
          <label>签发日期<input type="date" name="issue_date" required></label>
          <label>到期日期<input type="date" name="due_date"></label>
          <button type="submit" class="primary">签发一级代理发票</button>
        </form></section>""" if invoice_options else ""
        payment_options = "".join(
            f'<option value="{escape(str(item["item_uid"]), quote=True)}|{escape(str(item["master_uid"]), quote=True)}">{escape(str(item.get("student_reference") or "未指定学生"))} · 待收 {_money(int(item["outstanding_cents"]))}</option>'
            for item in action.get("payments") or []
        )
        payment_html = f"""<section class="revenue-action-panel"><div><span class="revenue-eyebrow">{confirm_badge}</span><h2>记录一级代理收款</h2><p>收款分配至已确认的 Statement Item。</p></div>
        <form method="post" action="/revenue/master-agent/payment" class="revenue-action-form">
          {_hidden_security(csrf, token, 'ma-payment')}
          <label>对账项目<select name="payment_target" required>{payment_options}</select></label>
          <label>收款金额（整数分）<input type="number" name="amount_cents" min="1" step="1" required></label>
          <label>收款日期<input type="date" name="received_date" required></label>
          <button type="submit" class="primary">确认收款与对账</button>
        </form></section>""" if payment_options else ""
        return f'<div id="revenue-action">{import_html}{confirm_html}{invoice_html}{payment_html}</div>'
    if kind in {"contract_upload", "contract_management"}:
        entities = "".join(
            f'<option value="{escape(str(item["uid"]), quote=True)}">{escape(str(item["label"]))}</option>'
            for item in action.get("entities") or []
        )
        upload_form = f"""<section class="revenue-action-panel"><div><span class="revenue-eyebrow">{operator_badge}</span><h2>登记佣金合同</h2><p>上传后建立不可原位覆盖的草稿版本；智能提取仅产生建议，失败时安全转人工审核。</p></div>
        <form method="post" action="/revenue/contracts/upload" enctype="multipart/form-data" class="revenue-action-form">
          {_hidden_security(csrf, token, 'contract-upload')}
          <label>合作方<select name="entity_uid" required>{entities}</select></label>
          <label>合同类型<select name="contract_kind" required><option value="direct_commission">直接佣金</option><option value="master_agent">一级代理</option><option value="other">其他</option></select></label>
          <label>生效日期<input type="date" name="effective_from" required></label>
          <label>参考编号<input name="reference_code" maxlength="100"></label>
          <label class="revenue-action-form__wide">合同文件<input type="file" name="contract_file" accept=".pdf,.docx" required></label>
          <button type="submit" class="primary">登记合同版本</button>
        </form></section>""" if entities else ""
        contract_options = "".join(
            f'<option value="{escape(str(item["uid"]), quote=True)}">{escape(str(item["label"]))}</option>'
            for item in action.get("manual_contracts") or []
        )
        manual_form = f"""<section class="revenue-action-panel"><div><span class="revenue-eyebrow">人工后备</span><h2>登记人工提取建议</h2><p>用于智能提取不可用的情况；登记后仍须由管理员单独审核确认。</p></div>
        <form method="post" action="/revenue/contracts/manual-extraction" class="revenue-action-form">
          {_hidden_security(csrf, token, 'contract-manual-extraction')}
          <label>合同版本<select name="contract_version_uid" required>{contract_options}</select></label>
          <label>字段<select name="field_name" required><option value="commission_rate_basis_points">佣金比例（基点）</option><option value="gst_basis_points">消费税比例（基点）</option><option value="fixed_commission_cents">固定佣金（分）</option><option value="minimum_commission_cents">最低佣金（分）</option><option value="maximum_commission_cents">最高佣金（分）</option><option value="tuition_cents">学费（分）</option></select></label>
          <label>建议值<input name="suggested_value" inputmode="numeric" required></label>
          <label>条款参考<input name="clause_reference" maxlength="100"></label>
          <label class="revenue-action-form__wide">证据说明<textarea name="evidence_text" required></textarea></label>
          <button type="submit" class="primary">登记待审核建议</button>
        </form></section>""" if contract_options else ""
        field_labels = {
            "commission_rate_basis_points": "佣金比例（基点）",
            "gst_basis_points": "消费税比例（基点）",
            "fixed_commission_cents": "固定佣金（分）",
            "minimum_commission_cents": "最低佣金（分）",
            "maximum_commission_cents": "最高佣金（分）",
            "tuition_cents": "学费（分）",
        }
        review_forms = "".join(
            f"""<form method="post" action="/revenue/contracts/extraction/review" class="revenue-action-form revenue-review-form">
              {_hidden_security(csrf, token, 'contract-review-' + str(item['uid']))}
              <input type="hidden" name="extraction_uid" value="{escape(str(item['uid']), quote=True)}">
              <div class="revenue-action-form__wide"><strong>{escape(str(item['entity_name']))} · {escape(field_labels.get(str(item['field_name']), '合同字段'))}</strong><p>{escape(str(item.get('evidence_text') or '未提供证据说明'))}</p></div>
              <label>确认值<input name="confirmed_value" value="{escape(str(item.get('suggested_value') or ''), quote=True)}" required></label>
              <label>审核决定<select name="decision" required><option value="accept">确认</option><option value="reject">拒绝</option></select></label>
              <button type="submit" class="primary">提交审核</button>
            </form>"""
            for item in (action.get("pending_extractions") or []) if can_administer
        )
        review_section = (
            f'<section class="revenue-action-panel"><div><span class="revenue-eyebrow">管理员确认</span><h2>审核提取建议</h2><p>建议本身不会激活合同或产生财务影响。</p></div>{review_forms}</section>'
            if can_administer and review_forms else ""
        )
        activatable_options = "".join(
            f'<option value="{escape(str(item["uid"]), quote=True)}">{escape(str(item["label"]))}</option>'
            for item in action.get("activatable_contracts") or []
        )
        activation_form = f"""<section class="revenue-action-panel"><div><span class="revenue-eyebrow">最终确认</span><h2>激活合同版本</h2><p>仅显示所有金额与比例字段均已由管理员确认的合同。</p></div>
        <form method="post" action="/revenue/contracts/activate" class="revenue-action-form">
          {_hidden_security(csrf, token, 'contract-activate')}
          <label>合同版本<select name="contract_version_uid" required>{activatable_options}</select></label>
          <button type="submit" class="primary">确认激活</button>
        </form></section>""" if can_administer and activatable_options else ""
        return f'<div id="revenue-action">{upload_form}{manual_form}{review_section}{activation_form}</div>'
    if kind == "statement_import":
        pairs = "".join(
            f'<option value="{escape(str(item["master_agent_entity_uid"]), quote=True)}|{escape(str(item["provider_entity_uid"]), quote=True)}">{escape(str(item["label"]))}</option>'
            for item in action.get("pairs") or []
        )
        return f"""<section id="revenue-action" class="revenue-action-panel"><div><span class="revenue-eyebrow">{operator_badge}</span><h2>导入对账单</h2><p>CSV 金额必须使用整数分；分成由已激活规则统一计算，匹配建议仍需确认。</p></div>
        <form method="post" action="/revenue/master-agent/import" enctype="multipart/form-data" class="revenue-action-form">
          {_hidden_security(csrf, token, 'statement-import')}
          <label>一级代理与院校<select name="entity_pair" required>{pairs}</select></label>
          <label>对账单编号<input name="statement_reference" required maxlength="100"></label>
          <label>开始日期<input type="date" name="period_start"></label>
          <label>结束日期<input type="date" name="period_end"></label>
          <label class="revenue-action-form__wide">CSV 文件<input type="file" name="statement_file" accept=".csv,text/csv" required></label>
          <button type="submit" class="primary">导入对账单</button>
        </form></section>"""
    if kind == "direct_claim":
        options = "".join(
            f'<option value="{escape(str(item["commission_line_uid"]), quote=True)}|{int(item["instalment_no"])}">{escape(str(item["label"]))} · {_money(int(item["claimable_cents"]))}</option>'
            for item in action.get("options") or []
        )
        return f"""<section id="revenue-action" class="revenue-action-panel"><div><span class="revenue-eyebrow">{operator_badge}</span><h2>签发直接佣金申领</h2><p>仅显示已确认可申领的期次；签发后使用调整或追回进行更正。</p></div>
        <form method="post" action="/revenue/direct-commission/claim" class="revenue-action-form">
          {_hidden_security(csrf, token, 'direct-claim')}
          <label>可申领项目<select name="claim_target" required>{options}</select></label>
          <label>申领金额（整数分）<input type="number" name="claimed_cents" min="1" step="1" required></label>
          <label>申领日期<input type="date" name="claim_date" required></label>
          <button type="submit" class="primary">签发申领</button>
        </form></section>"""
    if kind == "client_payment":
        return (
            '<aside id="revenue-action" class="revenue-action-note">'
            '<strong>请刷新客户收费工作区</strong>'
            '<p>旧版收款表单已停用，系统不会显示或提交缺少付款方式权威的收款操作。</p></aside>'
        )
    return f'<aside id="revenue-action" class="revenue-action-note"><strong>当前无可执行操作</strong><p>{unavailable}</p></aside>'


def _toorak_contract_configuration(data, csrf, token):
    schools = data.get("toorak_schools") or []
    if not schools:
        return ""
    options = "".join(f'<option value="{int(s["id"])}">{escape(s["school_name"])}</option>'
                      for s in schools)
    links = "".join(
        f'<li><a href="/revenue/contracts?contract={quote(d["uid"], safe="")}">'
        f'{escape(d["school_name"])} · V1 待登记 · {escape(d["uid"][-8:])}</a></li>'
        for d in data["toorak_drafts"]
    )
    detail = ""
    draft = data["toorak_draft"]
    if draft:
        categories = (("subclass_188", "Subclass 188"), ("other_resident_confirmed", "已确认的其他居民签证"),
                      ("full_student", "完整学生签证"), ("unknown", "无法确认"))
        def select_visa(name, selected):
            items = "".join(f'<option value="{key}"{" selected" if key == selected else ""}>{label}</option>'
                            for key, label in categories)
            return f'<select name="{name}">{items}</select>'

        detail = f"""<p><a href="/revenue/documents/{quote(draft["document_uid"], safe="")}/download">下载 PDF 原件</a></p>
        <p>本地首年学费 5% · 第 7 页 §7(a)(i)｜国际首年学费 17% · 第 7 页 §7(a)(ii)</p>
        <p>申领须有代理招募及署名申请、注册、向学校支付课程费、开学和有效发票。直接申请无佣金。学校收到有效发票后 30 天内付款。</p>
        <form method="post" action="/revenue/contracts/school/toorak-preview" class="revenue-action-form">
          {_hidden_security(csrf, token, "toorak-preview")}
          <input type="hidden" name="draft_uid" value="{escape(draft["uid"], quote=True)}">
          <label>本地档签证 {select_visa("local_visa", "subclass_188")}</label>
          <label>首年本地学费样本（AUD）<input name="local_tuition" inputmode="decimal" required></label>
          <label>国际档签证 {select_visa("international_visa", "full_student")}</label>
          <label>首年国际学费样本（AUD）<input name="international_tuition" inputmode="decimal" required></label>
          <label>GST<select name="gst_mode"><option value="">待确认</option><option value="none">不适用</option><option value="exclusive">另加 10%</option><option value="inclusive">已含 10%</option></select></label>
          <label>协议生效日<input type="date" name="effective_from"></label>
          <label>确认到期日（合同期限两年）<input type="date" name="effective_to"></label>
          <button class="primary" type="submit">分别预览两档</button>
        </form>"""
    signed = ('<label><input type="checkbox" name="signed_confirmed" value="1" required>'
              '已核对这是双方签署的合同原件</label>') if data.get("formal") else ""
    return f"""<section class="revenue-panel"><h3>Toorak College · 双档规则</h3>
      <form method="post" action="/revenue/contracts/school/toorak-draft" enctype="multipart/form-data" class="revenue-action-form">
        {_hidden_security(csrf, token, "toorak-draft")}
        <label>学校<select name="school_id">{options}</select></label>
        <label>合同原件<input type="file" name="contract_file" accept=".pdf,application/pdf" required></label>
        {signed}<button class="primary" type="submit">登记待确认草稿</button>
      </form><ul>{links}</ul>{detail}</section>"""


def render_toorak_commission_preview(preview, csrf, token):
    def amount(cents):
        return "待确认" if cents is None else _money(cents)

    labels = {"local": "首年本地学费", "international": "首年国际学费"}
    rows = []
    for row in preview["rows"]:
        outcome = "Needs Attention：签证与学费档不匹配或尚未确认。"
        if row["commission_cents"] is not None:
            outcome = (f'样本 {_money(row["tuition_cents"])} · 佣金 {_money(row["commission_cents"])}'
                       f' · GST {amount(row["gst_cents"])} · 合计 {amount(row["total_cents"])}')
        rows.append(f'<li>{labels[row["tuition_kind"]]} · {row["rate_basis_points"] // 100}%'
                    f' · {escape(row["source"])}<br>{outcome}</li>')
    fields = {**preview["confirmation_fields"], "preview_digest": preview["digest"]}
    gst_labels = {None: "待确认", "none": "不适用", "exclusive": "另加 10%", "inclusive": "已含 10%"}
    gst = gst_labels[preview["gst_mode"]]
    start = escape(preview["effective_from"] or "待确认")
    end = escape(preview["effective_to"] or "待确认")
    action = '<p>两档匹配结果、GST 或协议有效期待确认，暂不能激活。</p>'
    if preview["can_activate"]:
        hidden = "".join(f'<input type="hidden" name="{key}" value="{escape(value, quote=True)}">'
                         for key, value in fields.items())
        action_label = "确认并激活两档规则" if preview.get("formal") else "确认并在测试库整套激活"
        action = f"""<form method="post" action="/revenue/contracts/school/toorak-activate">
          {_hidden_security(csrf, token, "toorak-activate")}{hidden}
          <button class="primary" type="submit">{action_label}</button>
        </form>"""
    return f"""<section class="revenue-panel"><h2>{escape(preview["school_name"])} · 双档佣金预览</h2>
      <a href="/revenue/documents/{quote(preview["document_uid"], safe="")}/download">下载 PDF 原件</a>
      <p>GST {gst} · 生效日 {start} · 到期日 {end}</p>
      <ul>{''.join(rows)}</ul>{action}
      <a href="/revenue/contracts?contract={quote(fields["draft_uid"], safe="")}">返回合同草稿</a>
    </section>"""


def _school_contract_configuration(data: dict, csrf: str, token: str) -> str:
    schools = "".join(
        f'<option value="{int(school["id"])}">{escape(school["school_name"])}</option>'
        for school in data["schools"]
    )
    bindings = data["bindings"]
    upload_options = "".join(
        f'<option value="{int(binding["id"])}">{escape(binding["school_name"])} · '
        f'{escape(binding["legal_name"])}</option>'
        for binding in bindings if not binding["contract_uid"]
    )
    registered = "".join(
        f'<li><a href="/revenue/contracts?contract={escape(quote(binding["contract_uid"], safe=""), quote=True)}">'
        f'{escape(binding["school_name"])} · {escape(binding["legal_name"])}</a></li>'
        for binding in bindings if binding["contract_uid"]
    )
    bind_form = f"""
      <form method="post" action="/revenue/contracts/school/bind" class="revenue-action-form">
        {_hidden_security(csrf, token, "school-bind")}
        <label>学校<select name="school_id" required>{schools}</select></label>
        <label>签约及付款主体<input name="legal_name" maxlength="200" required></label>
        <label>绑定生效日<input type="date" name="effective_from" required></label>
        <button class="primary" type="submit">绑定学校佣金主体</button>
      </form>""" if schools else ""
    upload_form = f"""
      <form method="post" action="/revenue/contracts/school/upload" enctype="multipart/form-data" class="revenue-action-form">
        {_hidden_security(csrf, token, "school-upload")}
        <label>已绑定学校<select name="school_id" required>{upload_options}</select></label>
        <label>合同参考编号<input name="reference_code" maxlength="100"></label>
        <label>合同生效日<input type="date" name="effective_from" required></label>
        <label>合同到期日<input type="date" name="effective_to" required></label>
        <label>已签 PDF<input name="contract_file" type="file" accept=".pdf,application/pdf" required></label>
        <button class="primary" type="submit">登记学校合同 V1</button>
      </form>""" if upload_options else ""
    detail = ""
    contract = data.get("contract")
    if contract:
        school = contract["school"]
        uid = escape(contract["uid"], quote=True)
        document_uid = escape(quote(contract["document_uid"], safe=""), quote=True)
        detail = f"""<section class="revenue-panel">
          <h3>{escape(school["school_name"])} · 合同 V{int(contract["version_no"])}</h3>
          <p>付款主体 {escape(school["legal_name"])} · 参考编号 {escape(contract["reference_code"] or "—")}</p>
          <p>有效期 {escape(contract["effective_from"])} 至 {escape(contract["effective_to"] or "—")}</p>
          <a href="/revenue/documents/{document_uid}/download">下载 PDF 原件</a>"""
        if contract.get("toorak_package"):
            detail += '<p>双档已在测试库激活 · 首年本地学费 5% / 首年国际学费 17%</p>'
        elif contract["rule_uid"]:
            rate = int(contract["rate_basis_points"])
            required = "需要 COE 和对应文件" if contract["required_document"] else "无附加材料要求"
            detail += (
                f'<p>规则已在测试库激活 · 佣金比例 {rate // 100}.{rate % 100:02d}% '
                f'· 开学后 {int(contract["commencement_days"])} 天 · {required}</p>'
                f'<p>来源 {escape(contract["clause_reference"] or "—")} · '
                f'{escape(contract["evidence_text"] or "—")}</p>'
            )
        if contract["status"] == "draft":
            detail += f"""
          <form method="post" action="/revenue/contracts/school/preview" class="revenue-action-form">
            {_hidden_security(csrf, token, "school-preview")}
            <input type="hidden" name="school_id" value="{int(school["id"])}">
            <input type="hidden" name="contract_version_uid" value="{uid}">
            <label>计佣学费样本（AUD）<input name="sample_tuition" inputmode="decimal" required></label>
            <label>单期佣金比例（%）<input name="rate_percent" inputmode="decimal" required></label>
            <label>GST<select name="gst_mode"><option value="exclusive">佣金另加 10% GST</option></select></label>
            <label>开学后可申领天数<input name="commencement_days" type="number" min="0" max="365" required></label>
            <label><input name="required_coe" type="checkbox" value="1"> 需要 COE 及对应文件</label>
            <label>来源页码或条款<input name="clause_reference" maxlength="100" required></label>
            <label class="revenue-action-form__wide">条款证据<textarea name="evidence_text" maxlength="2000" required></textarea></label>
            <button class="primary" type="submit">预览佣金规则</button>
          </form>"""
        detail += "</section>"
    links = ""
    if data.get("allow_invoicing",True):
        links = ('<p><a href="/revenue/school-commission-invoices">按学校开具佣金发票</a>'
                 ' · <a href="/revenue/commission-invoice-templates">编辑佣金发票模板</a></p>')
    elif data.get("allow_invoice_preview"):
        links = '<p><a href="/revenue/school-commission-invoices">预览学校佣金发票</a></p>'
    if data.get("formal"):
        bind_form = upload_form = ""
    mode = "隔离测试环境 · " if data.get("simulation",True) else ""
    return f"""<section class="revenue-panel" aria-label="学校佣金合同配置">
      <h2>学校佣金合同配置</h2><p>{mode}人工核对已签合同</p>
      {links}
      {_toorak_contract_configuration(data, csrf, token)}
      {bind_form}{upload_form}<ul>{registered}</ul>{detail}
    </section>"""


def render_school_commission_preview(preview: dict, csrf: str, token: str) -> str:
    fields = {**preview["confirmation_fields"], "preview_digest": preview["digest"]}
    hidden_fields = "".join(
        f'<input type="hidden" name="{escape(key, quote=True)}" value="{escape(str(value), quote=True)}">'
        for key, value in fields.items()
    )
    contract_uid = quote(str(fields["contract_version_uid"]), safe="")
    contract = preview["contract"]
    document_uid = quote(contract["document_uid"], safe="")
    clause = escape(str(fields["clause_reference"]))
    evidence = escape(str(fields["evidence_text"]))
    required = "需要 COE 和对应文件" if fields["required_coe"] == "1" else "无附加材料要求"
    rate = escape(str(fields["rate_percent"]))
    days = escape(str(fields["commencement_days"]))
    return f"""<section class="revenue-panel">
      <h2>学校佣金规则预览</h2>
      <p>{escape(contract["school_name"])} · {escape(contract["legal_name"])} · 合同 V1</p>
      <p>参考编号 {escape(contract["reference_code"] or "—")} · 有效期
        {escape(contract["effective_from"])} 至 {escape(contract["effective_to"])}</p>
      <a href="/revenue/documents/{escape(document_uid, quote=True)}/download">下载 PDF 原件</a>
      <p>样本学费 {_money(preview["tuition_cents"])} · 单期比例 {rate}%</p>
      <p>净佣金 {_money(preview["net_cents"])} · GST {_money(preview["gst_cents"])} · 合计 {_money(preview["gross_cents"])}</p>
      <p>开学后 {days} 天 · {required}</p><p>来源 {clause} · {evidence}</p>
      <form method="post" action="/revenue/contracts/school/activate">
        {_hidden_security(csrf, token, "school-activate")}{hidden_fields}
        <button class="primary" type="submit">确认条款并在测试库激活</button>
      </form>
      <a href="/revenue/contracts?contract={escape(contract_uid, quote=True)}">返回合同</a>
    </section>"""


def render_revenue_workspace_page(
    data: dict, csrf: str, action_token: str = "", mobile: bool = False
) -> str:
    """Render one of the six read-only Revenue workspaces."""

    columns = data.get("columns") or []
    header = "".join(f'<th scope="col">{escape(str(column["label"]))}</th>' for column in columns)
    rows = []
    for row in data.get("rows") or []:
        cells = []
        for index, column in enumerate(columns):
            key = str(column["key"])
            raw = row.get(key)
            if column.get("kind") == "money":
                value = "—" if raw is None else _money(int(raw))
            else:
                value = "—" if raw in (None, "") else str(raw)
            content = escape(value)
            if index == 0 and row.get("href"):
                content = (
                    f'<a class="revenue-row-link" href="{escape(str(row["href"]), quote=True)}">'
                    f'{content}</a>'
                )
            cells.append(f"<td>{content}</td>")
        selected = " is-selected" if row.get("selected") else ""
        rows.append(f'<tr class="revenue-data-row{selected}">{"".join(cells)}</tr>')
    if not rows:
        rows.append(
            f'<tr><td colspan="{max(1, len(columns))}" class="revenue-empty">'
            f'{escape(str(data.get("empty_message") or "暂无记录。"))}</td></tr>'
        )
    filters = "".join(
        f'<a class="revenue-filter{" is-active" if item.get("active") else ""}" '
        f'href="{escape(str(item["href"]), quote=True)}"'
        f'{" aria-current=\"page\"" if item.get("active") else ""}>'
        f'{escape(str(item["label"]))}</a>'
        for item in data.get("filters") or []
    )
    coverage_items = []
    for item in data.get("coverage_cards") or []:
        content = (
            f'<span>{escape(str(item["label"]))}</span>'
            f'<strong>{int(item["count"])}</strong>'
        )
        href = item.get("href")
        if href:
            coverage_items.append(
                f'<a class="revenue-coverage-card" href="{escape(str(href), quote=True)}">{content}</a>'
            )
        else:
            coverage_items.append(f'<div class="revenue-coverage-card">{content}</div>')
    coverage = (
        '<section class="revenue-panel revenue-coverage-panel">'
        '<div class="revenue-panel-heading"><div><span class="revenue-eyebrow">权威数据</span>'
        '<h2>业务覆盖</h2></div></div>'
        f'<div class="revenue-coverage-grid">{"".join(coverage_items)}</div></section>'
        if coverage_items else ""
    )
    controls = data.get("search_controls") or {}
    contract_search = ""
    if controls and data.get("name") == "contracts":
        type_options = (
            ("", "全部机构类型"), ("school", "学校"),
            ("higher_education_provider", "高等教育机构"),
            ("master_agent", "一级代理"), ("other", "其他"),
        )
        options = "".join(
            f'<option value="{escape(value, quote=True)}"'
            f'{" selected" if controls.get("institution_type") == value else ""}>'
            f'{escape(label)}</option>'
            for value, label in type_options
        )
        contract_search = f"""<form method="get" action="/revenue/contracts" class="revenue-search-form" aria-label="合同筛选">
          <input type="hidden" name="filter" value="{escape(str(data.get('active_filter') or 'all'), quote=True)}">
          <label>搜索<input name="q" value="{escape(str(controls.get('q') or ''), quote=True)}" placeholder="合作方或合同类型"></label>
          <label>机构类型<select name="institution_type">{options}</select></label>
          <label>州<input name="state" value="{escape(str(controls.get('state') or ''), quote=True)}" placeholder="例如 VIC"></label>
          <label>城市<input name="city" value="{escape(str(controls.get('city') or ''), quote=True)}" placeholder="例如 Melbourne"></label>
          <button type="submit" class="secondary">筛选</button>
        </form>"""
    if data.get("name") == "cases":
        contract_search = f"""<form method="get" action="{escape(str(data.get('cases_href') or '/revenue/cases'), quote=True)}" class="revenue-search-form" aria-label="学生搜索">
          <input type="hidden" name="filter" value="{escape(str(data.get('active_filter') or 'all'), quote=True)}">
          <label>学生编号或姓名<input name="q" value="{escape(str(controls.get('q') or ''), quote=True)}" placeholder="Student Number / Student Name"></label>
          <button type="submit" class="secondary">搜索</button></form>"""
    pagination = data.get("pagination") or {}
    paging = ""
    if pagination:
        links = " ".join(f'<a href="{escape(str(pagination[key]), quote=True)}">{label}</a>'
                         for key, label in (("previous", "上一页"), ("next", "下一页")) if pagination.get(key))
        paging = f'<nav aria-label="学生分页">共 {int(pagination["total"])} 位学生 · 第 {int(pagination["page"])} / {int(pagination["pages"])} 页 {links}</nav>'
    student_detail = ""
    detail = data.get("student_detail")
    if detail:
        if detail["state"] == "not_found":
            content = "未找到符合条件的学生。"
        else:
            content = f'<h2>{escape(str(detail["student_name"]))} · {escape(str(detail["student_id"]))}</h2>'
            cases = detail.get("cases") or []
            content += "<ul>" + "".join(
                f'<li><a href="{escape(str(case["href"]), quote=True)}">{escape(str(case["business_type"]))} · {escape(str(case["intake"]))} · {escape(str(case["status"]))}</a>'
                f' · 收费：{escape(str(case["billing_status"]))} · 佣金：{escape(str(case["commission_status"]))}</li>' for case in cases) + "</ul>" if cases else "<p>未建立 / Not Yet Created</p>"
        student_detail = f'<section class="revenue-panel" aria-label="学生收入与佣金详情">{content}</section>'
    action_content = (
        '<aside id="revenue-action" class="revenue-action-note">'
        '<strong>移动端仅提供查看与筛选</strong>'
        '<p>合同上传、对账单导入、正式申领与收款确认请在桌面端完成。</p></aside>'
        if mobile else _render_workspace_action(data, csrf, action_token)
    )
    if not mobile and data.get("can_administer") and data.get("school_commission"):
        action_content = _school_contract_configuration(
            data["school_commission"], csrf, action_token
        ) + action_content
    if not mobile and data.get("multischool_commission"):
        from revenue.multischool_views import render_multischool_configuration
        action_content = render_multischool_configuration(data["multischool_commission"],csrf,action_token) + action_content
    mobile_variant = " revenue-workspace-page--mobile" if mobile else ""
    access_badge = "仅限管理员" if data.get("can_administer", True) else "已授权用户"
    revenue_home_href = str(data.get("revenue_home_href") or "/revenue")
    student_context = data.get("student_context") or {}
    if student_context:
        case_href = str(student_context["case_href"])
        breadcrumb = (
            f'<nav class="revenue-breadcrumb" aria-label="面包屑"><a href="{escape(revenue_home_href, quote=True)}">收入与佣金</a>'
            f'<span aria-hidden="true">/</span><a href="{escape(case_href, quote=True)}">收费与佣金档案</a>'
            f'<span aria-hidden="true">/</span><strong>当前学生客户收费</strong></nav>'
        )
        back_href = case_href
        back_label = "返回该学生档案"
    else:
        breadcrumb = (
            f'<nav class="revenue-breadcrumb" aria-label="面包屑"><a href="{escape(revenue_home_href, quote=True)}">收入与佣金</a>'
            f'<span aria-hidden="true">/</span><strong>{escape(str(data["title"]))}</strong></nav>'
        )
        back_href = revenue_home_href
        back_label = "返回总览"
    return f"""
    <link rel="stylesheet" href="/assets/revenue.css">
    <script src="/assets/revenue-payment.js?v=3" defer></script>
    <main class="revenue-workspace revenue-workspace-page{mobile_variant}" data-revenue-schema="{escape(str(data['schema_version']))}" data-revenue-workspace="{escape(str(data['name']), quote=True)}">
      {breadcrumb}
      <header class="revenue-page-heading">
        <div><span class="revenue-eyebrow">{access_badge}</span><h1>{escape(str(data['title']))}</h1><p>{escape(str(data['description']))}</p></div>
        <a class="revenue-back-link" href="{escape(back_href, quote=True)}">{escape(back_label)}</a>
      </header>
      <nav class="revenue-tabs" aria-label="收入与佣金工作区">{_workspace_tabs(data.get('tabs') or [])}</nav>
      {coverage}
      {student_detail}
      <section class="revenue-panel revenue-list-panel">
        <div class="revenue-panel-toolbar"><h2>{escape(str(data['title']))}</h2><nav class="revenue-filters" aria-label="筛选">{filters}</nav></div>
        {contract_search}
        <div class="revenue-table-scroll"><table><thead><tr>{header}</tr></thead><tbody>{''.join(rows)}</tbody></table></div>
        {paging}
      </section>
      {action_content}
      <input type="hidden" name="csrf" value="{escape(str(csrf), quote=True)}">
    </main>"""


def render_toorak_forecast_preview(preview, csrf, token, confirm_other):
    case_url = "/revenue/cases/" + quote(preview["case_uid"], safe="")
    component = escape(preview["component_uid"], quote=True)
    digest = escape(preview["digest"], quote=True)
    tier = preview["tier"]
    basis = "首年本地学费" if tier["tuition_kind"] == "local" else "首年国际学费"
    visa = preview["visa"]
    tuition = preview["tuition"]
    rule = preview["rule"]
    visa_line = ""
    if visa:
        visa_source = visa["document_id"] or visa["record_id"]
        visa_line = f'<p>签证 Subclass {escape(visa["subclass"])} · 批签证据编号 {int(visa_source)}</p>'
    return f"""<section class="revenue-panel">
      <h2>{escape(preview["school"])} · 学生预计佣金</h2>
      <p>学生 {escape(preview["student_id"])} · 实际开学日 {escape(preview["commencement"]["actual_commencement_date"])}</p>
      {visa_line}
      <p>{basis} {_aud_money(tuition["tuition_cents"])} · 学费证据编号 {int(tuition["evidence_document_id"])}</p>
      <p>合同 V{int(rule["version_no"])} · {escape(rule["effective_from"])} 至 {escape(rule["effective_to"])}
         · {escape(tier["source"])} · {tier["rate_basis_points"] / 100:g}%</p>
      <p>净佣金 {_aud_money(preview["net_cents"])} · GST {_aud_money(preview["gst_cents"])}
         · 含税 {_aud_money(preview["gross_cents"])}</p>
      <form method="post" action="{case_url}/toorak-forecast/save">
        {_hidden_security(csrf, token, "toorak-forecast-save")}
        <input type="hidden" name="component_uid" value="{component}">
        <input type="hidden" name="preview_digest" value="{digest}">
        <input type="hidden" name="confirm_other" value="{int(confirm_other)}">
        <button class="primary" type="submit">保存预计佣金</button>
      </form><a href="{case_url}">返回学生业务档案</a>
    </section>"""


def render_toorak_claim_preview(data, csrf, token, *, simulation=True):
    path = "/revenue/cases/" + quote(data["case_uid"],safe="")
    def evidence_value(item):
        return str(item.get("document_uid") or item["student_document_id"])

    evidence = "".join(
        f'<li>{escape(item["label"])} · {escape(item["filename"])} · {escape(evidence_value(item))}</li>'
        for item in data["evidence"]
    )
    keys = ("agent_document_id","enrolment_document_id","payment_document_id")
    hidden = "".join(
        f'<input type="hidden" name="{item.get("field",key)}" value="{escape(evidence_value(item),quote=True)}">'
        for key,item in zip(keys,data["evidence"])
    )
    claim_label = "内部测试 Claim" if simulation else "内部 Claim"
    recruited = "configured_rule" if data.get("school_commission_configurable") else "agent_named"
    title = "学校佣金" if data.get("school_commission_configurable") else "Toorak College"
    return f"""<section class="revenue-panel"><h2>{title} · 内部 Claim 核对</h2>
      <p>学生 {escape(data["student_id"])} · 开学 {escape(data["commencement"]["actual_commencement_date"])}</p>
      <ul>{evidence}</ul>
      <p>净佣金 {_aud_money(data["net_cents"])} · GST {_aud_money(data["gst_cents"])}
         · 含税 {_aud_money(data["gross_cents"])}</p>
      <p>{claim_label}。学校收到有效发票后才开始计算付款期限。</p>
      <form method="post" action="{path}/toorak-claim/issue">
        {_hidden_security(csrf,token,"toorak-claim-issue")}{hidden}
        <input type="hidden" name="line_uid" value="{escape(data["line_uid"],quote=True)}">
        <input type="hidden" name="preview_digest" value="{escape(data["digest"],quote=True)}">
        <input type="hidden" name="recruited" value="{recruited}">
        <input type="hidden" name="reviewed" value="1">
        <button class="primary" type="submit">确认并签发内部 Claim</button>
      </form><a href="{path}">返回学生业务档案</a>
    </section>"""


def _toorak_settlement_controls(line, path, csrf, token, *, allow_actions=True):
    data = line.get("settlement")
    if not data:
        return ""
    delivery = data["delivery"]
    reconciliation = data["reconciliation"]
    uid = escape(line["invoice_uid"],quote=True)
    hidden = f'<input type="hidden" name="invoice_uid" value="{uid}">'
    status = {"unpaid":"未收款","partial":"部分收款","awaiting_reconciliation":"已收清，待核销",
              "reconciled":"已核销"}[data["status"]]
    timing = {"late":"逾期后收清","due_day":"到期当天收清","early":"到期前收清",
              "overdue":"逾期未收清"}.get(data["timing"],"")
    received = escape(delivery["received_on"]) if delivery else "待确认"
    due = escape(delivery["due_date"]) if delivery else "待确认"
    term = ""
    if delivery:
        days = delivery.get("term_days",30)
        term = " · 立即到期" if days == 0 else f" · 付款期限 {int(days)} 天"
    allocation_inputs = ""
    if len(line.get("lines", [])) > 1:
        allocation_inputs = "".join(
            f'<label>{escape(item["student_id"])} · {escape(item["claim_number"])} · 未收 '
            f'{_aud_money(item["outstanding_cents"])}<input name="allocation_{escape(item["invoice_line_uid"],quote=True)}" '
            f'inputmode="decimal" placeholder="0.00" {"disabled" if item["outstanding_cents"]==0 else ""}></label>'
            for item in line["lines"]
        )
    by_id = {item["invoice_line_id"]:item["student_id"] for item in line.get("lines", [])}
    evidence_link = ""
    if delivery:
        evidence_link = f' · <a href="/revenue/documents/{quote(delivery["document_uid"],safe="")}/download">收件凭证</a>'
    payments = "".join(
        f'<li>{escape(payment["received_date"])} · {escape(payment["external_reference"])} · '
        f'{_aud_money(payment["invoice_cents"])} · {escape(by_id.get(payment.get("invoice_line_id"),""))} '
        f'<a href="/revenue/documents/{quote(payment["document_uid"],safe="")}/download">汇款凭证</a></li>'
        for payment in data["payments"] if payment["document_uid"]
    )
    action = ""
    evidence_input = '<label>PDF 凭证<input type="file" name="settlement_evidence" accept=".pdf,application/pdf" required></label>'
    fixed_term = line.get("payment_days")
    term_options = ((0,"立即到期"),(7,"7 天"),(15,"15 天"),(30,"30 天"))
    options = "".join(
        f'<option value="{days}"{" selected" if days==(30 if fixed_term is None else fixed_term) else ""}>{label}</option>'
        for days,label in term_options if fixed_term is None or fixed_term==days
    )
    if not delivery:
        action = f"""<form method="post" action="{path}/delivery" enctype="multipart/form-data" class="revenue-action-form">
          {_hidden_security(csrf,token,"toorak-delivery")}{hidden}
          <label>学校收到有效发票日期<input type="date" name="received_on" required></label>
          <label>付款期限<select name="term_days" required>{options}</select></label>{evidence_input}
          <button class="primary" type="submit">确认学校收件</button></form>"""
    elif data["outstanding_cents"]>0 and not reconciliation:
        action = f"""<form method="post" action="{path}/payment" enctype="multipart/form-data" class="revenue-action-form">
          {_hidden_security(csrf,token,"toorak-school-payment")}{hidden}
          <label>学校到账日<input type="date" name="received_on" required></label>
          <label>到账金额（AUD）<input name="amount_aud" inputmode="decimal" required></label>
          <label>银行流水参考号<input name="bank_reference" maxlength="100" required></label>{allocation_inputs}{evidence_input}
          <button class="primary" type="submit">登记学校到账</button></form>"""
    elif not reconciliation:
        action = f"""<form method="post" action="{path}/reconcile" class="revenue-action-form">
          {_hidden_security(csrf,token,"toorak-reconcile")}{hidden}
          <label><input type="checkbox" name="reviewed" value="1" required>已核对发票与全部学校银行流水</label>
          <button class="primary" type="submit">确认最终核销</button></form>"""
    else:
        action = (f'<p>核销记录 {escape(reconciliation["uid"])} · 管理员 #{int(reconciliation["confirmed_by"])}'
                  f' · {escape(reconciliation["confirmed_at"])}</p>')
    return f"""<div class="revenue-panel">
      <p>发票签发日 {escape(line["issue_date"])} · 学校收件日 {received} · 到期日 {due}{term}{evidence_link}</p>
      <p>{status} {timing} · 应收 {_aud_money(line["gross_cents"])}
         · 已收 {_aud_money(data["received_cents"])} · 未收 {_aud_money(data["outstanding_cents"])}</p>
      <ul>{payments}</ul>{action if allow_actions or reconciliation else ""}
    </div>"""


def _toorak_claim_controls(data, csrf, token):
    claim = data.get("toorak_claim")
    if not claim or not data.get("can_administer"):
        return ""
    path = "/revenue/cases/" + quote(data["case"]["uid"],safe="") + "/toorak-claim"
    fields = {}
    for field in claim["fields"]:
        options = "".join(f'<option value="{escape(str(doc["id"]),quote=True)}">{escape(doc["original_name"])}</option>'
                          for doc in field["options"])
        fields[field["name"]] = (f'<label>{escape(field["label"])}<select name="{field["name"]}" required>'
                                 f'<option value="">请选择证据</option>{options}</select></label>')
    blocks = []
    simulation = data.get("school_commission_simulation",True)
    invoice_label = "测试佣金发票" if simulation else "佣金发票"
    template_label = "合成测试发票格式" if simulation else "已审批发票模板和财务资料"
    finance_label = "测试财务资料" if simulation else "财务资料"
    for line in claim["lines"]:
        uid = escape(line["uid"],quote=True)
        if line["invoice_document_uid"]:
            document = quote(line["invoice_document_uid"],safe="")
            blocks.append(f'<p>内部 Claim {escape(line["claim_number"])} · {invoice_label} '
                          f'<a href="/revenue/documents/{document}/download">{escape(line["invoice_number"])}</a>'
                          '</p>')
            settlement_path = "/revenue/cases/" + quote(data["case"]["uid"],safe="") + "/toorak-settlement"
            invoice = quote(line["invoice_uid"],safe="")
            if line["grouped_invoice"]:
                blocks.append(f'<p><a href="/revenue/school-commission-invoices?invoice={invoice}">查看合开发票与学校到账</a></p>')
            else:
                blocks.append(_toorak_settlement_controls(
                    line,settlement_path,csrf,token,allow_actions=data.get("school_commission_settlement",True),
                ))
        elif line["claim_uid"]:
            blocks.append(f"""<form method="post" action="{path}/invoice" class="revenue-action-form">
              {_hidden_security(csrf,token,"toorak-claim-invoice")}
              <input type="hidden" name="claim_uid" value="{escape(line["claim_uid"],quote=True)}">
              <input type="hidden" name="template_revision" value="{escape(claim["template_revision"],quote=True)}">
              <input type="hidden" name="finance_revision" value="{escape(claim["finance_revision"],quote=True)}">
              <p>模板版本 {escape(claim["template_revision"][-10:] or "默认")} · {finance_label} {escape(claim["finance_revision"][-10:] or "默认占位")}</p>
              <p>内部 Claim {escape(line["claim_number"])} · {_aud_money(line["gross_cents"])}</p>
              <label><input type="checkbox" name="template_confirmed" value="1" required>确认使用{template_label}</label>
              <button class="primary" type="submit">生成{invoice_label}</button></form>""")
        elif line["status"] == "forecast":
            selected_fields = "".join(fields[key] for key in line.get("evidence_field_names",fields) if key in fields)
            recruitment = ('<input type="hidden" name="recruited" value="configured_rule">' if line.get("configurable")
                           else '<label>招募来源<select name="recruited" required><option value="">请选择</option>'
                                '<option value="agent_named">代理招募并提交署名申请</option>'
                                '<option value="direct">学生直接申请</option></select></label>')
            blocks.append(f"""<form method="post" action="{path}/preview" class="revenue-action-form">
              {_hidden_security(csrf,token,"toorak-claim-preview")}
              <input type="hidden" name="line_uid" value="{uid}">
              {selected_fields}{recruitment}
              <label><input type="checkbox" name="reviewed" value="1" required>已按合同核对该学生的申领条件和凭证</label>
              <button class="primary" type="submit">核对申领证据</button></form>""")
    intake = ""
    if data.get("school_commission_formal") and any(line["status"]=="forecast" for line in claim["lines"]):
        categories = "".join(
            f'<option value="{value}">{label}</option>' for value,label in (
                ("ToorakAgentNamedApplication","代理招募及署名申请"),
                ("ToorakEnrolmentConfirmation","学校注册确认"),
                ("ToorakSchoolTuitionPaid","学校课程费实收证据"),
            )
        )
        intake = f"""<form method="post" action="{path}/evidence" enctype="multipart/form-data"
                           class="revenue-action-form">
          {_hidden_security(csrf,token,"toorak-claim-evidence")}
          <label>申领凭证类别<select name="evidence_kind" required>{categories}</select></label>
          <label>PDF 凭证<input type="file" name="claim_evidence" accept=".pdf,application/pdf" required></label>
          <button type="submit">登记申领凭证</button></form>"""
    heading = "<h3>学校佣金测试申领与发票</h3>" if simulation else "<h3>学校佣金申领与发票</h3>"
    return heading + intake + "".join(blocks)


def render_revenue_case_workbench(
    data: dict, csrf: str = "", action_token: str = "", mobile: bool = False
) -> str:
    def render_value(value) -> str:
        if value is None or value == "":
            return "—"
        return escape(str(value))

    def case_section(key: str, title: str, content: str) -> str:
        return (
            f'<section class="revenue-panel revenue-case-section" id="revenue-case-{escape(key, quote=True)}">'
            f'<div class="revenue-panel-heading"><h2>{escape(title)}</h2></div>{content}</section>'
        )

    def definition_list(items: list[tuple[str, str]]) -> str:
        return '<dl class="revenue-case-record">' + "".join(
            f'<div><dt>{escape(label)}</dt><dd>{value}</dd></div>'
            for label, value in items
        ) + '</dl>'

    def student_application_content() -> str:
        summary = data.get("business_summary") or {}
        contract = data.get("contract_summary") or {}
        fields = [
            ("学生", render_value(summary.get("student_name"))),
            ("学生编号", render_value(summary.get("student_id"))),
            ("申请周期", render_value(summary.get("application_cycle"))),
            ("业务类型", render_value(summary.get("business_type"))),
            ("当前学校 / 目标学校", render_value(summary.get("institution"))),
            ("入学期", render_value(summary.get("intake"))),
            ("服务合同", render_value(contract.get("status_label"))),
            ("合同签署完成时间", render_value(contract.get("fully_executed_display"))),
            ("档案状态", render_value(summary.get("case_status"))),
        ]
        return definition_list(fields)

    def client_billing_content() -> str:
        cards = []
        for item in data.get("client_billing_summary") or []:
            add_payment = ""
            if not mobile and int(item.get("outstanding_base_cents") or 0) > 0:
                add_payment = (
                    f'<a class="revenue-inline-action" href="{escape(str(item["add_payment_href"]), quote=True)}">'
                    '+ 添加收款记录</a>'
                )
            cards.append(
                '<article class="revenue-billing-card">'
                f'<header><div><span class="revenue-eyebrow">基础服务费</span><h3>{escape(str(item.get("description") or "基础服务费"))}</h3></div>'
                f'<span class="revenue-status-pill">{escape(str(item.get("payment_status") or "待收款"))}</span></header>'
                '<dl class="revenue-money-summary">'
                f'<div><dt>基础服务费</dt><dd>{_aud_money(int(item.get("final_base_cents") or 0))}</dd></div>'
                f'<div><dt>基础服务费已收</dt><dd>{_aud_money(int(item.get("base_received_cents") or 0))}</dd></div>'
                f'<div><dt>GST 已收</dt><dd>{_aud_money(int(item.get("gst_collected_cents") or 0))}</dd></div>'
                f'<div class="is-outstanding"><dt>未收基础服务费</dt><dd>{_aud_money(int(item.get("outstanding_base_cents") or 0))}</dd></div>'
                f'</dl>{add_payment}</article>'
            )
        return "".join(cards) or '<p class="revenue-empty">暂无客户收费项目。</p>'

    def payment_receipt_content() -> str:
        payment_cards = []
        for item in data.get("payment_history") or []:
            evidence = (
                f'<a href="{escape(str(item["evidence_view_url"]), quote=True)}">查看付款凭证</a>'
                if item.get("evidence_view_url") else "—"
            )
            receipt = (
                f'<a href="{escape(str(item["receipt_view_url"]), quote=True)}">{escape(str(item.get("receipt_number") or "查看收据"))}</a>'
                if item.get("receipt_view_url") else "待生成"
            )
            payment_cards.append(definition_list([
                ("收款日期", render_value(item.get("date_display"))),
                ("付款方式", render_value(item.get("method_label"))),
                ("实际到账", _currency_money(str(item.get("currency_code") or "AUD"), int(item.get("actual_amount_cents") or 0))),
                ("基础服务费已收", _aud_money(int(item.get("allocated_base_cents") or 0))),
                ("GST 已收", _aud_money(int(item.get("gst_cents") or 0))),
                ("付款凭证", evidence),
                ("收据", receipt),
            ]))
        receipts = []
        for item in data.get("receipt_records") or []:
            view = (
                f'<a href="{escape(str(item["view_url"]), quote=True)}">查看收据</a>'
                if item.get("view_url") else "—"
            )
            receipts.append(definition_list([
                ("收据编号", render_value(item.get("receipt_number"))),
                ("金额", _currency_money(str(item.get("currency_code") or "AUD"), int(item.get("amount_cents") or 0))),
                ("签发时间", render_value(item.get("issued_at_display"))),
                ("文件", view),
            ]))
        return (
            '<div class="revenue-history-group"><h3>收款记录</h3>'
            + ("".join(payment_cards) or '<p class="revenue-empty">暂无收款记录</p>')
            + '</div><div class="revenue-history-group"><h3>收据</h3>'
            + ("".join(receipts) or '<p class="revenue-empty">暂无收据</p>')
            + '</div>'
        )

    def safe_generic_content(section: dict) -> str:
        safe_labels = {
            "component_kind": "入学组成", "source_kind": "权威来源",
            "status": "状态", "authority_level": "权威层级",
            "tuition_authority_level": "学费权威层级", "commission_status": "佣金状态",
            "match_status": "匹配状态", "异常原因": "异常原因", "状态": "状态",
            "说明": "说明", "技术信息": "技术信息", "event_type": "事件类型",
        }
        value_labels = {
            "school": "学校课程", "higher_education": "高等教育课程",
            "package_segment": "课程组合阶段", "school_enrolment": "学校在读记录",
            "final_acceptance": "最终接受记录", "higher_education_application": "高等教育申请",
            "higher_education_package_segment": "高等教育课程组合", "coe_language": "语言课程 COE",
            "official": "正式权威", "manual": "管理员核实", "estimated": "估算",
            "admin_confirmed": "管理员确认", "active": "当前有效", "confirmed": "已确认",
            "open": "待处理", "closed": "已关闭", "accrued": "已计提",
            "eligible": "可申领", "partial": "部分完成", "paid": "已收款",
            "disputed": "有争议", "pending": "待处理", "suggested": "待审核建议",
        }
        cards = []
        for row in section.get("rows") or []:
            items = []
            for key, value in row.items():
                if key not in safe_labels:
                    continue
                if key == "技术信息":
                    shown = f'<small class="revenue-exception-technical">小字技术信息：<code>{render_value(value)}</code></small>'
                else:
                    shown = render_value(value_labels.get(str(value), value))
                items.append((safe_labels[key], shown))
            if items:
                cards.append(definition_list(items))
        return "".join(cards) or '<p class="revenue-empty">暂无记录。</p>'

    contexts = "".join(
        f'<a href="{escape(str(item["href"]), quote=True)}">{escape(str(item["label"]))}</a>'
        for item in data.get("context_links") or []
    )
    def toorak_forecast_controls():
        if mobile or not data.get("can_administer") or not data.get("toorak_forecast_components"):
            return ""
        path = "/revenue/cases/" + quote(data["case"]["uid"], safe="") + "/toorak-forecast/preview"
        controls = []
        for component in data["toorak_forecast_components"]:
            if component["forecast_cents"] is not None:
                label = "预计佣金" if component["commission_status"] == "forecast" else "已有佣金明细"
                controls.append(f'<p>{label} · {_aud_money(component["forecast_cents"])}</p>')
                continue
            uid = escape(component["uid"], quote=True)
            school = component.get("school_name") or "Toorak College"
            visa_confirmation = ""
            if school.strip().casefold()=="toorak college":
                visa_confirmation = ('<label><input type="checkbox" name="confirm_other" value="1">'
                                     '已查验签证原件，确认属于合同所指的其他临时／永久居民签证</label>')
            controls.append(f"""<form method="post" action="{path}" class="revenue-action-form">
              {_hidden_security(csrf, action_token, "toorak-forecast-preview")}
              <input type="hidden" name="component_uid" value="{uid}">
              <p>{escape(school)} · 入学确认 {int(component["final_acceptance_id"])}</p>
              <p>请确认实际开学日，并登记有证据的首年学费（期间 year_1，费用类型 tuition_local 或 tuition_international）。</p>
              {visa_confirmation}
              <button class="primary" type="submit">预览预计佣金</button>
            </form>""")
        return "".join(controls)

    sections = []
    for section in data.get("sections") or []:
        key = str(section["key"])
        if key == "documents_evidence":
            continue
        if key == "student_application":
            content = student_application_content()
        elif key == "client_billing":
            content = client_billing_content()
        elif key == "invoice_payment_receipt":
            content = payment_receipt_content()
        elif key == "direct_commission":
            claim_controls = "" if mobile else _toorak_claim_controls(data, csrf, action_token)
            content = toorak_forecast_controls() + claim_controls + safe_generic_content(section)
        else:
            content = safe_generic_content(section)
        sections.append(case_section(key, str(section["title"]), content))
    technical_cards_parts = []
    for group_name, records in (data.get("technical_information") or {}).items():
        if not records:
            continue
        normalized_records = records if isinstance(records, list) else [records]
        for record in normalized_records:
            if not isinstance(record, dict):
                continue
            technical_cards_parts.append(
                '<section class="revenue-technical-record"><h3>'
                + escape(str(group_name).replace("_", " ").title())
                + "</h3><dl class=\"revenue-case-record\">"
                + "".join(
                    f'<div><dt>{escape(str(key).replace("_", " "))}</dt><dd>{render_value(value)}</dd></div>'
                    for key, value in record.items()
                )
                + "</dl></section>"
            )
    technical_cards = "".join(technical_cards_parts)
    technical_details = (
        '<details class="revenue-technical-details"><summary>技术信息 / 审计信息</summary>'
        f'<div class="revenue-technical-grid">{technical_cards}</div></details>'
        if technical_cards else ""
    )
    actions = ""
    if not mobile:
        operations = data.get("operations") or {}
        component_options = "".join(
            f'<option value="{escape(str(item["uid"]), quote=True)}">{escape(str(item["label"]))}</option>'
            for item in operations.get("components") or []
        )
        evidence_options = '<option value="">不关联 Revenue 文件</option>' + "".join(
            f'<option value="{int(item["id"])}">{escape(str(item["label"]))}</option>'
            for item in operations.get("evidence_documents") or []
        )
        operation_forms = ""
        if component_options:
            operation_forms = f"""
            <div class="revenue-case-operation-grid">
              <form method="post" action="/revenue/cases/tuition" class="revenue-action-form">
                {_hidden_security(csrf, action_token, 'case-tuition')}
                <label>入学组成<select name="component_uid" required>{component_options}</select></label>
                <label>学年 / 学期<input name="academic_period" required maxlength="60" placeholder="例如 2027 / Term 1"></label>
                <label>费用组成<input name="fee_component" required maxlength="80" value="tuition"></label>
                <label>学费（整数分）<input type="number" name="tuition_cents" min="0" step="1" required></label>
                <label>权威层级<select name="authority_level" required><option value="official">正式 Offer / Fee Schedule</option><option value="manual">管理员核实</option><option value="estimated">估算</option></select></label>
                <label>权威来源参考<input name="source_reference" required maxlength="160" placeholder="Offer / Fee Schedule 编号"></label>
                <label>Revenue 证据文件<select name="evidence_document_id">{evidence_options}</select></label>
                <button type="submit" class="primary">确认结构化学费</button>
              </form>
              <form method="post" action="/revenue/cases/commencement" class="revenue-action-form">
                {_hidden_security(csrf, action_token, 'case-commencement')}
                <label>入学组成<select name="component_uid" required>{component_options}</select></label>
                <label>实际入学日期<input type="date" name="actual_commencement_date" required></label>
                <label>Revenue 证据文件<select name="evidence_document_id">{evidence_options}</select></label>
                <button type="submit" class="primary">确认实际入学</button>
              </form>
            </div>"""
        actions = (
            '<section class="revenue-panel revenue-case-actions"><div class="revenue-panel-heading">'
            '<h2>收入与佣金操作</h2></div><nav>'
            + "".join(
                f'<a class="revenue-primary-link" href="{escape(str(item["href"]), quote=True)}">{escape(str(item["label"]))}</a>'
                for item in data.get("actions") or []
            )
            + f"</nav>{operation_forms}</section>"
        )
    variant = " revenue-case-workbench--mobile" if mobile else ""
    access_badge = "仅限管理员" if data.get("can_administer", True) else "已授权用户"
    surface = "mobile" if mobile else "desktop"
    case_list_href = cases_url(surface)
    home_href = revenue_home_url(surface)
    summary = data.get("business_summary") or {}
    student_name = str(summary.get("student_name") or "姓名待关联")
    student_id = str(summary.get("student_id") or "—")
    return f"""
    <link rel="stylesheet" href="/assets/revenue.css">
    <main class="revenue-workspace revenue-case-workbench{variant}" data-revenue-schema="{escape(str(data['schema_version']))}">
      <nav class="revenue-breadcrumb" aria-label="面包屑"><a href="{escape(home_href, quote=True)}">收入与佣金</a><span>/</span><a href="{escape(case_list_href, quote=True)}">收费与佣金档案</a></nav>
      <header class="revenue-page-heading revenue-case-identity"><div><span class="revenue-eyebrow">{access_badge}</span><h1>{escape(str(data['title']))}</h1><p><strong>{escape(student_name)}</strong><span>{escape(student_id)}</span></p></div><a class="revenue-back-link" href="{escape(case_list_href, quote=True)}">返回档案列表</a></header>
      <nav class="revenue-case-contexts" aria-label="权威业务上下文">{contexts}</nav>
      {actions}
      <div class="revenue-case-sections">{''.join(sections)}</div>
      {technical_details}
    </main>"""


def load_revenue_dashboard(connection: sqlite3.Connection) -> dict:
    return RevenueReadModels(connection).dashboard()


def render_revenue_initialization_page(data: dict) -> str:
    summary = data["proposal"]["summary"]
    exception_rows = "".join(
        f'<tr><td><strong>{escape(exception_code_label(code))}</strong>'
        f'<small class="revenue-exception-technical">小字技术信息：<code>{escape(str(code))}</code></small></td>'
        f'<td>{int(count)}</td></tr>'
        for code, count in sorted(data.get("exception_counts", {}).items())
    ) or '<tr><td colspan="2" class="revenue-empty">当前接入检查没有待处理事项。</td></tr>'
    return f"""
    <link rel="stylesheet" href="/assets/revenue.css">
    <main class="revenue-workspace" data-revenue-initialization-preview="read-only">
      <nav class="revenue-breadcrumb" aria-label="面包屑"><a href="/revenue">收入与佣金</a><span>/</span><span>系统设置</span><span>/</span><span>数据接入检查</span></nav>
      <header class="revenue-page-heading"><div><span class="revenue-eyebrow">仅限管理员 · 只读</span><h1>收费规则与数据接入检查</h1><p>此页面只生成受控接入检查信息；当前状态不会执行正式初始化、数据接入或收费写入。</p></div><a class="revenue-back-link" href="/revenue">返回收入与佣金</a></header>
      <section class="revenue-metrics" aria-label="收费规则与数据接入摘要">
        <article class="revenue-metric"><span>有效收费规则</span><strong>{int(data['active_fee_rules'])}</strong><small>数据库权威</small></article>
        <article class="revenue-metric"><span>支付方式</span><strong>{int(data['active_payment_methods'])}</strong><small>有效期版本</small></article>
        <article class="revenue-metric"><span>当前在办申请周期</span><strong>{int(summary['active_cycles'])}</strong><small>只读权威来源</small></article>
        <article class="revenue-metric"><span>待建立收费与佣金档案</span><strong>{int(summary['commercial_cases_to_create'])}</strong><small>不含阻塞项</small></article>
        <article class="revenue-metric"><span>待生成收费项目</span><strong>{int(summary['fee_items_to_create'])}</strong><small>尚未开票</small></article>
        <article class="revenue-metric"><span>待生成待处理事项</span><strong>{int(summary['exceptions_to_create'])}</strong><small>需要处理</small></article>
      </section>
      <section class="revenue-dashboard-grid">
        <article class="revenue-panel"><div class="revenue-panel-heading"><div><span class="revenue-eyebrow">受控接入批次</span><h2>接入检查信息</h2></div></div>
          <dl class="revenue-summary-list">
            <div><dt>接入批次编号</dt><dd>{escape(str(data['proposal']['proposal_uid']))}</dd></div>
            <div><dt>批次校验值</dt><dd><code>{escape(str(data['proposal']['proposal_sha256']))}</code></dd></div>
            <div><dt>数据库结构校验值</dt><dd><code>{escape(str(data['proposal']['source']['schema_fingerprint']))}</code></dd></div>
            <div><dt>收费规则校验值</dt><dd><code>{escape(str(data['proposal']['source']['fee_authority_fingerprint']))}</code></dd></div>
            <div><dt>当前状态</dt><dd>等待管理员批准正式接入</dd></div>
          </dl>
        </article>
        <article class="revenue-panel"><div class="revenue-panel-heading"><div><span class="revenue-eyebrow">需要处理</span><h2>待处理事项检查</h2></div></div>
          <div class="revenue-table-scroll"><table><thead><tr><th>业务说明</th><th>数量</th></tr></thead><tbody>{exception_rows}</tbody></table></div>
        </article>
      </section>
      <section class="revenue-panel"><div class="revenue-panel-heading"><div><span class="revenue-eyebrow">禁止自动制造历史财务</span><h2>本接入批次财务边界</h2></div></div>
        <p>历史兼容凭证不自动生成 · 收款 {int(summary['payments_to_create'])} · 收据 {int(summary['receipts_to_create'])} · 佣金申领 {int(summary['claims_to_create'])}</p>
      </section>
    </main>"""


def render_revenue_workspace(data: dict, csrf: str, mobile: bool = False) -> str:
    variant = " revenue-workspace--mobile" if mobile else ""
    recent = data.get("recent_cases") or []
    cases_href = str(data.get("cases_href") or "/revenue/cases")
    metrics = "".join(
        f'<a class="revenue-metric revenue-metric--{escape(str(item["key"]), quote=True)}" '
        f'href="{escape(str(item["href"]), quote=True)}">'
        f'<span>{escape(str(item["label"]))}</span>'
        f'<strong>{_money(int(item["value"])) if str(item["key"]).endswith("_cents") else int(item["value"])}</strong>'
        f'<small>查看明细</small></a>'
        for item in data.get("metrics") or []
    )
    pending = "".join(
        f'<a class="revenue-pending-item" href="{escape(str(item["href"]), quote=True)}">'
        f'<span>{escape(str(item["label"]))}</span><strong>{int(item["count"])}</strong></a>'
        for item in data.get("pending_items") or []
    ) or '<p class="revenue-empty">当前没有待办事项。</p>'
    workspace_links = "".join(
        f'<a class="revenue-workspace-link" href="{escape(str(item["href"]), quote=True)}">'
        f'<strong>{escape(str(item["label"]))}</strong><span>进入工作区</span></a>'
        for item in data.get("workspace_links") or []
    )
    quick_action_items = [
        item for item in data.get("quick_actions") or []
        if not mobile or item.get("mobile_safe")
    ]
    quick_actions = "".join(
        f'<a class="revenue-quick-action" href="{escape(str(item["href"]), quote=True)}">'
        f'<strong>{escape(str(item["label"]))}</strong>'
        f'<span>{escape(str(item["description"]))}</span></a>'
        for item in quick_action_items
    ) or '<p class="revenue-empty">当前没有可用快捷操作。</p>'
    desktop_rows = "".join(
        f'<tr><td><a class="revenue-row-link" href="{escape(str(row["href"]), quote=True)}">{escape(str(row["student_name"]))}</a></td>'
        f'<td>{escape(str(row["student_id"]))}</td><td>{escape(str(row["classification"]))}</td><td>{escape(str(row["business_type"]))}</td><td>{escape(str(row["institution"]))}</td>'
        f'<td>{escape(str(row["intake"]))}</td><td>{escape(str(row["billing_status"]))}</td>'
        f'<td>{escape(str(row["commission_status"]))}</td><td>{escape(str(row["status"]))}</td>'
        f'<td>{escape(str(row["updated_at_display"]))}</td></tr>'
        for row in recent
    ) or '<tr><td colspan="10" class="revenue-empty">暂无收费与佣金档案。</td></tr>'
    mobile_cases = "".join(
        f'<a class="revenue-mobile-case" href="{escape(str(row["href"]), quote=True)}">'
        f'<span class="revenue-mobile-case__top"><strong>{escape(str(row["student_name"]))}</strong><small>{escape(str(row["status"]))}</small></span>'
        f'<span>学生编号 {escape(str(row["student_id"]))}</span>'
        f'<span>{escape(str(row["business_type"]))} · {escape(str(row["classification"]))}</span>'
        f'<span>{escape(str(row["institution"]))} · {escape(str(row["intake"]))}</span>'
        f'<span class="revenue-mobile-case__states">{escape(str(row["billing_status"]))} · {escape(str(row["commission_status"]))}</span>'
        f'<small>最后更新 {escape(str(row["updated_at_display"]))}</small></a>'
        for row in recent
    ) or '<p class="revenue-empty">暂无收费与佣金档案。</p>'
    recent_content = (
        f'<div class="revenue-mobile-cases">{mobile_cases}</div>'
        if mobile
        else f'<div class="revenue-table-scroll"><table><thead><tr><th>学生</th><th>学生编号</th><th>客户分类</th><th>业务类型</th><th>院校</th><th>入学期</th><th>收费状态</th><th>佣金状态</th><th>档案状态</th><th>最后更新</th></tr></thead><tbody>{desktop_rows}</tbody></table></div>'
    )
    access_badge = "仅限管理员" if data.get("can_administer", True) else "已授权用户"
    system_tools = (
        '<nav class="revenue-panel revenue-system-tools" aria-label="收入与佣金系统设置">'
        '<span class="revenue-eyebrow">系统设置</span>'
        '<a href="/revenue/initialization">收费规则与数据接入检查</a></nav>'
        if data.get("can_administer", True) else ""
    )
    return f"""
    <link rel="stylesheet" href="/assets/revenue.css">
    <main class="revenue-workspace{variant}" data-revenue-schema="{escape(data['schema_version'])}">
      <header class="revenue-dashboard-heading">
        <div><span class="revenue-eyebrow">{access_badge}</span>
        <h1>收入与佣金</h1>
        <p>统一查看收费与佣金档案、客户收费、直接佣金、一级代理、合同与对账。</p></div>
        <div><a class="revenue-primary-link" href="{escape(cases_href, quote=True)}">查看收费与佣金档案</a></div>
      </header>
      <section class="revenue-metrics" aria-label="收入与佣金摘要">{metrics}</section>
      <nav class="revenue-workspace-links" aria-label="收入与佣金工作区">{workspace_links}</nav>
      <section class="revenue-panel revenue-quick-panel"><div class="revenue-panel-heading"><div><span class="revenue-eyebrow">已闭环能力</span><h2>快捷操作</h2></div></div><div class="revenue-quick-actions">{quick_actions}</div></section>
      <section class="revenue-dashboard-grid">
        <article class="revenue-panel revenue-recent-panel"><div class="revenue-panel-heading"><div><span class="revenue-eyebrow">实时数据</span><h2>最近有收费/佣金记录的学生</h2><p>仅显示近期已有收费或佣金档案的学生。查看完整学生名单，请点击“查看全部学生”。</p></div><a href="{escape(cases_href, quote=True)}">查看全部学生</a></div>{recent_content}</article>
        <aside class="revenue-panel revenue-pending-panel"><div class="revenue-panel-heading"><div><span class="revenue-eyebrow">需要关注</span><h2>待办事项</h2></div><a href="/revenue/reconciliation">进入对账</a></div><div class="revenue-pending-list">{pending}</div></aside>
      </section>
      {system_tools}
      <input type="hidden" name="csrf" value="{escape(str(csrf), quote=True)}">
    </main>"""
