from __future__ import annotations

from html import escape

from google_drive_sync_reports import normalize_failure_report
from google_drive_sync_service import localized_error_summary, localized_run_status, safe_error_text


def _e(value) -> str:
    return escape(str(value if value is not None else ""), quote=True)


def _safe_e(value) -> str:
    return _e(safe_error_text(value))


def _status_class(code: str) -> str:
    return {
        "connected": "is-ok",
        "running": "is-ok",
        "healthy": "is-ok",
        "paused": "is-paused",
        "authorization_expired": "is-warning",
        "connection_error": "is-error",
        "abnormal": "is-error",
        "blocked": "is-error",
        "failed": "is-error",
        "writer_busy": "is-error",
        "stale": "is-error",
    }.get(str(code or ""), "is-neutral")


def render_preview_items(items, csrf: str, scan_id: str, nonce: str) -> str:
    cards = []
    for item in items or []:
        file_id = _e(item.get("drive_file_id"))
        eligible = bool(item.get("eligible"))
        attributes = "checked" if eligible and item.get("default_selected") else "disabled" if not eligible else ""
        matches = ", ".join(f"CRM #{_e(value)}" for value in (item.get("crm_matches") or [])) or "未匹配"
        cards.append(
            f"""
            <article class="drive-item-card" data-drive-item data-decision="{_e(item.get('decision'))}">
              <div class="drive-item-select">
                <label><input type="checkbox" data-drive-selection name="selected_ids" value="{file_id}" {attributes}> 选择本次更新</label>
                <span class="drive-status {_status_class(item.get('decision'))}">{_e(item.get('status'))}</span>
              </div>
              <h4>{_e(item.get('file_name') or 'Untitled')}</h4>
              <dl class="drive-item-grid">
                <div><dt>Drive File ID</dt><dd class="drive-file-id">{file_id}</dd></div>
                <div><dt>文件类型</dt><dd>{_e(item.get('mime_type') or 'unknown')}</dd></div>
                <div><dt>最后修改时间</dt><dd>{_e(item.get('modified_time') or '未知')}</dd></div>
                <div><dt>CRM 当前匹配记录</dt><dd>{matches}</dd></div>
                <div><dt>判断结果</dt><dd>{_e(item.get('status'))}</dd></div>
                <div><dt>准备执行的操作</dt><dd>{_e(item.get('planned_action'))}</dd></div>
                <div class="drive-item-wide"><dt>错误或警告原因</dt><dd>{_e(item.get('warning') or '无')}</dd></div>
              </dl>
            </article>"""
        )
    if not cards:
        cards.append('<p class="drive-empty">尚无扫描预览。请先点击“立即检查 Drive”。</p>')
    return f"""
      <form class="drive-preview-form" data-drive-confirm action="/content/google-drive/previews/{_e(scan_id)}/confirm" method="post">
        <input type="hidden" name="csrf" value="{_e(csrf)}">
        <input type="hidden" name="confirmation_nonce" value="{_e(nonce)}">
        <div class="drive-selection-tools"><label><input type="checkbox" data-drive-select-all> 选择全部可更新记录</label></div>
        <div class="drive-item-list">{''.join(cards)}</div>
        <button class="primary drive-action" type="submit" data-drive-confirm-button>确认更新 CRM</button>
      </form>"""


def _summary_html(summary: dict) -> str:
    fields = (
        ("Drive 文件总数", "total"),
        ("新增内容数量", "new"),
        ("已修改内容数量", "changed"),
        ("已存在／重复内容数量", "existing_duplicate"),
        ("需要人工确认数量", "manual_review"),
        ("无法处理数量", "unsupported"),
        ("错误数量", "errors"),
    )
    return '<div class="drive-summary-grid">' + "".join(
        f"<div><span>{label}</span><strong>{_e(summary.get(key, 0))}</strong></div>"
        for label, key in fields
    ) + "</div>"


