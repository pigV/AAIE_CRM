import unittest


class LifecycleWorkspaceTerminologyTests(unittest.TestCase):
    def test_business_labels_never_expose_known_internal_values(self):
        from lifecycle_workspace_ui import business_label

        expected = {
            "materials_preparation": "材料准备",
            "application_in_progress": "学校申请中",
            "accepted": "已接受录取",
            "withdrawn": "已撤回",
            "rejected": "未获录取",
            "overview": "概览",
            "history": "历史记录",
            "offer": "录取通知",
            "coe": "入学确认",
            "oshc": "学生保险",
        }
        for raw, label in expected.items():
            with self.subTest(raw=raw):
                self.assertEqual(label, business_label(raw))
                self.assertNotEqual(raw, business_label(raw))

    def test_unknown_internal_value_uses_safe_chinese_fallback(self):
        from lifecycle_workspace_ui import business_label

        self.assertEqual("待确认", business_label("future_internal_state"))
        self.assertEqual("需要人工核对", business_label("future_internal_state", fallback="需要人工核对"))


class LifecycleWorkspaceRendererTests(unittest.TestCase):
    def test_shared_renderers_escape_values_and_expose_semantic_contract(self):
        from lifecycle_workspace_ui import (
            WorkspaceStep,
            WorkspaceTab,
            render_empty_state,
            render_lifecycle_stepper,
            render_status_badge,
            render_workspace_header,
            render_workspace_tabs,
        )

        badge = render_status_badge("申请中<script>", "info")
        self.assertIn("申请中&lt;script&gt;", badge)
        self.assertIn('class="lifecycle-status-badge lifecycle-status-badge--info"', badge)

        header = render_workspace_header(
            eyebrow="转学办理",
            title="测试学生 <一>",
            status_label="学校申请中",
            facts=(("转学年份", "2027"), ("转学学期", "第一学期")),
            next_action_html='<a class="primary" href="/next">继续办理</a>',
        )
        self.assertIn('class="lifecycle-workspace__header"', header)
        self.assertIn("测试学生 &lt;一&gt;", header)
        self.assertIn('aria-label="核心信息"', header)
        self.assertIn('class="lifecycle-next-action"', header)
        self.assertIn('href="/next"', header)

        stepper = render_lifecycle_stepper(
            steps=(
                WorkspaceStep("documents", "材料准备"),
                WorkspaceStep("application", "学校申请"),
                WorkspaceStep("offer", "录取通知"),
            ),
            current_key="application",
        )
        self.assertIn('aria-label="办理进度"', stepper)
        self.assertEqual(1, stepper.count('aria-current="step"'))
        self.assertIn('data-step-state="complete"', stepper)
        self.assertIn('data-step-state="current"', stepper)
        self.assertIn('data-step-state="upcoming"', stepper)

        tabs = render_workspace_tabs(
            tabs=(
                WorkspaceTab("overview", "概览", "<p>概览内容</p>"),
                WorkspaceTab("history", "历史记录", "<p>历史内容</p>", panel_id="existing-history-anchor"),
            ),
            namespace="progression",
            legacy_namespace="transfer-v2",
        )
        self.assertIn('role="tablist"', tabs)
        self.assertIn('data-lifecycle-tab="overview"', tabs)
        self.assertIn('data-lifecycle-panel="history"', tabs)
        self.assertIn('data-transfer-v2-tab="overview"', tabs)
        self.assertIn('data-transfer-v2-panel="history"', tabs)
        self.assertIn('id="existing-history-anchor"', tabs)
        self.assertIn('id="progression-tab-overview"', tabs)
        self.assertIn('aria-controls="existing-history-anchor"', tabs)
        self.assertEqual(1, tabs.count('aria-selected="true"'))
        self.assertRegex(tabs, r'data-lifecycle-panel="history"[^>]* hidden')

        empty = render_empty_state("暂无记录", "后续记录将在这里显示。")
        self.assertIn('class="lifecycle-empty-state"', empty)
        self.assertIn("暂无记录", empty)
        self.assertIn("后续记录将在这里显示。", empty)


if __name__ == "__main__":
    unittest.main()
