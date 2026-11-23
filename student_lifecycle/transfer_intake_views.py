"""Read-only controls for employee-confirmed parent schools and handoff recovery."""
from html import escape
from urllib.parse import quote
from student_lifecycle.transfer_intake import _dicts, parent_school_preview


def confirmation_fields(conn, student_id, token, *, disabled=False):
    preview = parent_school_preview(conn, student_id, token)
    disabled_attr = ' disabled' if disabled else ''
    choices = '<option value="">其他学校（确认名称）</option>'
    for row in _dicts(conn, "SELECT id,school_name,state FROM schools WHERE COALESCE(deleted_at,'')='' ORDER BY school_name,id"):
        selected = ' selected' if row['id'] == preview['suggested_school_id'] else ''
        label = f"{row['school_name']} · {row['state'] or ''} · #{row['id']}"
        choices += f'<option value="{row["id"]}"{selected}>{escape(label)}</option>'
    warning = '<p role="alert">学校名称有多个匹配，请核对州及学校后选择。</p>' if len(preview['school_matches']) > 1 else ''
    def field(name, value):
        return f'<input type="hidden" name="transfer_current_{name}" value="{escape(str(value), quote=True)}"{disabled_attr}>'
    return f'''<div data-transfer-current-confirmation data-transfer-intake-token="{escape(token,quote=True)}">
      <h4>确认家长填写的当前学校</h4>
      <p>家长填写：{escape(preview['parent_school_name'])} · {escape(preview['year_level'])}。确认后登记当前在读学校，入读日期留空。</p>{warning}
      {field('digest', preview['snapshot_digest'])}{field('token', token)}
      <label>确认学校<select name="transfer_current_school_id"{disabled_attr}>{choices}</select></label>
      <label>学校名称（选择其他学校时使用）<input name="transfer_current_school_name" value="{escape(preview['parent_school_name'],quote=True)}"{disabled_attr}></label>
      <label>当前年级<input name="transfer_current_year_level" value="{escape(preview['year_level'],quote=True)}" readonly{disabled_attr}></label>
      <label class="checkbox-label"><input type="checkbox" name="transfer_current_confirmed" value="yes"{disabled_attr}>我已核对当前学校和年级，确认登记为本次转学的当前在读记录</label>
    </div>'''


def signed_handoff_panels(conn, student_id, *, csrf, can_manage):
    rows = _dicts(conn, """SELECT id,application_cycle_id FROM contracts WHERE student_id=?
        AND contract_type='TransferApplicationContract' AND contract_status='FullyExecuted'
        AND COALESCE(deleted_at,'')='' AND COALESCE(voided_at,'')='' ORDER BY id DESC""", (student_id,))
    cards=[]
    for row in rows:
        if row['application_cycle_id'] is not None:
            # Successful handoffs disappear; normal Revenue controls show the fee.
            fee = conn.execute('''SELECT 1 FROM revenue_fee_item_contract_links l
                JOIN revenue_fee_items f ON f.id=l.fee_item_id
                WHERE l.contract_id=? AND f.status<>'cancelled' LIMIT 1''',(row['id'],)).fetchone()
            if fee:
                continue
        source = conn.execute('SELECT parent_form_token FROM transfer_source_authorities WHERE transfer_contract_id=?',(row['id'],)).fetchone()
        requests = _dicts(conn, '''SELECT request_status,last_error_message FROM external_transfer_case_requests
            WHERE contract_id=? ORDER BY id DESC LIMIT 1''',(row['id'],))
        reason = requests[0]['last_error_message'] if requests else '合同已签妥，转学与收费尚未完成衔接。'
        cancelled = bool(requests and requests[0]['request_status'] == 'cancelled')
        if cancelled:
            reason = '本次转学请求已取消；已签合同保留，请核对后通过新的业务申请处理。'
        allow_recovery = can_manage and not cancelled
        fields=''
        if allow_recovery and row['application_cycle_id'] is None and source and source[0]:
            try:
                fields=confirmation_fields(conn,student_id,source[0])
            except (ValueError,TypeError) as error:
                reason=str(error)
        base=f'/students/{quote(str(student_id))}/transfer-intake/{row["id"]}'
        controls=''
        if allow_recovery:
            action='confirm' if fields else 'retry'
            controls=f'''<form method="post" action="{base}/{action}" class="stack-form">
                <input type="hidden" name="csrf" value="{escape(csrf,quote=True)}">{fields}
                <button type="submit">{'确认学校并完成转学收费建档' if fields else '重试转学及收费建档'}</button></form>'''
        cards.append(f'''<section class="notice warning transfer-intake-handoff" data-transfer-intake-contract="{row['id']}">
            <h3>合同 #{row['id']} 已签妥 · 待完成转学收费建档</h3><p>{escape(reason or '')}</p>
            <p>目标学校可稍后补充；已签署合同保留。</p>{controls}</section>''')
    return ''.join(cards)
