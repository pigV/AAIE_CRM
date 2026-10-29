"""School Management views for university master data."""

from urllib.parse import parse_qs, quote, urlencode, urlsplit

from ui import esc


def _value(row, key, default=""):
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        value = default
    return default if value is None else value


def render_australian_universities_card(count):
    return f'''<a class="metric clickable-metric australian-universities-card" href="/schools/universities">
      <span>School Management</span><strong>Universities</strong>
      <small>澳洲大学 · {int(count or 0)} institutions</small>
    </a>'''


def _directory_url(filters, page):
    params = {
        "q": str((filters or {}).get("q") or ""),
        "state": str((filters or {}).get("state") or ""),
        "status": str((filters or {}).get("status") or ""),
        "page": max(1, int(page or 1)),
    }
    return "/schools/universities?" + urlencode(params)


def safe_universities_return_path(value):
    """Keep only the directory's documented query context."""
    target = str(value or "").strip()
    parsed = urlsplit(target)
    if parsed.path != "/schools/universities" or parsed.scheme or parsed.netloc:
        return "/schools/universities"
    incoming = parse_qs(parsed.query)
    filters = {
        "q": str((incoming.get("q") or [""])[0])[:120],
        "state": str((incoming.get("state") or [""])[0]).upper(),
        "status": str((incoming.get("status") or [""])[0]).title(),
    }
    try:
        page = max(1, int((incoming.get("page") or ["1"])[0]))
    except (TypeError, ValueError):
        page = 1
    if not any(filters.values()) and page == 1:
        return "/schools/universities"
    return _directory_url(filters, page)


def render_australian_universities_list(
    universities,
    *,
    can_manage,
    csrf="",
    filters=None,
    pagination=None,
):
    filters = filters or {"q": "", "state": "", "status": ""}
    pagination = pagination or {"page": 1, "page_size": 12, "total": len(universities), "pages": 1}
    q = str(filters.get("q") or "")
    selected_state = str(filters.get("state") or "")
    selected_status = str(filters.get("status") or "")
    states = ("VIC", "NSW", "QLD", "SA", "WA", "ACT", "TAS", "NT")
    state_options = '<option value="">All states</option>' + "".join(
        f'<option value="{state}"{" selected" if selected_state == state else ""}>{state}</option>'
        for state in states
    )
    status_options = '<option value="">All statuses</option>' + "".join(
        f'<option value="{status}"{" selected" if selected_status == status else ""}>{status}</option>'
        for status in ("Active", "Inactive")
    )
    cards = []
    directory_context = _directory_url(filters, pagination.get("page") or 1)
    has_directory_context = bool(q or selected_state or selected_status or int(pagination.get("page") or 1) > 1)
    for row in universities:
        university_id = int(_value(row, "id", 0))
        name = str(_value(row, "provider_name") or "Unnamed university")
        short_name = str(_value(row, "short_name") or "")
        state = str(_value(row, "state") or "")
        city = str(_value(row, "city") or "")
        location = " · ".join(part for part in (state, city) if part) or "Location not recorded"
        status = str(_value(row, "active_status") or "Status not recorded")
        logo = short_name or "No logo"
        detail_href = f"/schools/universities/{university_id}"
        if has_directory_context:
            detail_href += "?return_to=" + quote(directory_context, safe="")
        cards.append(
            f'''<article class="university-directory-card" data-university-id="{university_id}">
              <div class="university-directory-card__top"><span class="university-directory-logo" aria-label="University logo or abbreviation">{esc(logo)}</span><span class="status university-directory-status">{esc(status)}</span></div>
              <div class="university-directory-card__body"><h2>{esc(name)}</h2><p>{esc(location)}</p><small>Available Pathways: {esc(_value(row, "pathway_count", 0))}</small></div>
              <a class="secondary button-link university-directory-action" href="{esc(detail_href)}">View Details</a>
            </article>'''
        )
    has_filters = bool(q or selected_state or selected_status)
    empty_text = (
        "No universities match the current search or filters."
        if has_filters
        else "No universities configured."
    )
    grid = "".join(cards) or f'<div class="empty universities-directory-empty">{empty_text}</div>'
    page = max(1, int(pagination.get("page") or 1))
    pages = max(1, int(pagination.get("pages") or 1))
    previous = (
        f'<a class="secondary button-link" href="{esc(_directory_url(filters, page - 1))}">Previous</a>'
        if page > 1 else '<span aria-disabled="true">Previous</span>'
    )
    following = (
        f'<a class="secondary button-link" href="{esc(_directory_url(filters, page + 1))}">Next</a>'
        if page < pages else '<span aria-disabled="true">Next</span>'
    )
    create_form = ""
    if can_manage:
        create_form = f'''<details class="universities-directory-create"><summary class="primary button-link">Add University</summary>
          <form class="stack-form" method="post" action="/higher-education/providers/create">
            <input type="hidden" name="csrf" value="{esc(csrf)}"><input type="hidden" name="provider_type" value="University"><input type="hidden" name="return_to" value="/schools/universities">
            <label>University Name<input name="provider_name" required></label><label>Source URL<input name="source_url" type="url"></label><label>Evidence<textarea name="evidence"></textarea></label><button class="primary">Save University</button>
          </form></details>'''
    return f'''<section class="panel australian-universities-page universities-directory">
      <div class="panel-head universities-directory-header"><div><span class="eyebrow">SCHOOL MANAGEMENT</span><h1>Universities</h1><p>大学、Pathway Architecture 与 Course Library 基础资料。</p></div><div class="row-actions">{create_form}<a class="secondary button-link" href="/schools">返回学校管理</a></div></div>
      <form class="filter-bar universities-directory-filters" method="get" action="/schools/universities">
        <label class="wide">Search<input name="q" value="{esc(q)}" placeholder="University name, abbreviation or city"></label>
        <label>State<select name="state">{state_options}</select></label><label>Status<select name="status">{status_options}</select></label><button class="primary">Search</button><a class="secondary button-link" href="/schools/universities">Clear</a>
      </form>
      <div class="universities-directory-grid">{grid}</div>
      <nav class="universities-directory-pagination" aria-label="Universities pages">{previous}<strong>Page {page} of {pages}</strong>{following}</nav>
    </section>'''


