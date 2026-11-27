"""Historical collection pages: review first, explicit confirmation second."""
from html import escape
from urllib.parse import urlencode
from decimal import Decimal
import json
from revenue.historical_receipts import reviews, summary, KINDS, available
from revenue.operational_visibility import RevenueOperationalVisibility

def money(cents, currency='AUD'):
    return '待核实' if cents is None else f'{currency} {Decimal(cents) / 100:,.2f}'

def page_data(c, student_id='', review_uid=''):
    c.row_factory = __import__('sqlite3').Row
    rows = reviews(c, student_id=student_id or None)
    visibility = RevenueOperationalVisibility(c)
    rows = [r for r in rows if visibility.is_student_visible(r['student_id'])]
    cycles = []
    if student_id and visibility.is_student_visible(student_id):
        cycles = [dict(r) for r in c.execute('SELECT id,student_id,cycle_type,status,education_stream FROM application_cycles WHERE student_id=? ORDER BY id DESC', (student_id,))]
    student = None
    if student_id and visibility.is_student_visible(student_id):
        student = dict(c.execute('SELECT * FROM students WHERE student_id=?', (student_id,)).fetchone())
    for row in rows:
        owner = dict(c.execute('SELECT * FROM students WHERE student_id=?', (row['student_id'],)).fetchone())
        row['student_name'] = ' '.join((str(owner.get(k) or '') for k in ('passport_first_name', 'passport_last_name'))).strip()
        row['summary'] = summary(row)
        row['evidence'] = [dict(r) for r in c.execute('SELECT e.evidence_kind,d.uid document_uid FROM revenue_historical_receipt_evidence e JOIN revenue_documents d ON d.id=e.document_id WHERE e.historical_receipt_id=? ORDER BY e.id', (row['id'],))]
        row['system_receipt'] = bool(row['payment_id'] and c.execute('SELECT 1 FROM revenue_receipts WHERE payment_id=?', (row['payment_id'],)).fetchone())
    selected = next((r for r in rows if r['uid'] == review_uid), None)
    return {'available': available(c), 'student': student, 'student_id': student_id, 'cycles': cycles, 'rows': rows[:100], 'selected': selected}

def _e(value):
    return escape(str(value if value is not None else ''), quote=True)


def _missing_facts(record):
    fields = [('claimed_received', '收款确认'), ('actual_cents', '实际金额'),
              ('currency_code', '到账币种'), ('received_date', '收款日期'),
              ('method_code', '付款方式')]
    missing = [label for key, label in fields if not record.get(key)]
    if record['service_kind'] == 'student_visa' and not record.get('visa_case_id'):
        missing.append('500签证案件归属')
    return missing


