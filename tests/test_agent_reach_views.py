import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

class WorkspaceTests(unittest.TestCase):
    def render(self, csrf='safe'):
        from agent_reach_views import render_workspace
        return render_workspace(csrf)

    def test_native_chinese_workspace(self):
        text = self.render()
        for value in ['搜索', '趋势', '运行搜索', '采集趋势', '暂未开放', 'panel', 'table-wrap', 'aria-live="polite"']:
            self.assertIn(value, text)
        for forbidden in ['iframe', 'localhost', '8790', 'Bridge', 'OpenCLI', 'http://']:
            self.assertNotIn(forbidden, text)

    def test_csrf_escaped(self):
        self.assertIn('&quot;&gt;&lt;script&gt;', self.render('\"><script>'))

    def test_deep_has_no_action(self):
        text = self.render()
        deep = text.split('id="ar-deep"',1)[1]
        self.assertIn('disabled', deep)
        self.assertNotIn('<form', deep)
        self.assertNotIn('data-ar-submit', deep)

    def test_browser_has_no_unsafe_rendering_or_polling(self):
        js = (ROOT/'assets/agent-reach.js').read_text()
        for bad in ['innerHTML', 'setInterval', 'setTimeout', 'console.', 'window.open', 'http://', 'https://']:
            self.assertNotIn(bad, js)
        self.assertIn('textContent', js)
        self.assertIn('redirect: "error"', js)
        self.assertIn('pending.has(source)', js)
        self.assertIn('X-CSRF-Token', js)

    def test_uncertain_submit_requires_read_before_next_submit(self):
        js = (ROOT/"assets/agent-reach.js").read_text()
        failed = js.split("await request(source,", 1)[1].split("} catch (_) {", 1)[1].split("} finally", 1)[0]
        self.assertIn("readable.delete(source)", failed)
        self.assertIn("请先更新显示确认运行记录", failed)
        self.assertIn("状态待确认", failed)
        self.assertNotIn("运行失败", failed)
        self.assertIn("button.disabled = active.has(source) || !readable.has(source)", js)

    def test_employee_docs(self):
        for name in ['AGENT_REACH_USER_MANUAL_CN.md','AGENT_REACH_QUICK_START_CN.md']:
            text=(ROOT/'docs'/name).read_text()
            self.assertIn('CRM → 内容管理 → Agent Reach', text)
            for bad in ['SHA','Bridge','OpenCLI','executor','sidecar','localhost','8790']:
                self.assertNotIn(bad,text)

if __name__ == '__main__': unittest.main()