def render_university_detail(
    *, university, pathways, segments, courses, providers, relationships, csrf,
    can_manage, list_context="", course_filters=None, course_pagination=None,
):
    university_id = int(_value(university, "id", 0))
    directory_url = safe_universities_return_path(list_context)
    detail_url = f"/schools/universities/{university_id}"
    edit_url = f"{detail_url}/edit?return_to={quote(directory_url, safe='')}"
    course_filters = course_filters or {"cq": ""}
    course_pagination = course_pagination or {"page": 1, "pages": 1, "total": len(courses)}
    course_query = str(course_filters.get("cq") or "")
    segments_by_pathway = {}
    for segment in segments:
        segments_by_pathway.setdefault(int(_value(segment, "pathway_combination_id", 0)), []).append(segment)
    pathway_cards = "".join(
        '<article class="he-card" data-mobile-card="university-pathway">'
        f'<span class="he-badge">{esc(_value(pathway, "active_status"))}</span><h3>{esc(_value(pathway, "display_name"))}</h3>'
        f'<p>Final: {esc(_value(pathway, "final_course_level"))}</p>'
        + '<ol>' + "".join(
            f'<li>{esc(_value(segment, "course_level"))} · {esc(_value(segment, "provider_name"))}</li>'
            for segment in segments_by_pathway.get(int(_value(pathway, "id", 0)), [])
        ) + '</ol>'
        + (f'<small><a href="{esc(_value(pathway, "source_url"))}">Source / Evidence</a></small>' if _value(pathway, "source_url") else '')
        + '</article>'
        for pathway in pathways
    ) or '<div class="empty university-detail-empty">No pathways have been added</div>'
    course_rows = "".join(
        f'<tr><td>{esc(_value(course, "course_name"))}</td><td>{esc(_value(course, "course_level"))}</td><td>{esc(_value(course, "specialisation") or "—")}</td><td>{esc(_value(course, "active_status"))}</td>'
        + (f'<td><form method="post" action="/schools/universities/{university_id}/courses/{esc(_value(course, "id"))}/status"><input type="hidden" name="csrf" value="{esc(csrf)}"><input type="hidden" name="return_to" value="{esc(directory_url)}"><input type="hidden" name="active_status" value="{"Inactive" if _value(course, "active_status") == "Active" else "Active"}"><button class="ghost compact">{"Deactivate" if _value(course, "active_status") == "Active" else "Activate"}</button></form><form method="post" action="/schools/universities/{university_id}/courses/{esc(_value(course, "id"))}/merge"><input type="hidden" name="csrf" value="{esc(csrf)}"><input type="hidden" name="return_to" value="{esc(directory_url)}"><input type="number" name="canonical_course_id" min="1" placeholder="Canonical Course ID" required><button class="ghost compact">Merge duplicate</button></form></td>' if can_manage else '<td>—</td>')
        + '</tr>'
        for course in courses
    ) or '<tr><td colspan="5" class="empty">No courses or programs have been added</td></tr>'
    provider_options = "".join(
        f'<option value="{esc(_value(provider, "id"))}">{esc(_value(provider, "provider_name"))}</option>'
        for provider in providers
    )
    pathway_options = "".join(
        f'<option value="{esc(_value(pathway, "id"))}">{esc(_value(pathway, "display_name"))}</option>'
        for pathway in pathways
    )
    course_page = max(1, int(course_pagination.get("page") or 1))
    course_pages = max(1, int(course_pagination.get("pages") or 1))

    def course_page_url(page):
        params = {"return_to": directory_url, "cq": course_query, "cp": max(1, int(page))}
        return f"{detail_url}?{urlencode(params)}"

    course_previous = (
        f'<a class="secondary button-link" href="{esc(course_page_url(course_page - 1))}">Previous courses</a>'
        if course_page > 1 else '<span aria-disabled="true">Previous courses</span>'
    )
    course_next = (
        f'<a class="secondary button-link" href="{esc(course_page_url(course_page + 1))}">Next courses</a>'
        if course_page < course_pages else '<span aria-disabled="true">Next courses</span>'
    )
    management_actions = ""
    if can_manage:
        management_actions = f'''<div class="university-management-actions">
          <details><summary class="secondary button-link">Manage Pathways</summary><div class="university-management-forms"><form method="post" action="/schools/universities/{university_id}/pathways"><input type="hidden" name="csrf" value="{esc(csrf)}"><input type="hidden" name="return_to" value="{esc(directory_url)}"><h3>Add Pathway</h3><label>Display Name<input name="display_name" placeholder="Foundation + Bachelor" required></label><label>Code<input name="pathway_code" placeholder="foundation-bachelor" required></label><label>Final Level<select name="final_course_level"><option>Foundation</option><option>Diploma</option><option>Bachelor</option><option>Master</option></select></label><label>Source URL<input name="source_url" type="url"></label><label>Evidence<textarea name="evidence"></textarea></label><button class="primary">Add Pathway</button></form><form method="post" action="/schools/universities/{university_id}/pathway-segments"><input type="hidden" name="csrf" value="{esc(csrf)}"><input type="hidden" name="return_to" value="{esc(directory_url)}"><h3>Pathway Providers</h3><label>Pathway<select name="pathway_combination_id">{pathway_options}</select></label><label>Order<input type="number" name="segment_order" min="1" required></label><label>Provider<select name="provider_id">{provider_options}</select></label><label>Level<select name="course_level"><option>Foundation</option><option>Diploma</option><option>Bachelor</option><option>Master</option></select></label><button class="primary">Add Segment</button></form></div></details>
          <details><summary class="secondary button-link">Add Course / Program</summary><form class="university-inline-form" method="post" action="/schools/universities/{university_id}/courses"><input type="hidden" name="csrf" value="{esc(csrf)}"><input type="hidden" name="return_to" value="{esc(directory_url)}"><label>Course Name<input name="course_name" required></label><label>Course Level<select name="course_level"><option>Foundation</option><option>Diploma</option><option>Bachelor</option><option>Master</option></select></label><label>Major / Specialisation<input name="specialisation"></label><button class="primary">Add Course / Program</button></form></details>
        </div>'''
    location = " · ".join(part for part in (_value(university, "state"), _value(university, "city")) if part) or "Location not recorded"
    edit_action = f'<a class="primary button-link" href="{esc(edit_url)}">Edit University</a>' if can_manage else ""
    status_class = "ok" if _value(university, "active_status") == "Active" else ""
    return f'''<section class="he-workspace university-detail-page">
      <a class="university-detail-back" href="{esc(directory_url)}">← Back to Universities</a>
      <nav class="university-detail-breadcrumb" aria-label="Breadcrumb">School Management / Universities / {esc(_value(university, "provider_name"))}</nav>
      <header class="he-hero university-detail-hero"><div><span class="eyebrow">UNIVERSITIES</span><h1>{esc(_value(university, "provider_name"))}</h1><p>{esc(_value(university, "short_name") or "Short name not recorded")} · {esc(location)}</p></div><div class="row-actions"><span class="status {status_class}">{esc(_value(university, "active_status"))}</span>{edit_action}</div></header>
      <section class="panel university-overview"><h2>Overview</h2><dl><div><dt>University name</dt><dd>{esc(_value(university, "provider_name"))}</dd></div><div><dt>Short name</dt><dd>{esc(_value(university, "short_name") or "Not recorded")}</dd></div><div><dt>Location</dt><dd>{esc(location)}</dd></div><div><dt>Status</dt><dd>{esc(_value(university, "active_status"))}</dd></div></dl></section>
      <section class="panel"><div class="panel-head"><div><h2>Pathways</h2><p>Foundation Provider · Diploma Provider · Bachelor Provider · Master Provider</p></div></div><div class="he-card-grid">{pathway_cards}</div></section>
      <section class="panel university-courses-section"><div class="panel-head"><div><h2>Courses / Programs</h2><p>{int(course_pagination.get("total") or 0)} records</p></div></div><form class="filter-bar university-course-search" method="get" action="{detail_url}"><input type="hidden" name="return_to" value="{esc(directory_url)}"><label class="wide">Search courses<input name="cq" value="{esc(course_query)}" placeholder="Course, program, level or specialisation"></label><button class="primary">Search</button></form><div class="table-wrap university-course-table"><table><thead><tr><th>Course</th><th>Level</th><th>Specialisation</th><th>Status</th><th>Action</th></tr></thead><tbody>{course_rows}</tbody></table></div><nav class="university-course-pagination" aria-label="Course pages">{course_previous}<strong>Course page {course_page} of {course_pages}</strong>{course_next}</nav></section>
      {management_actions}
    </section>'''


