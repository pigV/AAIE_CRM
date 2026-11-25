"""Explicit ownership choices for documents when active application streams coexist."""
from html import escape


def render_coe_cycle_selection(connection, student_id, *, selected=None, initial_only=False):
    columns = {
        row[1] for row in connection.execute('PRAGMA table_info(application_cycles)')
    }
    if not columns:
        return ''
    stream_column = 'education_stream' if 'education_stream' in columns else "'school_education'"
    cycles = connection.execute(
        f"""SELECT id, {stream_column} FROM application_cycles
             WHERE student_id=? AND status='active' ORDER BY id""",
        (str(student_id),),
    ).fetchall()
    active_count=len(cycles)
    if initial_only:
        allowed={row[0] for row in connection.execute(
            "SELECT id FROM application_cycles WHERE student_id=? AND status='active' AND cycle_type='initial'",(str(student_id),))}
        cycles=[row for row in cycles if row[0] in allowed]
        if not cycles:
            return '<p class="muted-line">转学或升学材料请从对应业务的上传入口提交。</p>'
    if active_count <= 1:
        return ''
    offer_columns = {row[1] for row in connection.execute('PRAGMA table_info(offers)')}
    options = ['<option value="">请选择所属申请，不会自动选择</option>']
    for cycle_id, education_stream in cycles:
        offers = []
        if {'student_id', 'application_cycle_id', 'school_name'} <= offer_columns:
            offers = connection.execute(
                """SELECT DISTINCT school_name FROM offers
                    WHERE student_id=? AND application_cycle_id=? ORDER BY school_name""",
                (str(student_id), cycle_id),
            ).fetchall()
        names = '、'.join(str(row[0]) for row in offers if row[0]) or '暂无已关联 Offer'
        stream_label = '高等教育申请' if education_stream == 'higher_education' else '学校教育申请'
        label = f'{stream_label} #{cycle_id} · {names}'
        selected_attribute = ' selected' if str(selected or '') == str(cycle_id) else ''
        options.append(
            f'<option value="{int(cycle_id)}"{selected_attribute}>{escape(label)}</option>'
        )
    return (
        '<label>本批材料所属申请<select name="coe_application_cycle_id" required>'
        + ''.join(options)
        + '</select><small>请核对材料对应的申请及学校；同一批文件使用同一所属申请。</small></label>'
    )
