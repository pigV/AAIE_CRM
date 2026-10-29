import html
import hashlib
import json
from pathlib import Path
from urllib.parse import quote

from fields import SECTIONS, display_date_value
from agent_reach_permissions import can_access_agent_reach
from revenue.access_policy import can_access_revenue


ASSET_DIR = Path(__file__).resolve().parent / "assets"


def asset_content_version(asset_dir=ASSET_DIR):
    digest = hashlib.sha256()
    for path in sorted(Path(asset_dir).rglob("*")):
        if not path.is_file() or path.name.startswith("._") or path.suffix not in {".css", ".js"}:
            continue
        digest.update(path.relative_to(asset_dir).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:16]


ASSET_VERSION = asset_content_version()


def esc(value):
    return html.escape(str(value if value is not None else ""), quote=True)


def json_script(value):
    return json.dumps(value, ensure_ascii=False).replace("</", "<\\/")


def notice(kind, text):
    if not text:
        return ""
    return f'<div class="notice {esc(kind)}">{esc(text)}</div>'


def role_display_label(value):
    return {
        "Admin": "管理员", "Manager": "经理", "Content Staff": "内容人员",
        "Consultant": "顾问", "Visa Officer": "Visa 专员", "Finance": "财务",
        "View Only": "只读",
    }.get(value, value)


ICONS = {
    "dashboard": '<path d="M3 13h8V3H3v10Z"/><path d="M13 21h8V11h-8v10Z"/><path d="M13 3v6h8V3h-8Z"/><path d="M3 21h8v-6H3v6Z"/>',
    "students": '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
    "contracts": '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z"/><path d="M14 2v6h6"/><path d="M8 13h8"/><path d="M8 17h5"/>',
    "templates": '<path d="M4 4h16v4H4z"/><path d="M4 12h7v8H4z"/><path d="M15 12h5v8h-5z"/><path d="M7 6h10"/><path d="M6.5 15h2"/><path d="M17 15h1"/>',
    "schools": '<path d="M3 21h18"/><path d="M5 21V8l7-5 7 5v13"/><path d="M9 21v-6h6v6"/><path d="M9 10h.01"/><path d="M12 10h.01"/><path d="M15 10h.01"/>',
    "school_plan": '<path d="M4 3h10l6 6v12H4z"/><path d="M14 3v6h6"/><path d="M8 13h8"/><path d="M8 17h5"/><path d="M8 9h2"/>',
    "book": '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M4 4.5A2.5 2.5 0 0 1 6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5z"/><path d="M8 6h8"/><path d="M8 10h6"/>',
    "link": '<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>',
    "bell": '<path d="M18 8a6 6 0 1 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/>',
    "users": '<path d="M18 21a8 8 0 0 0-16 0"/><circle cx="10" cy="8" r="5"/><path d="M22 20c0-3-1.5-5.5-4-6.7"/><path d="M17 3.3a4 4 0 0 1 0 7.4"/>',
    "audit": '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z"/><path d="M14 2v6h6"/><path d="M9 15h6"/><path d="M9 11h2"/>',
    "revenue": '<circle cx="12" cy="12" r="9"/><path d="M15.5 8.5c-.8-.7-2-1-3.5-1-2 0-3.5 1-3.5 2.5s1.5 2.2 3.5 2.5 3.5 1 3.5 2.5-1.5 2.5-3.5 2.5c-1.5 0-2.8-.4-3.7-1.2"/><path d="M12 5.5v13"/>',
    "files": '<path d="M3 6.5A2.5 2.5 0 0 1 5.5 4H10l2 2h6.5A2.5 2.5 0 0 1 21 8.5v7A2.5 2.5 0 0 1 18.5 18h-13A2.5 2.5 0 0 1 3 15.5v-9Z"/><path d="M3 9h18"/>',
    "settings": '<path d="M12 15.5A3.5 3.5 0 1 0 12 8a3.5 3.5 0 0 0 0 7.5Z"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06A1.65 1.65 0 0 0 15 19.4a1.65 1.65 0 0 0-1 .6 1.65 1.65 0 0 0-.4 1v.17a2 2 0 1 1-4 0V21a1.65 1.65 0 0 0-.4-1 1.65 1.65 0 0 0-1-.6 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.6 15a1.65 1.65 0 0 0-.6-1 1.65 1.65 0 0 0-1-.4H2.83a2 2 0 1 1 0-4H3a1.65 1.65 0 0 0 1-.4 1.65 1.65 0 0 0 .6-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06A2 2 0 1 1 7.04 3.5l.06.06A1.65 1.65 0 0 0 9 4.6c.36-.08.7-.28 1-.6.25-.3.4-.64.4-1V2.83a2 2 0 1 1 4 0V3c0 .36.15.7.4 1 .3.32.64.52 1 .6a1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9c.08.36.28.7.6 1 .3.25.64.4 1 .4h.17a2 2 0 1 1 0 4H21c-.36 0-.7.15-1 .4-.32.3-.52.64-.6 1Z"/>',
    "sun": '<circle cx="12" cy="12" r="4"/><path d="M12 2v2"/><path d="M12 20v2"/><path d="m4.93 4.93 1.41 1.41"/><path d="m17.66 17.66 1.41 1.41"/><path d="M2 12h2"/><path d="M20 12h2"/><path d="m6.34 17.66-1.41 1.41"/><path d="m19.07 4.93-1.41 1.41"/>',
    "moon": '<path d="M20.99 13.36A8 8 0 1 1 10.64 3.01 6.5 6.5 0 0 0 20.99 13.36Z"/>',
    "key": '<path d="M21 2l-2 2"/><path d="M15.5 7.5 19 4"/><circle cx="7.5" cy="16.5" r="5.5"/><path d="M10.5 13.5 21 3"/>',
    "logout": '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="M16 17l5-5-5-5"/><path d="M21 12H9"/>',
    "search": '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/>',
    "trash": '<path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="m19 6-1 14H6L5 6"/><path d="M10 11v5"/><path d="M14 11v5"/>',
}