def render_university_edit(*, university, csrf, return_to=""):
    university_id = int(_value(university, "id", 0))
    directory_url = safe_universities_return_path(return_to)
    detail_url = f"/schools/universities/{university_id}?return_to={quote(directory_url, safe='')}"
    return f'''<section class="he-workspace university-edit-page">
      <nav class="university-detail-breadcrumb" aria-label="Breadcrumb">School Management / Universities / {esc(_value(university, "provider_name"))} / Edit</nav>
      <div class="panel-head"><div><span class="eyebrow">UNIVERSITIES</span><h1>Edit University</h1><p>Update the selected record only.</p></div></div>
      <form class="panel university-edit-form" method="post" action="/schools/universities/{university_id}/update" data-unsaved-form>
        <input type="hidden" name="csrf" value="{esc(csrf)}"><input type="hidden" name="return_to" value="{esc(directory_url)}">
        <fieldset><legend>University details</legend><label>University Name<input name="provider_name" value="{esc(_value(university, "provider_name"))}" required></label><label>Short Name<input name="short_name" value="{esc(_value(university, "short_name"))}"></label><label class="wide">Source URL<input name="source_url" type="url" value="{esc(_value(university, "source_url"))}"></label></fieldset>
        <fieldset><legend>Location and status</legend><label>State<input name="state" value="{esc(_value(university, "state"))}"></label><label>City<input name="city" value="{esc(_value(university, "city"))}"></label><label>Status<select name="active_status"><option{' selected' if _value(university, 'active_status') == 'Active' else ''}>Active</option><option{' selected' if _value(university, 'active_status') == 'Inactive' else ''}>Inactive</option></select></label></fieldset>
        <div class="row-actions"><a class="secondary button-link" href="{esc(detail_url)}">Cancel</a><button class="primary" type="submit">Save Changes</button></div>
      </form>
    </section>'''
