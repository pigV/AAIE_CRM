import unittest

from google_drive_sync_views import render_google_drive_workspace, render_preview_items


def item(file_id, decision, status, *, eligible=False):
    return {
        "drive_file_id": file_id,
        "file_name": f"{file_id}.txt",
        "mime_type": "text/plain",
        "modified_time": "2026-08-10T01:00:00Z",
        "crm_matches": [41] if decision != "new" else [],
        "decision": decision,
        "status": status,
        "planned_action": "准备新增" if decision == "new" else "停止写入并核对",
        "warning": "synthetic warning" if not eligible else "",
        "eligible": eligible,
        "default_selected": eligible,
    }


class GoogleDriveSyncViewTests(unittest.TestCase):
    def setUp(self):
        self.status = {
            "connection_code": "connected",
            "connection_label": "已连接",
            "service_code": "paused",
            "service_label": "已暂停",
            "current_plan": "每天 23:00，Australia/Melbourne",
            "last_success_at": "2026-08-01T23:10:00+10:00",
            "last_scan_at": "2026-08-09T09:00:00+10:00",
            "next_run_at": "",
            "last_result": "扫描完成",
            "last_error": "无",
        }
        self.items = [
            item("new", "new", "新增", eligible=True),
            item("changed", "changed", "内容有更新", eligible=True),
            item("current", "current", "已存在，无需更新"),
            item("conflict", "conflict", "疑似重复"),
            item("manual", "manual_review", "需要人工确认"),
            item("unsupported", "unsupported", "无法识别"),
            item("failed", "scan_failed", "扫描失败"),
        ]
        self.preview = {
            "scan_id": "scan-1",
            "confirmation_nonce": "nonce-1",
            "summary": {
                "total": 7,
                "new": 1,
                "changed": 1,
                "existing_duplicate": 2,
                "manual_review": 2,
                "unsupported": 1,
                "errors": 1,
            },
            "items": self.items,
        }

    def render(self, **kwargs):
        return render_google_drive_workspace(
            status=kwargs.get("status", self.status),
            preview=kwargs.get("preview", self.preview),
            report=kwargs.get("report"),
            logs=kwargs.get("logs", []),
            csrf="csrf-safe",
            permissions=kwargs.get("permissions", {"can_control": True, "can_scan": True, "can_confirm": True}),
            scan_job=kwargs.get("scan_job"),
        )

    def test_running_scan_renders_accessible_progress_and_paused_controls(self):
        html = self.render(
            preview=None,
            status={**self.status, "carrier_status": "PAUSED", "policy_status": "PAUSED", "is_enabled": False},
            scan_job={
                "job_id": "scanjob-safe", "state": "running", "phase": "reading",
                "percent": 38, "processed": 326, "total": 853,
                "message": "正在读取文件内容。",
            },
        )

        self.assertIn("data-drive-scan-progress", html)
        self.assertIn('data-status-url="/content/google-drive/scan-jobs/scanjob-safe"', html)
        self.assertIn('role="progressbar"', html)
        self.assertIn('aria-valuenow="38"', html)
        self.assertIn("326 / 853", html)
        self.assertIn("38%", html)
        self.assertIn("文件内容读取", html)
        self.assertIn("通常需要 1–3 分钟", html)
        self.assertRegex(html, r">启动自动更新</button>")
        self.assertRegex(html, r'<button[^>]+disabled[^>]*>启动自动更新</button>')
        self.assertRegex(html, r'<button[^>]+disabled[^>]*>暂停自动更新</button>')
        self.assertIn("aaie-crm-google-drive 当前为 PAUSED", html)

    def test_workspace_contains_status_text_plan_times_and_safe_actions(self):
        html = self.render()
        for text in (
            "Google Drive 服务控制",
            "已连接",
            "已暂停",
            "每天 23:00，Australia/Melbourne",
            "最近成功运行时间",
            "最近一次扫描时间",
            "下次计划运行时间",
            "最近一次运行结果",
            "最近错误摘要",
            "启动自动更新",
            "暂停自动更新",
            "立即检查 Drive",
            "查看同步日志",
            "确认更新 CRM",
        ):
            self.assertIn(text, html)
        self.assertNotIn('action="/content/sync-google-drive-video"', html)
        self.assertNotIn("OAuth token", html)
        self.assertNotIn("refresh token", html.lower())

    def test_running_service_labels_disabled_start_as_already_started(self):
        html = self.render(status={
            **self.status,
            "carrier_status": "ACTIVE",
            "policy_status": "RUNNING",
            "is_enabled": True,
            "service_code": "running",
            "service_label": "运行中",
            "next_run_at": "2026-08-10T23:00:00+10:00",
        })

        self.assertRegex(html, r'<button[^>]+disabled[^>]*>自动更新已启动</button>')
        self.assertRegex(html, r'<button[^>]*>暂停自动更新</button>')

    def test_blocked_scheduler_attempt_is_visible_and_not_called_running(self):
        html = self.render(status={
            **self.status,
            "carrier_status": "ACTIVE",
            "policy_status": "RUNNING",
            "is_enabled": True,
            "service_code": "abnormal",
            "service_label": "异常",
            "scheduler_health_code": "blocked",
            "scheduler_health_label": "最近自动任务被阻止",
            "last_attempt_at": "2026-08-26T23:01:35+10:00",
            "last_attempt_reason": "无法连接 Admin 健康检查",
            "last_expected_run_at": "2026-08-26T23:00:00+10:00",
            "next_run_at": "2026-08-27T23:00:00+10:00",
        })

        self.assertIn("最近自动任务被阻止", html)
        self.assertIn("最近自动任务尝试", html)
        self.assertIn("无法连接 Admin 健康检查", html)
        self.assertIn(
            '自动任务健康：<strong class="drive-status is-error">最近自动任务被阻止</strong>',
            html,
        )
        self.assertNotIn("自动更新已启动", html)

    def test_scheduler_badge_uses_scheduler_health_style_not_service_style(self):
        html = self.render(status={
            **self.status,
            "service_code": "abnormal",
            "service_label": "异常",
            "scheduler_health_code": "pending",
            "scheduler_health_label": "等待首次计划运行",
        })

        self.assertIn(
            '自动任务健康：<strong class="drive-status is-neutral">等待首次计划运行</strong>',
            html,
        )

    def test_all_connection_and_service_states_have_visible_text(self):
        for code, label in (
            ("connected", "已连接"),
            ("authorization_expired", "授权失效"),
            ("connection_error", "连接异常"),
        ):
            status = {**self.status, "connection_code": code, "connection_label": label}
            self.assertIn(label, self.render(status=status))
        for code, label in (("running", "运行中"), ("paused", "已暂停"), ("abnormal", "异常")):
            status = {**self.status, "service_code": code, "service_label": label}
            self.assertIn(label, self.render(status=status))

    def test_preview_has_seven_statuses_metadata_and_safe_checkboxes(self):
        html = render_preview_items(self.items, "csrf-safe", "scan-1", "nonce-1")
        for text in (
            "新增", "内容有更新", "已存在，无需更新", "疑似重复",
            "需要人工确认", "无法识别", "扫描失败", "Drive File ID",
            "文件类型", "最后修改时间", "CRM 当前匹配记录", "判断结果",
            "准备执行的操作", "错误或警告原因",
        ):
            self.assertIn(text, html)
        self.assertRegex(html, r'name="selected_ids" value="conflict"[^>]*disabled')
        self.assertRegex(html, r'name="selected_ids" value="manual"[^>]*disabled')
        self.assertRegex(html, r'name="selected_ids" value="failed"[^>]*disabled')
        self.assertNotRegex(html, r'value="conflict"[^>]*checked')

    def test_summary_report_logs_assets_and_log_privacy(self):
        html = self.render(
            report={
                "created": 1,
                "updated": 2,
                "skipped_duplicate": 3,
                "manual_review": 4,
                "failed": 1,
                "items": [{
                    "drive_file_id": "drive-timeout",
                    "file_name": "Timed out document",
                    "source_key": "gdrive://drive-timeout",
                    "stage": "google_drive_content_read",
                    "exception_type": "TimeoutError",
                    "reason": "The read operation timed out",
                    "failure_at": "2026-08-10T01:00:30Z",
                    "status": "failed",
                }],
            },
            logs=[{
                "started_at": "2026-08-10T01:00:00Z", "ended_at": "2026-08-10T01:01:00Z",
                "trigger": "manual_confirm", "actor_user_id": 7, "scanned": 8,
                "created": 1, "updated": 2, "skipped": 3, "failed": 2,
                "status": "completed_with_errors", "error_summary": "item_failures",
                "failure_stages": ["google_drive_content_read"],
                "report_id": "automatic-timeout-report",
                "failure_items": [{
                    "drive_file_id": "automatic-timeout",
                    "file_name": "Automatic timed out document",
                    "source_key": "gdrive://automatic-timeout",
                    "source_url": "https://drive.invalid/automatic-timeout",
                    "stage": "google_drive_metadata_read",
                    "exception_type": "TimeoutError",
                    "attempts": 2,
                    "reason": "The read operation timed out",
                    "failure_at": "2026-08-10T01:00:45Z",
                    "status": "failed",
                }],
            }],
        )
        for text in (
            "Drive 文件总数", "新增内容数量", "已修改内容数量", "已存在／重复内容数量",
            "需要人工确认数量", "无法处理数量", "错误数量", "成功新增", "成功更新",
            "已跳过重复", "需要人工处理", "失败数量", "任务开始和结束时间",
            "触发方式", "操作用户", "扫描数量", "运行状态", "错误摘要",
            "失败明细", "Timed out document", "gdrive://drive-timeout",
            "Google Drive 内容读取", "TimeoutError", "The read operation timed out",
            "Automatic timed out document", "gdrive://automatic-timeout",
            "https://drive.invalid/automatic-timeout", "automatic-timeout-report", "读取尝试次数",
            "/assets/google-drive-update.css", "/assets/google-drive-update.js",
        ):
            self.assertIn(text, html)
        self.assertIn("完成，但有 2 项失败", html)
        self.assertIn("2 个文件在 Google Drive 读取阶段失败；请查看本次更新报告。", html)
        self.assertNotIn("completed_with_errors", html)
        self.assertNotIn("item_failures", html)

    def test_current_report_and_manual_automatic_logs_never_render_raw_failure_lists(self):
        one = {
            "drive_file_id": "one<&>",
            "file_name": "one<&>.docx",
            "stage": "google_drive_content_read",
            "reason": "timeout <unsafe> authorization=secret-token",
            "source_url": "https://drive.invalid/file?access_token=url-secret",
        }
        html = self.render(
            report={"failed": [one], "items": [one]},
            logs=[
                {
                    "trigger": "manual_confirm",
                    "failed": "[]",
                    "failure_items": [],
                    "status": "completed",
                },
                {
                    "trigger": "automatic",
                    "failed": '[{"file_name":"two.docx","reason":"denied"}]',
                    "status": "completed_with_errors",
                    "error_summary": "item_failures",
                },
            ],
        )

        self.assertIn("失败数量</span><strong>1</strong>", html)
        self.assertIn("新增／更新／跳过／失败</dt><dd>0／0／0／0", html)
        self.assertIn("新增／更新／跳过／失败</dt><dd>0／0／0／1", html)
        self.assertIn("失败明细", html)
        self.assertIn("one&lt;&amp;&gt;.docx", html)
        self.assertIn("timeout &lt;unsafe&gt;", html)
        self.assertIn("two.docx", html)
        self.assertNotIn("<unsafe>", html)
        self.assertNotIn("secret-token", html)
        self.assertNotIn("url-secret", html)
        self.assertNotIn("<strong>[]</strong>", html)
        self.assertNotIn("／[]", html)

    def test_view_only_permissions_do_not_render_mutating_controls(self):
        html = self.render(permissions={"can_control": False, "can_scan": False, "can_confirm": False})
        self.assertNotIn("启动自动更新", html)
        self.assertNotIn("暂停自动更新", html)
        self.assertNotIn("立即检查 Drive", html)
        self.assertNotIn("确认更新 CRM", html)
        self.assertIn("查看同步日志", html)


if __name__ == "__main__":
    unittest.main()