def icon(name):
    return f'<svg class="icon" viewBox="0 0 24 24" aria-hidden="true">{ICONS.get(name, "")}</svg>'


def nav_link(active, key, href, label, icon_name):
    state = "active" if active == key else ""
    return f'<a class="{state}" href="{href}" data-nav-key="{esc(key)}">{icon(icon_name)}<span>{esc(label)}</span></a>'


def _user_modules(user):
    if not user:
        return set()
    content_allowed = str(user.get("username") or "").strip().lower() in {"admin", "sonnyz"}
    if user.get("role_name") == "Admin":
        modules = {"dashboard", "students", "parent_form", "reminders", "school_library", "school_plan_reports", "higher_education", "visa", "content_center", "contract_management_center", "template_management", "revenue_commission", "data_update_centre", "file_manager", "user_management", "audit_logs", "system_settings", "backup_restore", "safebox_centre"}
        if not content_allowed:
            modules.discard("content_center")
        return modules
    try:
        modules = set(json.loads(user.get("module_permissions_json") or "[]"))
    except Exception:
        modules = set()
    if can_access_revenue(user):
        modules.add("revenue_commission")
    else:
        modules.discard("revenue_commission")
    if content_allowed:
        modules.add("content_center")
    else:
        modules.discard("content_center")
    return modules


def _nav_items(active, user):
    modules = _user_modules(user)
    if can_access_agent_reach(user):
        modules.add("agent_reach")
    else:
        modules.discard("agent_reach")
    groups = [
        ("主菜单", [
            ("dashboard", "dashboard", "/", "首页", "dashboard"),
            ("students", "students", "/students", "学生管理", "students"),
            ("parent_form", "links", "/parent-links", "家长表单", "link"),
            ("reminders", "reminders", "/reminders", "提醒中心", "bell"),
            ("school_library", "schools", "/schools", "学校管理", "schools"),
            ("school_plan_reports", "school_plan_reports", "/school-plan-reports", "学校方案报告", "school_plan"),
            ("visa", "visa_management", "/visa-management", "Visa 管理", "files"),
        ]),
        ("内容管理", [
            ("content_center", "content", "/content", "内容管理中心", "templates"),
            ("agent_reach", "agent_reach", "/agent-reach", "Agent Reach", "search"),
        ]),
        ("管理", [
            ("contract_management_center", "contracts", "/contracts", "合同管理", "contracts"),
            ("template_management", "templates", "/templates", "模板管理", "templates"),
            ("revenue_commission", "revenue", "/revenue", "收入与佣金", "revenue"),
            ("data_update_centre", "data_update_centre", "/data-updates", "数据更新中心", "settings"),
            ("file_manager", "file_manager", "/file-manager", "文件管理", "files"),
        ]),
        ("系统", [
            ("system_settings", "settings", "/settings", "系统管理和设置", "settings"),
            ("backup_restore", "backup_restore", "/settings/backup-restore", "备份与恢复", "settings"),
            ("user_management", "users", "/users", "用户管理", "users"),
            ("audit_logs", "audit", "/audit-logs", "操作日志", "audit"),
        ]),
    ]
    html = []
    for group_name, items in groups:
        links = [
            nav_link(active, key, href, label, icon_name)
            for module_key, key, href, label, icon_name in items
            if module_key in modules
        ]
        if links:
            html.append(f'<div class="nav-group"><div class="nav-group-title">{esc(group_name)}</div>{"".join(links)}</div>')
    return "".join(html)