def render_failure_details(items, *, report_id="", heading="失败明细") -> str:
    stage_labels = {
        "google_drive_metadata_read": "Google Drive 文件信息读取",
        "google_drive_content_read": "Google Drive 内容读取",
        "database_write": "CRM 数据库写入",
    }
    failed_items = [item for item in (items or []) if item.get("status") == "failed"]
    if failed_items:
        cards = []
        for item in failed_items:
            stage = str(item.get("stage") or "unknown")
            cards.append(
                f"""
                <article class="drive-log-card drive-failure-card">
                  <dl class="drive-item-grid">
                    <div><dt>文件名</dt><dd>{_safe_e(item.get('file_name') or '未知')}</dd></div>
                    <div><dt>Drive File ID</dt><dd class="drive-file-id">{_safe_e(item.get('drive_file_id'))}</dd></div>
                    <div><dt>Source Key</dt><dd class="drive-file-id">{_safe_e(item.get('source_key'))}</dd></div>
                    <div><dt>Source URL</dt><dd class="drive-file-id">{_safe_e(item.get('source_url') or '未知')}</dd></div>
                    <div><dt>失败阶段</dt><dd>{_safe_e(stage_labels.get(stage, stage))}</dd></div>
                    <div><dt>异常类型</dt><dd>{_safe_e(item.get('exception_type') or 'UnknownError')}</dd></div>
                    <div><dt>读取尝试次数</dt><dd>{_safe_e(item.get('attempts') or 1)}</dd></div>
                    <div><dt>失败时间</dt><dd>{_safe_e(item.get('failure_at') or '未知')}</dd></div>
                    <div class="drive-item-wide"><dt>Exact error</dt><dd>{_safe_e(item.get('reason') or '未知错误')}</dd></div>
                  </dl>
                </article>"""
            )
        report_ref = f'<p><small>Report ID: <code>{_safe_e(report_id)}</code></small></p>' if report_id else ""
        return f'<div class="drive-failure-details"><h4>{_e(heading)}</h4>{report_ref}{"".join(cards)}</div>'
    return ""


def _report_html(report: dict | None) -> str:
    if not report:
        return ""
    fields = (
        ("成功新增", "created"),
        ("成功更新", "updated"),
        ("已跳过重复", "skipped_duplicate"),
        ("需要人工处理", "manual_review"),
        ("失败数量", "failed"),
    )
    failures = normalize_failure_report(report)
    display_report = {**report, "failed": failures["count"]}
    failure_details = render_failure_details(
        failures["items"],
        report_id=report.get("report_id") or report.get("scan_id"),
    )
    control_notice = (
        '<div class="notice warn">已有同步任务正在运行；本次未读取文件，也未写入 CRM。</div>'
        if report.get("status") == "writer_busy"
        else ""
    )
    return f"""
      <section class="panel drive-report" data-drive-result tabindex="-1">
        <h3 data-drive-result-heading tabindex="-1">本次更新报告</h3>
        <div class="drive-summary-grid">{''.join(f'<div><span>{label}</span><strong>{_e(display_report.get(key, 0))}</strong></div>' for label, key in fields)}</div>
        {control_notice}
        {failure_details}
      </section>"""


def _logs_html(logs) -> str:
    trigger_labels = {
        "automatic": "自动",
        "manual_scan": "人工扫描",
        "manual_confirm": "人工确认写入",
    }
    cards = []
    for row in logs or []:
        failures = normalize_failure_report(row)
        display_row = {**row, "failed": failures["count"]}
        failure_details = render_failure_details(
            failures["items"],
            report_id=row.get("report_id"),
            heading="本次日志失败文件详情",
        )
        cards.append(
            f"""
            <article class="drive-log-card">
              <dl class="drive-item-grid">
                <div><dt>任务开始和结束时间</dt><dd>{_e(row.get('started_at'))} — {_e(row.get('ended_at'))}</dd></div>
                <div><dt>触发方式</dt><dd>{_e(trigger_labels.get(row.get('trigger'), row.get('trigger') or '未知'))}</dd></div>
                <div><dt>操作用户</dt><dd>{_e(row.get('actor_user_id') or '系统')}</dd></div>
                <div><dt>扫描数量</dt><dd>{_e(row.get('scanned', 0))}</dd></div>
                <div><dt>新增／更新／跳过／失败</dt><dd>{_e(display_row.get('created', 0))}／{_e(display_row.get('updated', 0))}／{_e(display_row.get('skipped', 0))}／{_e(display_row.get('failed', 0))}</dd></div>
                <div><dt>运行状态</dt><dd>{_e(localized_run_status(display_row) or '未知')}</dd></div>
                <div><dt>错误摘要</dt><dd>{_e(localized_error_summary(display_row) or '无')}</dd></div>
              </dl>
              {failure_details}
            </article>"""
        )
    return "".join(cards) or '<p class="drive-empty">暂无同步日志。</p>'


