"""Server-rendered Higher Education School Plan and application workspace."""

import json

from student_applications_display import application_status_label, course_type_label

from ui import esc


def _value(row, key, default=""):
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        value = default
    return default if value is None else value


def render_higher_education_workspace(*, providers, relationships, plans, applications, csrf, can_manage, cycles=(), plan_items=(), offers=(), temporary_shares=(), can_review=False, pathways=(), courses=(), can_view_legacy_commission=False, legacy_commission_writes_closed=False):
    universities = [row for row in providers if _value(row, "provider_type") == "University" and _value(row, "active_status") == "Active"]
    university_options = "".join(f'<option value="{esc(_value(row, "id"))}">{esc(_value(row, "provider_name"))}</option>' for row in universities)
    university_cards = "".join(
        f'<article class="he-card" data-mobile-card="provider"><span class="he-badge">大学</span><h3>{esc(_value(row, "provider_name"))}</h3><p>{esc(_value(row, "state") or "澳大利亚")}</p></article>'
        for row in universities
    ) or '<p class="muted-line">暂无已审核通过的大学。</p>'
    pathway_data = [
        {"id": int(_value(row, "id", 0)), "university_id": int(_value(row, "university_id", 0)), "name": str(_value(row, "display_name")), "final_level": str(_value(row, "final_course_level"))}
        for row in pathways
    ]
    course_data = [
        {"id": int(_value(row, "id", 0)), "university_id": int(_value(row, "university_id", 0)), "name": str(_value(row, "course_name")), "level": str(_value(row, "course_level"))}
        for row in courses
    ]
    pathway_json = json.dumps(pathway_data, ensure_ascii=False).replace("<", "\\u003c")
    course_json = json.dumps(course_data, ensure_ascii=False).replace("<", "\\u003c")
    cycle_options = "".join(
        f'<option value="{esc(_value(row, "id"))}">{esc(_value(row, "student_id"))} · 申请周期 {esc(_value(row, "id"))}</option>'
        for row in cycles
    )
    items_by_plan = {}
    for item in plan_items:
        items_by_plan.setdefault(int(_value(item, "plan_id", 0)), []).append(item)

    def plan_item_card(item, confirmed):
        submit = ""
        if confirmed:
            channel_fields = '<select name="submission_channel"><option value="Direct">直接递交</option></select>'
            if can_view_legacy_commission:
                channel_fields = '<select name="submission_channel"><option value="Direct">直接递交</option><option value="Master Agent">主代理递交</option></select><input name="master_agent_id" placeholder="主代理编号">'
            submit = f'<form method="post" action="/higher-education/plan-items/{esc(_value(item, "id"))}/submit"><input type="hidden" name="csrf" value="{esc(csrf)}"><input name="intake" value="{esc(_value(item, "intake"))}" required>{channel_fields}<button class="primary compact">标记已递交</button></form>'
        remove = "" if confirmed else f'''<form method="post" action="/higher-education/plan-items/{esc(_value(item, "id"))}/remove"><input type="hidden" name="csrf" value="{esc(csrf)}"><button class="ghost compact" type="submit">移除</button></form>'''
        return f'''<article class="he-university-application" data-mobile-card="university-application">
          <span class="he-badge">{esc(application_status_label(_value(item, "status") or "Draft"))}</span><h3>{esc(_value(item, "provider_name") or ("大学 #" + str(_value(item, "provider_id"))))}</h3>
          <dl><div><dt>升学路径</dt><dd>{esc(_value(item, "pathway_name") or course_type_label(_value(item, "course_type")))}</dd></div><div><dt>课程 / 专业</dt><dd>{esc(_value(item, "course_name"))}</dd></div><div><dt>入学时间</dt><dd>{esc(_value(item, "intake") or "未设置")}</dd></div></dl>{remove}{submit}
        </article>'''

    def plan_card(plan):
        plan_id = int(_value(plan, "id", 0))
        confirmed = _value(plan, "status") == "Confirmed"
        item_cards = "".join(plan_item_card(item, confirmed) for item in items_by_plan.get(plan_id, [])) or '<p class="muted-line">尚无大学申请。</p>'
        add_form = ""
        if not confirmed:
            add_form = f'''<form class="he-add-university-form" method="post" action="/higher-education/plans/{plan_id}/items" data-he-university-form>
              <input type="hidden" name="csrf" value="{esc(csrf)}"><h3>添加大学</h3>
              <label>大学<select name="provider_id" required data-he-university-select><option value="">请选择大学</option>{university_options}</select></label>
              <label>升学路径<select name="pathway_combination_id" required data-he-pathway-select disabled><option value="">请先选择大学</option></select></label>
              <label>课程 / 专业<input name="course_name" list="he-course-library-{plan_id}" placeholder="搜索课程 / 专业" autocomplete="off" required data-he-course-input><datalist id="he-course-library-{plan_id}" data-he-course-list></datalist><small>找不到？可添加新课程 / 专业，保存后自动加入该大学课程库。</small></label>
              <label>课程层级<select name="course_level" data-he-course-level required><option value="">请先选择升学路径</option></select></label>
              <label>入学时间<input name="intake" placeholder="例如：2027年2月" required></label><button class="primary">添加大学</button>
            </form><form method="post" action="/higher-education/plans/{plan_id}/confirm"><input type="hidden" name="csrf" value="{esc(csrf)}"><button class="secondary">确认高等教育学校方案</button></form>'''
        return f'''<article class="he-plan" data-mobile-card="plan"><header><div><span class="eyebrow">学生 {esc(_value(plan, "student_id"))}</span><h2>高等教育学校方案 #{plan_id}</h2></div><span class="he-badge">{esc(application_status_label(_value(plan, "status")))}</span></header><div class="he-default-pathway"><span>家长选择的升学路径</span><strong>默认升学路径： {esc(_value(plan, "default_pathway_code") or "未记录")}</strong><small>每所大学可采用不同的实际升学路径。</small></div><div class="he-university-grid">{item_cards}</div>{add_form}</article>'''

    plan_cards = "".join(plan_card(plan) for plan in plans) or '<p class="muted-line">尚无高等教育学校方案。</p>'
    create_plan = ""
    if can_manage:
        create_plan = f'<form class="panel he-create-plan" method="post" action="/higher-education/plans/create"><input type="hidden" name="csrf" value="{esc(csrf)}"><h2>新建高等教育学校方案</h2><label>当前高等教育申请周期<select name="application_cycle_id" required><option value="">请选择学生</option>{cycle_options}</select></label><button class="primary">新建学校方案</button></form>'

    offers_by_application = {}
    for offer in offers:
        offers_by_application.setdefault(int(_value(offer, "application_id", 0)), []).append(offer)
    shares_by_application = {}
    if can_view_legacy_commission:
        for share in temporary_shares:
            shares_by_application.setdefault(int(_value(share, "application_id", 0)), []).append(share)

    def application_card(row):
        application_id = int(_value(row, "id", 0))
        offer_actions = "".join(f'<form method="post" action="/higher-education/offers/{esc(_value(offer, "id"))}/accept"><input type="hidden" name="csrf" value="{esc(csrf)}"><span>Offer #{esc(_value(offer, "id"))} · {esc(application_status_label(_value(offer, "status")))}</span>' + ('<button class="primary compact">接受 Offer</button>' if _value(offer, "status") == "Received" else '') + '</form>' for offer in offers_by_application.get(application_id, []))
        legacy_archive = ""
        if can_view_legacy_commission:
            share_actions = "".join(
                f'<div class="he-legacy-share"><span>Temporary share {esc(_value(share, "share_percent"))}% · {esc(_value(share, "status"))}</span>'
                + (
                    f'<form method="post" action="/higher-education/temporary-shares/{esc(_value(share, "id"))}/review"><input type="hidden" name="csrf" value="{esc(csrf)}"><button name="decision" value="Approved">Approve</button></form>'
                    if can_review and not legacy_commission_writes_closed and _value(share, "status") == "Pending"
                    else ""
                )
                + "</div>"
                for share in shares_by_application.get(application_id, [])
            )
            write_forms = ""
            if not legacy_commission_writes_closed:
                share_form = f'<form method="post" action="/higher-education/applications/{application_id}/temporary-shares"><input type="hidden" name="csrf" value="{esc(csrf)}"><input name="share_percent" required><input name="evidence" required><button>Submit Share</button></form>' if _value(row, "submission_channel") == "Master Agent" else ""
                commission_form = f'<form method="post" action="/higher-education/applications/{application_id}/commission-lines"><input type="hidden" name="csrf" value="{esc(csrf)}"><input name="teaching_provider_id" required><select name="source_type"><option>Agreement</option><option>Temporary Share</option></select><input name="source_id" required><button>Create Commission Line</button></form>'
                write_forms = share_form + commission_form
            legacy_archive = f'<section class="he-legacy-commission"><h4>Legacy HE Commission Archive</h4>{share_actions}{write_forms}</section>'
        return f'''<article class="he-card" data-mobile-card="application"><span class="he-badge">{esc(application_status_label(_value(row, "status")))}</span><h3>{esc(_value(row, "provider_name"))}</h3><p>{esc(_value(row, "pathway_name") or course_type_label(_value(row, "course_type")))} · {esc(_value(row, "course_name"))} · {esc(_value(row, "intake"))}</p><form method="post" action="/higher-education/applications/{application_id}/offers"><input type="hidden" name="csrf" value="{esc(csrf)}"><button>记录 Offer</button></form>{offer_actions}<form method="post" action="/higher-education/applications/{application_id}/coe-links"><input type="hidden" name="csrf" value="{esc(csrf)}"><input name="coe_oshc_id" required><button>关联 CoE</button></form>{legacy_archive}</article>'''

    application_cards = "".join(application_card(row) for row in applications) or '<p class="muted-line">尚无已递交的大学申请。</p>'
    legacy_cutover_notice = ""
    if can_view_legacy_commission and legacy_commission_writes_closed:
        legacy_cutover_notice = '''<section class="panel he-legacy-commission he-legacy-commission--closed"><h2>Legacy HE Commission Archive</h2><p>Historical records are read-only after Revenue cutover. No backfill is performed and all new financial records belong to Revenue &amp; Commission.</p></section>'''
    return f'''<section class="he-workspace" data-higher-education-workspace><script type="application/json" data-he-pathway-source>{pathway_json}</script><script type="application/json" data-he-course-source>{course_json}</script><header class="he-hero"><div><span class="eyebrow">高等教育申请</span><h1>高等教育申请</h1><p>升学路径提供机构 · 大学 · 课程 / 专业 · 入学时间</p></div><span class="status waiting">无需合同／不适用</span></header>{legacy_cutover_notice}<section class="panel"><div class="panel-head"><div><h2>大学申请</h2><p>家长选择的升学路径作为默认值；每所大学分别保留实际升学路径。</p></div><a class="secondary button-link" href="/schools/universities">大学列表</a></div><div class="he-card-grid">{university_cards}</div></section>{create_plan}<section class="he-plans">{plan_cards}</section><section class="panel"><h2>申请进度</h2><div class="he-card-grid">{application_cards}</div></section></section>'''