def _editor(data, csrf, token, record=None, *, new_label="新增历史收款核对"):
    from revenue.views import _hidden_security
    record = record or {}
    slots = {str(c['id']): [kind for kind in KINDS if not any(
        r['application_cycle_id'] == c['id'] and r['service_kind'] == kind
        for r in data['rows'])] for c in data['cycles']}
    if not record:
        free_cycles = [c for c in data['cycles'] if slots[str(c['id'])]]
        if not free_cycles:
            return ''
        record = {'application_cycle_id': free_cycles[0]['id'],
                  'service_kind': slots[str(free_cycles[0]['id'])][0]}
    uid = record.get('uid', '')
    editing = bool(uid)
    eid = 'hcr-edit-' + (uid or 'new')

    def option(value, label, selected):
        return f'<option value="{_e(value)}"' + (' selected' if str(selected or '') == str(value) else '') + f'>{_e(label)}</option>'

    def value(key):
        v = record.get(key)
        return '' if v is None else _e(Decimal(v) / 100 if key.endswith('_cents') else v)

    def select(name, label, choices, default='', locked=False):
        current = record.get(name, default)
        opts = ''.join(option(k, v, current) for k, v in choices)
        lock = ' disabled' if locked else ''
        hidden = f'<input type="hidden" name="{name}" value="{_e(current)}">' if locked else ''
        return f'<label>{label}<select name="{name}"{lock}>{opts}</select>{hidden}</label>'

    cycles = [(str(c['id']), f"周期 #{c['id']} · " + {'initial':'首次申请','transfer':'转学','progression':'升学','future':'未来申请'}.get(c['cycle_type'], c['cycle_type']) + (' · 已关闭' if c['status']=='closed' else ' · 进行中')) for c in data['cycles'] if editing or slots[str(c['id'])]]
    identity = select('application_cycle_id', '原申请周期', cycles, locked=editing)
    identity += select('service_kind', '核对费用类别', list(KINDS.items()), 'client_service', editing)
    identity += select('contract_state', '历史合同情况', [('historical_missing','历史未生成 / 未找到'),('documented','原合同已记录'),('not_required','本项无需服务合同')], 'historical_missing')
    identity += f'<label data-hcr-visa>独立学生签证案件编号（签证项必填）<input name="visa_case_id" type="number" min="1" value="{value("visa_case_id")}"><small>仅关联本学生、本周期的500学生签证。</small></label>'
    payment = select('claimed_received', '收款确认', [('0','是否收款尚待核实'),('1','用户已确认收过款')], 0)
    payment += select('currency_code', '实际到账币种', [('', '待核实'),('AUD','AUD 澳元'),('CNY','CNY 人民币')])
    for name, label in [('actual_cents','原币实际到账金额'),('agreed_aud_cents','历史议定服务费 AUD'),('settled_aud_cents','已核对的 AUD 结算金额（人民币勿填）')]:
        marker = ' data-hcr-settlement' if name == 'settled_aud_cents' else ''
        payment += f'<label{marker}>{label}<input name="{name}" type="number" inputmode="decimal" step="0.01" min="0" value="{value(name)}"></label>'
    payment += f'<label>实际收款日期<input name="received_date" type="date" value="{value("received_date")}"></label>'
    payment += select('method_code','付款方式',[('', '待核实'),('au_personal_account','澳洲私人账号'),('au_company_account','澳洲公司账号'),('alipay','支付宝'),('wechat','微信')])
    payment += '<p class="hcr-field-note" data-hcr-cny-note hidden>人民币按原币保存；AUD结算待单独核实，本轮不换算。</p>'
    security = _hidden_security(csrf, token, uid + 'editor')
    return f'''<details class="hcr-editor" id="{eid}"{ ' open' if editing or not data['rows'] else ''}>
      <summary>{'补充或修改核对资料' if editing else new_label}</summary>
      <form method="post" action="/revenue/historical/save" enctype="multipart/form-data" class="hcr-form" data-hcr-editor data-hcr-slots="{_e(json.dumps(slots))}">
        {security}<input type="hidden" name="student_id" value="{_e(data['student_id'])}">
        <input type="hidden" name="expected_revision" value="{record.get('revision',0)}"><input type="hidden" name="review_uid" value="{_e(uid)}">
        <p class="hcr-form-note">资料不齐可先保存。金额按历史事实核对，不按现价重算。</p>
        <fieldset><legend>归属与合同</legend><div class="hcr-fields">{identity}</div></fieldset>
        <fieldset><legend>历史实际收款</legend><div class="hcr-fields">{payment}</div></fieldset>
        <fieldset><legend>核对来源</legend><label>核对来源说明<textarea name="source_note" minlength="10" maxlength="2000" required rows="3">{value('source_note')}</textarea></label></fieldset>
        <label>付款凭证（可后补）<input type="file" name="historical_evidence" accept=".pdf,.png,.jpg,.jpeg"><small>支持 PDF、PNG 和 JPG/JPEG，最大 20 MB。</small></label>
        <div class="hcr-form-footer"><span>保存草稿不会产生正式收款、费用或收据。</span><button class="hcr-button hcr-button--primary">保存待核实资料</button></div>
      </form></details>'''