def page(title, body, user=None, csrf="", active="", nas=None, message=None, message_kind="success", asset_version=""):
    asset_version = asset_version or ASSET_VERSION
    if user:
        mode = (nas or {}).get("mode", "test").upper()
        mode_label = {"TEST": "测试模式", "PRODUCTION": "生产模式",
                      "STAGING": "预发布模式", "PREPROD": "预发布模式"}.get(mode, f"{mode} MODE")
        nas_ready = bool((nas or {}).get("ready"))
        nas_error_code = str(
            (nas or {}).get("access_error_code")
            or (nas or {}).get("error_code")
            or ""
        )
        nas_access_recovery_required = (
            not nas_ready and nas_error_code in {"read_denied", "write_denied"}
        )
        if nas_ready:
            nas_label = "NAS 已连接"
        elif nas_access_recovery_required:
            nas_label = "NAS 访问待恢复"
        else:
            nas_label = "NAS 未连接"
        nas_class = "ok" if nas_ready else "warn"
        nav = f"""
        <aside class="sidebar" id="app-sidebar">
          <div class="brand"><span class="brand-mark">A</span><div class="brand-copy"><strong>AAIE CRM</strong><small>学生事务管理</small></div></div>
          <nav>
            {_nav_items(active, user)}
          </nav>
          <div class="sidebar-bottom">
            <div class="user-chip"><strong>{esc(user['display_name'])}</strong><small>{esc(role_display_label(user['role_name']))}</small></div>
            <a class="ghost full sidebar-account-link" href="/account/password">{icon("key")}<span>修改密码</span></a>
            <form method="post" action="/logout"><input type="hidden" name="csrf" value="{esc(csrf)}"><button class="ghost full">{icon("logout")}<span>退出登录</span></button></form>
          </div>
        </aside>"""
        nas_banner = ""
        if nas and not nas.get("ready"):
            if nas_access_recovery_required:
                nas_banner = '<div class="nas-warning">NAS 已挂载，但当前服务需要恢复访问。文件操作已锁定。</div>'
            else:
                nas_banner = '<div class="nas-warning">学生文件存储未连接或当前模式未启用。文件操作已锁定。</div>'
        live_status = """
        <div class="top-live-status" data-topbar-status>
          <div class="top-health-badge" data-topbar-health>
            <span data-topbar-health-symbol>...</span><strong data-topbar-health-text>检查中</strong>
          </div>
          <div class="top-online-users" data-topbar-online-users title="最近 5 分钟有活动的用户">
            <span>在线用户</span><strong data-topbar-online-count>...</strong><small data-topbar-online-names>正在刷新</small>
          </div>
        </div>"""
        mobile_menu = f'<button type="button" class="ghost mobile-menu-button" data-sidebar-toggle aria-controls="app-sidebar" aria-expanded="false">{icon("settings")}<span>菜单</span></button>'
        shell = f'<div class="app-shell"><div class="sidebar-backdrop" data-sidebar-close></div>{nav}<main class="main">{nas_banner}<header class="topbar">{mobile_menu}<div class="topbar-title"><span>AAIE 学生管理 CRM</span><h1>{esc(title)}</h1></div><div class="top-actions"><span class="top-user"><strong>{esc(user["display_name"])}</strong><small>{esc(role_display_label(user["role_name"]))}</small></span><a class="ghost desktop-mobile-switch" href="/view-mode/mobile?return_to=%2F">移动版</a><a class="ghost top-account-link" href="/account/password">{icon("key")}<span>修改密码</span></a><div class="mode-badge">{esc(mode_label)}</div>{live_status}<div class="nas-badge {nas_class}">{esc(nas_label)}</div><form method="post" action="/logout"><input type="hidden" name="csrf" value="{esc(csrf)}"><button class="ghost top-logout">{icon("logout")}<span>退出</span></button></form></div></header>{notice(message_kind, message)}{body}</main></div>'
    else:
        shell = f'<main class="public-shell">{notice(message_kind, message)}{body}</main>'
    premium_css = f'<link rel="stylesheet" href="/assets/ui-premium-v2.css?v={esc(asset_version)}">' if user else ""
    body_class = ' class="ui-premium-v2"' if user else ""
    parent_form_css = (
        f'<link rel="stylesheet" href="/assets/parent-form.css?v={esc(asset_version)}">'
        if "data-public-parent-form-ui" in body
        else ""
    )
    parent_form_script = (
        f'<script src="/assets/parent-form.js?v={esc(asset_version)}"></script>'
        if "data-public-parent-form" in body
        else ""
    )
    school_plan_picker_css = (
        f'<link rel="stylesheet" href="/assets/school-plan-picker.css?v={esc(asset_version)}">'
        if "data-school-plan-picker" in body
        else ""
    )
    school_plan_picker_script = (
        f'<script src="/assets/school-plan-picker.js?v={esc(asset_version)}"></script>'
        if "data-school-plan-picker" in body
        else ""
    )
    school_plan_workspace_css = (
        f'<link rel="stylesheet" href="/assets/school-plan-report-workspace.css?v={esc(asset_version)}">'
        if "data-school-plan-report-workspace" in body
        else ""
    )
    school_plan_workspace_script = (
        f'<script src="/assets/school-plan-report-workspace.js?v={esc(asset_version)}"></script>'
        if "data-school-plan-report-workspace" in body
        else ""
    )
    higher_education_css = (
        f'<link rel="stylesheet" href="/assets/higher-education.css?v={esc(asset_version)}">'
        if "data-higher-education-workspace" in body
        else ""
    )
    higher_education_script = (
        f'<script src="/assets/higher-education.js?v={esc(asset_version)}"></script>'
        if "data-higher-education-workspace" in body
        else ""
    )
    return f"""<!doctype html>
<html lang="zh-CN" data-theme="light"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} | AAIE CRM</title><link rel="stylesheet" href="/assets/style.css?v={esc(asset_version)}">{premium_css}{parent_form_css}{school_plan_picker_css}{school_plan_workspace_css}{higher_education_css}</head>
<body{body_class}>{shell}<script src="/assets/app.js?v={esc(asset_version)}"></script>{parent_form_script}{school_plan_picker_script}{school_plan_workspace_script}{higher_education_script}</body></html>"""