def render_scan_progress(scan_job: dict | None) -> str:
    if not scan_job:
        return ""
    phase_labels = {
        "queued": "等待开始",
        "connecting": "连接 Google Drive",
        "listing": "读取文件目录",
        "reading": "文件内容读取",
        "deduplicating": "CRM 重复检查",
        "previewing": "生成扫描预览",
        "completed": "扫描完成",
        "failed": "扫描失败",
        "interrupted": "扫描已中断",
    }
    try:
        percent = max(0, min(100, int(scan_job.get("percent") or 0)))
        processed = max(0, int(scan_job.get("processed") or 0))
        total = max(0, int(scan_job.get("total") or 0))
    except (TypeError, ValueError):
        percent, processed, total = 0, 0, 0
    job_id = _e(scan_job.get("job_id"))
    phase = str(scan_job.get("phase") or "queued")
    return f"""
      <section class="panel drive-scan-progress" data-drive-scan-progress
               data-scan-job-id="{job_id}"
               data-scan-state="{_e(scan_job.get('state') or 'queued')}"
               data-scan-phase="{_e(phase)}"
               data-scan-percent="{percent}"
               data-scan-processed="{processed}"
               data-scan-total="{total}"
               data-status-url="/content/google-drive/scan-jobs/{job_id}">
        <div class="drive-scan-progress-head">
          <div><h3>Drive 扫描进度</h3><p>当前阶段：<strong data-drive-progress-phase>{_e(phase_labels.get(phase, phase))}</strong></p></div>
          <strong class="drive-scan-percent" data-drive-progress-percent>{percent}%</strong>
        </div>
        <div class="drive-scan-track" role="progressbar" aria-label="Google Drive 扫描进度"
             aria-valuemin="0" aria-valuemax="100" aria-valuenow="{percent}" data-drive-progress-bar>
          <span style="width:{percent}%"></span>
        </div>
        <div class="drive-scan-progress-meta">
          <span data-drive-progress-count>{processed} / {total}</span>
          <span>通常需要 1–3 分钟，可停留本页查看实时进度。</span>
        </div>
        <p class="drive-scan-message" data-drive-progress-message aria-live="polite">{_e(scan_job.get('message') or '正在准备扫描…')}</p>
      </section>"""