def render(data, csrf, token, *, mobile=False, can_administer=False, can_record=False):
    from revenue.views import _hidden_security
    base = '/mobile/revenue/historical-receipts' if mobile else '/revenue/historical-receipts'
    sid = data['student_id']
    editable = not mobile and (can_administer or can_record)
    selected = data['selected']
    student = data['student'] or {}
    student_name = ' '.join(str(student.get(k) or '') for k in ('passport_first_name','passport_last_name')).strip()
    search = f'''<form method="get" action="{base}" class="hcr-search"><label for="hcr-student">学生编号</label><div><input id="hcr-student" name="student" value="{_e(sid)}" placeholder="输入学生编号" required><button class="hcr-button hcr-button--secondary">查询</button></div></form>'''
    identity = f'<p class="hcr-student"><strong>{_e(student_name)}</strong><span>{_e(sid)}</span></p>' if student else ''
    cards = []
    for r in data['rows']:
        uid = r['uid']
        missing = _missing_facts(r)
        docs = r['evidence']
        badge_class = 'confirmed' if r['status']=='confirmed' else ('cancelled' if r['status']=='cancelled' else 'pending')
        detail_url = base + '?' + urlencode({'student':r['student_id'],'review':uid}) + '#hcr-edit-' + uid
        edit_link = ''
        if editable and r['status'] in ('draft','cancelled'):
            edit_link = f'<a class="hcr-button hcr-button--secondary" href="{_e(detail_url)}">'+('重新核对' if r['status']=='cancelled' else '补充资料')+'</a>'
        pending = ''
        if r['status']=='draft':
            pending = '<p class="hcr-pending">'+ ('待核对：' + '、'.join(missing) if missing else '资料已填写；确认时将检查归属及重复收费。') + '</p>'
        money_html = f'''<div class="hcr-money"><div class="hcr-actual"><span>原币实际到账</span><strong>{_e(money(r['actual_cents'],r['currency_code'] or '币种待核实'))}</strong></div><dl><div><dt>历史议定服务费</dt><dd>{_e(money(r['agreed_aud_cents']))}</dd></div><div><dt>已核对AUD结算</dt><dd>{_e(money(r['settled_aud_cents']))}</dd></div></dl></div>'''
        contract = {'documented':'原合同已记录','historical_missing':'历史未生成 / 未找到','not_required':'本项无需服务合同'}[r['contract_state']]
        method = {'au_personal_account':'澳洲私人账号','au_company_account':'澳洲公司账号','wechat':'微信','alipay':'支付宝'}.get(r['method_code'],'待核实')
        facts = f'<dl class="hcr-facts"><div><dt>实际收款日期</dt><dd>{_e(r["received_date"] or "待核实")}</dd></div><div><dt>付款方式</dt><dd>{method}</dd></div><div><dt>历史合同</dt><dd>{contract}</dd></div></dl>'
        if r['service_kind'] == 'student_visa':
            facts = facts.removesuffix('</dl>') + '<div><dt>500学生签证案件</dt><dd>' + _e(r['visa_case_id'] or '待核实') + '</dd></div></dl>'
        source = f'<details class="hcr-source"><summary>核对来源说明</summary><p>{_e(r["source_note"])}</p></details>'
        document_rows = []
        for kind,label in [('payment_evidence','付款凭证'),('external_receipt','历史服务费收据')]:
            linked = [d for d in docs if d['evidence_kind']==kind]
            links = ' '.join(f'<a aria-label="查看{label}{ "" if len(linked)==1 else str(i+1)}" href="/revenue/documents/{_e(d["document_uid"])}/view">查看{ "" if len(linked)==1 else str(i+1)}</a>' for i,d in enumerate(linked))
            upload_link = f'<a href="#hcr-upload-{uid}" data-hcr-disclose="hcr-upload-{uid}" data-evidence-kind="{kind}">补充</a>' if editable and r['status']!='cancelled' else ''
            document_rows.append(f'<div><dt>{label}</dt><dd><span class="hcr-doc-state{ " is-done" if linked else ""}">{"已补" if linked else "待补"}</span>{links or upload_link}</dd></div>')
        document_rows.append('<div><dt>系统收据</dt><dd>'+('已生成' if r['system_receipt'] else '未生成（不自动补开）')+'</dd></div>')
        upload = ''
        if editable and r['status']!='cancelled':
            upload = f'''<details class="hcr-upload" id="hcr-upload-{uid}"><summary>补充付款凭证或历史收据</summary><form method="post" enctype="multipart/form-data" action="/revenue/historical/evidence" class="hcr-form">{_hidden_security(csrf,token,uid+'evidence')}<input type="hidden" name="review_uid" value="{uid}"><label>资料类型<select name="evidence_kind"><option value="payment_evidence">付款凭证</option><option value="external_receipt">历史服务费收据</option></select></label><label>PDF或图片<input type="file" name="historical_evidence" accept=".pdf,.png,.jpg,.jpeg" required></label><button class="hcr-button hcr-button--secondary">保存凭证</button></form></details>'''
        confirm = ''
        cancel_form = ''
        if editable and can_administer and r['status']=='draft':
            cancel_form = f'''<form method="post" action="/revenue/historical/cancel">{_hidden_security(csrf,token,uid+'cancel')}<input type="hidden" name="review_uid" value="{uid}"><input type="hidden" name="revision" value="{r['revision']}"><button class="hcr-text-button">取消本次核对</button></form>'''
        if editable and r['status']=='draft':
            disabled = ' disabled' if missing else ''
            reason = '<p class="hcr-confirm-note">请先补齐上方待核对资料，再确认历史收款。</p>' if missing else ''
            confirm = f'''<div class="hcr-confirm-area">{reason}<form method="post" action="/revenue/historical/confirm" class="hcr-confirm-form">{_hidden_security(csrf,token,uid+'confirm')}<input type="hidden" name="review_uid" value="{uid}"><input type="hidden" name="revision" value="{r['revision']}"><label class="hcr-ack"><input type="checkbox" name="acknowledged" value="1" required{disabled}><span>已核对原始金额、币种、日期与来源；这是历史实际收款，不是新收费。</span></label><button class="hcr-button hcr-button--primary"{disabled}>确认历史实际收款</button></form>{cancel_form}</div>'''
        editor = _editor(data,csrf,token,r) if editable and selected and selected['uid']==uid and r['status'] in ('draft','cancelled') else ''
        owner = f'<p class="hcr-record-owner">{_e(r["student_name"])} {_e(r["student_id"])}</p>' if not sid else ''
        cards.append(f'''<article class="revenue-panel hcr-record"><header class="hcr-record-heading"><div>{owner}<h2>{_e(KINDS[r['service_kind']])}</h2><p>原申请周期 #{r['application_cycle_id']}</p></div><span class="hcr-badge hcr-badge--{badge_class}">{_e(r['summary'])}</span></header><div class="hcr-record-grid"><div class="hcr-record-main">{money_html}{pending}{facts}{source}{editor}</div><aside class="hcr-record-side"><div class="hcr-side-heading"><h3>凭证与收据</h3>{edit_link}</div><dl class="hcr-documents">{''.join(document_rows)}</dl>{upload}</aside></div>{confirm}</article>''')
    new_editor = ''
    if editable and data['cycles'] and not selected and data['available']:
        new_editor = _editor(data,csrf,token)
    notice = '<p class="hcr-readonly">手机只提供查看，请在桌面核对。</p>' if mobile else ''
    if not data['available']:
        content = '<section class="revenue-panel hcr-empty">历史核对功能尚未初始化。</section>'
    elif sid and not student:
        content = '<section class="revenue-panel hcr-empty">未找到可查看的学生，请核对学生编号。</section>'
    else:
        content = ''.join(cards) or '<section class="revenue-panel hcr-empty">暂无历史收款核对记录。'+('可在下方保存已掌握的资料，暂缺信息可后补。' if editable and data['cycles'] else '请先查询学生。')+'</section>'
    new_link = '<a class="hcr-button hcr-button--secondary" href="#hcr-edit-new" data-hcr-disclose="hcr-edit-new">新增核对</a>' if new_editor else ''
    back = '/mobile/revenue/cases' if mobile else '/revenue/cases'
    return f'''<link rel="stylesheet" href="/assets/revenue.css"><link rel="stylesheet" href="/assets/historical-receipts.css?v=20261123b"><main class="revenue-workspace hcr-workspace"><header class="revenue-panel hcr-page-heading"><div class="hcr-title-row"><div><h1>历史收款核对</h1><p>保留历史金额，分别核对收款事实、付款凭证与收据。</p></div><a class="hcr-back" href="{back}">返回收费与佣金档案</a></div><div class="hcr-toolbar">{identity}{search}{new_link}</div></header>{notice}{content}{new_editor}</main><script src="/assets/historical-receipts.js?v=20261123b" defer></script>'''

def render_case_summary(records, student_id, mobile=False):
    href = ('/mobile/revenue/historical-receipts' if mobile else '/revenue/historical-receipts') + '?' + urlencode({'student': student_id})
    content = ''.join(('<article class="revenue-billing-card"><h3>' + escape(KINDS[r['service_kind']]) + '</h3><p>' + escape(summary(r)) + '</p><p>历史议定 ' + escape(money(r['agreed_aud_cents'])) + ' · 实际到账 ' + escape(money(r['actual_cents'], r['currency_code'] or '币种待核实')) + '</p><p>付款凭证与历史收据分别核对；不会按现价重新收费。</p></article>' for r in records if r['status'] != 'cancelled'))
    return '<section class="revenue-panel"><h2>历史收款与凭证核对</h2>' + content + '<a href="' + escape(href, quote=True) + '">打开历史收款核对</a></section>'