def field_html(name, label, kind, required, options, value="", invalid=False):
    req = " required" if required else ""
    star = '<span class="required">(*)</span>' if required else ""
    attrs = f'name="{esc(name)}" id="{esc(name)}"{req}'
    visible_stream = (
        "higher_education" if name in {"higher_education_course_type", "higher_education_package_combination"}
        else "school_education" if name == "applying_year_level" else ""
    )
    visibility = f' data-visible-for-stream="{visible_stream}"' if visible_stream else ""
    visible_course_type = "Package" if name == "higher_education_package_combination" else ""
    if visible_course_type:
        visibility += f' data-visible-for-course-type="{visible_course_type}"'
    conditional_required = name in {"higher_education_course_type", "higher_education_package_combination", "applying_year_level"}
    conditional = (
        f' data-field="{esc(name)}" data-kind="{esc(kind)}"'
        f' data-required="{"1" if required or conditional_required else "0"}"{visibility}'
    )
    if kind == "textarea":
        control = f'<textarea {attrs} rows="3">{esc(value)}</textarea>'
    elif kind == "select":
        choices = ['<option value="">请选择</option>']
        for raw in options or []:
            val, _, text = raw.partition("|")
            text = text or val
            selected = " selected" if str(value) == val else ""
            choices.append(f'<option value="{esc(val)}"{selected}>{esc(text)}</option>')
        control = f'<select {attrs}>{"".join(choices)}</select>'
    elif kind in ("radio", "checkbox"):
        values = value if isinstance(value, list) else [value]
        items = []
        for raw in options or []:
            val, _, text = raw.partition("|")
            text = text or val
            checked = " checked" if val in values else ""
            item_req = req if kind == "radio" and not items else ""
            items.append(
                f'<label class="choice"><input type="{kind}" name="{esc(name)}" value="{esc(val)}"{checked}{item_req}><span>{esc(text)}</span></label>'
            )
        control = f'<div class="choices">{"".join(items)}</div>'
    elif kind == "date":
        control = (
            f'<input type="text" {attrs} value="{esc(display_date_value(value))}" '
            f'placeholder="dd/mm/yyyy" pattern="\\d{{2}}/\\d{{2}}/\\d{{4}}" '
            f'inputmode="numeric" maxlength="10" data-date-input '
            f'title="请按 dd/mm/yyyy 格式填写有效日期，例如 31/01/1980" autocomplete="off">'
        )
    else:
        control = f'<input type="{kind}" {attrs} value="{esc(value)}" autocomplete="off">'
    if invalid and kind == "date":
        error = '<div class="field-error">请按 dd/mm/yyyy 格式填写有效日期</div>'
    else:
        error = '<div class="field-error">请填写此必填项</div>' if invalid else ""
    return f'<div class="field {"invalid-field" if invalid else ""}"{conditional}><label for="{esc(name)}">{esc(label)}{star}</label>{control}{error}</div>'