def render_google_drive_workspace(status, preview, report, logs, csrf, permissions, scan_job=None) -> str:
    status = status or {}
    preview = preview or {}
    permissions = permissions or {}
    controls = []
    if permissions.get("can_control"):
        carrier_active = str(status.get("carrier_status") or "").upper() == "ACTIVE"
        service_enabled = bool(status.get("is_enabled"))
        start_disabled = " disabled aria-disabled=\"true\"" if not carrier_active or service_enabled else ""
        pause_disabled = " disabled aria-disabled=\"true\"" if not service_enabled else ""
        start_label = (
            "自动更新已启动"
            if service_enabled and status.get("service_code") == "running"
            else "自动更新已配置（异常）"
            if service_enabled
            else "启动自动更新"
        )
        controls.extend(
            (
                f'<form data-drive-start method="post" action="/content/google-drive/service/start"><input type="hidden" name="csrf" value="{_e(csrf)}"><button class="primary drive-action" type="submit"{start_disabled}>{start_label}</button></form>',
                f'<form data-drive-pause method="post" action="/content/google-drive/service/pause"><input type="hidden" name="csrf" value="{_e(csrf)}"><button class="secondary drive-action" type="submit"{pause_disabled}>暂停自动更新</button></form>',
            )
        )
    if permissions.get("can_scan"):
        scan_disabled = " disabled aria-disabled=\"true\"" if (scan_job or {}).get("state") in {"queued", "running"} else ""
        controls.append(
            f'<form data-drive-scan method="post" action="/content/google-drive/scan"><input type="hidden" name="csrf" value="{_e(csrf)}"><button class="secondary drive-action" type="submit"{scan_disabled}>立即检查 Drive</button></form>'
        )
    controls.append('<button class="secondary drive-action" type="button" data-drive-log-toggle>查看同步日志</button>')
    preview_html = ""
    if preview:
        preview_html = f"""
          <section class="panel drive-preview" data-drive-preview>
            <div class="panel-head"><div><h3 data-drive-result-heading tabindex="-1">扫描预览</h3><p>扫描结果不会写入 CRM；请核对后选择本次更新记录。</p></div></div>
            {_summary_html(preview.get('summary') or {})}
            {render_preview_items(preview.get('items') or [], csrf, preview.get('scan_id', ''), preview.get('confirmation_nonce', '')) if permissions.get('can_confirm') else '<p class="drive-empty">你只有查看权限，不能确认写入。</p>'}
          </section>"""
    return f"""
      <link rel="stylesheet" href="/assets/google-drive-update.css?v=20260810-progress1">
      <section class="content-workspace google-drive-update-page">
        <div class="content-workspace-title"><div><span class="eyebrow">Google Drive Sync</span><h2>Google Drive 更新</h2><p>先扫描预览，确认后才写入 CRM。</p></div></div>
        <section class="panel drive-service-control">
          <div class="panel-head"><div><h2>Google Drive 服务控制</h2><p>自动任务和人工确认共用同一套重复保护。</p></div></div>
          <div class="drive-status-row">
            <span>Google Drive：<strong class="drive-status {_status_class(status.get('connection_code'))}">{_e(status.get('connection_label') or '连接异常')}</strong></span>
            <span>自动更新服务：<strong class="drive-status {_status_class(status.get('service_code'))}">{_e(status.get('service_label') or '异常')}</strong></span>
            <span>自动任务健康：<strong class="drive-status {_status_class(status.get('scheduler_health_code'))}">{_e(status.get('scheduler_health_label') or '暂无运行证据')}</strong></span>
          </div>
          <dl class="drive-service-grid">
            <div><dt>当前计划</dt><dd>{_e(status.get('current_plan') or '每天 23:00，Australia/Melbourne')}</dd></div>
            <div><dt>最近成功运行时间（仅自动）</dt><dd>{_e(status.get('last_success_at') or '暂无')}</dd></div>
            <div><dt>最近一次扫描时间（仅自动）</dt><dd>{_e(status.get('last_scan_at') or '暂无')}</dd></div>
            <div><dt>最近自动任务尝试</dt><dd>{_e(status.get('last_attempt_at') or '暂无')}</dd></div>
            <div><dt>最近应运行时间</dt><dd>{_e(status.get('last_expected_run_at') or '暂无')}</dd></div>
            <div><dt>下次计划运行时间</dt><dd>{_e(status.get('next_run_at') or '暂无')}</dd></div>
            <div><dt>最近一次运行结果</dt><dd>{_e(status.get('last_result') or '暂无')}</dd></div>
            <div><dt>最近错误摘要</dt><dd>{_e(status.get('last_error') or '无')}</dd></div>
            <div><dt>自动任务诊断</dt><dd>{_e(status.get('last_attempt_reason') or '暂无')}</dd></div>
          </dl>
          <div class="drive-control-actions">{''.join(controls)}</div>
          {'<p class="drive-carrier-note">外部任务 aaie-crm-google-drive 当前为 PAUSED；保持暂停，不会由此页面自动启用。</p>' if str(status.get('carrier_status') or '').upper() == 'PAUSED' else ''}
        </section>
        {render_scan_progress(scan_job)}
        <div data-drive-result-container>{_report_html(report)}</div>
        {preview_html}
        <section class="panel drive-logs" data-drive-logs hidden>
          <div class="panel-head"><div><h3>同步日志</h3><p>仅显示运行统计与脱敏错误摘要。</p></div></div>
          {_logs_html(logs)}
        </section>
      </section>
      <script src="/assets/google-drive-update.js?v=20260810-progress1" defer></script>"""