def parent_form_public_shell(content, *, view, status_label, expires_at=""):
    expiry = f'<span class="parent-ui-expiry">{esc(expires_at)}</span>' if expires_at else ""
    return f"""
    <div class="public-parent-form-ui" data-public-parent-form-ui data-parent-form-view="{esc(view)}">
      <header class="parent-ui-topbar">
        <div class="parent-ui-brand"><span class="parent-ui-logo" aria-hidden="true">A</span><div><strong>AAIE 学生信息表</strong><small>Secure parent portal</small></div></div>
        <div class="parent-ui-link-state"><span>{esc(status_label)}</span>{expiry}</div>
      </header>
      <main class="parent-ui-main">{content}</main>
      <footer class="parent-ui-footer">AAIE · 安全家长资料入口</footer>
    </div>"""


def parent_form_login(token, expires_at, *, oauth_enabled):
    if oauth_enabled:
        auth_control = f"""
        <a class="parent-ui-wechat-action" href="/parent-form/{quote(token)}/wechat-login">
          <span class="parent-ui-wechat-symbol" aria-hidden="true">●</span>
          <span>微信验证并继续</span>
        </a>"""
        mode_note = "首次验证后，此链接只允许同一微信继续访问。"
    else:
        auth_control = f"""
        <form class="parent-ui-local-auth" method="post" action="/parent-form/{quote(token)}/wechat-login">
          <label>微信昵称<input name="wechat_nickname" placeholder="请输入微信昵称" required></label>
          <label>测试识别码<input name="wechat_openid" placeholder="本地测试可填写微信备注" required><small>正式环境由微信自动完成，家长无需填写。</small></label>
          <button class="parent-ui-wechat-action" type="submit">微信验证并继续</button>
        </form>"""
        mode_note = "当前为本地测试模式；正式家长页面使用微信服务号授权。"
    content = f"""
    <section class="parent-ui-login-card">
      <div class="parent-ui-secure-hero">
        <span class="parent-ui-eyebrow">SECURE ACCESS</span>
        <h1>验证微信身份<br>继续填写资料</h1>
        <p>该链接仅用于本次学生资料收集。验证后可在有效期内安全保存并继续填写。</p>
      </div>
      <div class="parent-ui-trust-chips" aria-label="表单安全说明">
        <span>7 天有效</span><span>加密保存</span><span>独立绑定</span>
      </div>
      {auth_control}
      <p class="parent-ui-privacy-note">{esc(mode_note)} 正式提交后资料将锁定，不能再次修改。</p>
    </section>"""
    return parent_form_public_shell(
        content,
        view="login",
        status_label="安全验证",
        expires_at=expires_at,
    )


def parent_form_result(title, message, *, tone="warning", details=()):
    tone = tone if tone in {"success", "warning", "danger"} else "warning"
    symbol = "✓" if tone == "success" else "!"
    rows = "".join(
        f'<div><span>{esc(label)}</span><strong>{esc(value)}</strong></div>'
        for label, value in details
    )
    summary = f'<div class="parent-ui-result-details">{rows}</div>' if rows else ""
    content = f"""
    <section class="parent-ui-result-card" data-parent-form-tone="{tone}">
      <div class="parent-ui-result-icon" aria-hidden="true">{symbol}</div>
      <h1>{esc(title)}</h1>
      <p>{esc(message)}</p>
      {summary}
      <p class="parent-ui-privacy-note">如需协助，请联系 AAIE 顾问。</p>
    </section>"""
    return parent_form_public_shell(
        content,
        view="result",
        status_label="提交结果" if tone == "success" else "安全提示",
    )


def parent_form(
    token,
    expires_at,
    errors=None,
    values=None,
    error_names=None,
    action=None,
    revision=0,
    last_saved_at="",
):
    values = values or {}
    error_names = set(error_names or [])
    sections = []
    for index, (_, title, fields) in enumerate(SECTIONS, 1):
        controls = "".join(field_html(*field, values.get(field[0], ""), invalid=field[0] in error_names) for field in fields)
        sections.append(
            f'<section class="form-section parent-ui-section-card"><div class="section-number">{index:02}</div><div class="section-content"><h2>{esc(title)}</h2><div class="form-grid">{controls}</div></div></section>'
        )
    error_box = notice("error", "请检查必填项目：" + "、".join(errors)) if errors else ""
    declaration_invalid = "declaration" in error_names
    declaration_error = '<div class="field-error">请先确认信息真实完整</div>' if declaration_invalid else ""
    draft_url = f"/api/parent-form/{quote(token)}/draft"
    initial_save_status = "已恢复上次保存的草稿" if last_saved_at else "尚未保存"
    form_content = f"""
    <form class="parent-form parent-ui-form" method="post" action="{esc(action or f'/api/parent-form/{quote(token)}/submit')}" data-public-parent-form
      data-parent-form-draft-url="{esc(draft_url)}" data-parent-form-revision="{esc(revision)}" data-parent-form-last-saved-at="{esc(last_saved_at)}" novalidate>
      {error_box}
      <div class="parent-form-status notice error" data-parent-form-status role="alert" aria-live="assertive" hidden></div>
      <div class="intro-band parent-ui-form-hero"><div><span class="parent-ui-eyebrow">STUDENT INFORMATION</span><h1>学生信息表</h1><p>请按护照和真实情况填写。所有带 * 的项目必须填写。</p></div><span class="parent-ui-expiry-chip">有效期至 {esc(expires_at[:10])}</span></div>
      <div class="parent-progress"><span class="active">1. 填写信息</span><span>2. 检查确认</span><span>3. 提交完成</span></div>
      {"".join(sections)}
      <div class="form-submit parent-ui-submit-card">
        <div class="declaration-field {"invalid-field" if declaration_invalid else ""}" data-field="declaration" data-kind="checkbox" data-required="1">
          <label class="consent"><input type="checkbox" name="declaration" value="yes"> 我确认以上信息真实、完整，并同意 AAIE 为留学申请及学生服务目的保存和使用这些资料。</label>
          {declaration_error}
        </div>
        <button class="primary large" type="submit" data-parent-form-submit>确认并提交</button>
      </div>
      <div class="parent-draft-dock" data-parent-draft-dock>
        <span class="parent-draft-state" data-parent-form-save-status aria-live="polite">{esc(initial_save_status)}</span>
        <button class="secondary" type="button" data-parent-form-save>保存草稿</button>
      </div>
    </form>"""
    return parent_form_public_shell(
        form_content,
        view="form",
        status_label="安全连接",
        expires_at=expires_at[:10],
    )


def internal_student_form(action, csrf, errors=None, values=None, error_names=None):
    values = values or {}
    error_names = set(error_names or [])
    sections = []
    for index, (_, title, fields) in enumerate(SECTIONS, 1):
        controls = "".join(field_html(*field, values.get(field[0], ""), invalid=field[0] in error_names) for field in fields)
        sections.append(
            f'<section class="form-section"><div class="section-number">{index:02}</div><div class="section-content"><h2>{esc(title)}</h2><div class="form-grid">{controls}</div></div></section>'
        )
    error_box = notice("error", "请检查必填项目：" + "、".join(errors)) if errors else ""
    return f"""
    <form class="parent-form internal-form" method="post" action="{esc(action)}">
      <input type="hidden" name="csrf" value="{esc(csrf)}">
      {error_box}
      <div class="intro-band internal-band"><h1>手动新建学生</h1><p>后台直接填写学生资料。提交后会自动生成 Student ID，并在 CRM_TEST 中建立学生文件夹。</p></div>
      {"".join(sections)}
      <div class="form-submit"><a class="ghost button-link" href="/students">取消</a><button class="primary large" type="submit">创建学生</button></div>
    </form>"""
