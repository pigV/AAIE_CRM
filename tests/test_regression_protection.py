import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RegressionProtectionTests(unittest.TestCase):
    def test_stable_features_document_lists_initial_protected_features(self):
        stable_doc = ROOT / "docs" / "STABLE_FEATURES.md"
        content = stable_doc.read_text(encoding="utf-8")

        required_features = [
            "学生管理列表",
            "学生详情页",
            "学生搜索功能",
            "Application 文件上传",
            "Application 文件预览",
            "Application 文件下载",
            "文件类型修改",
            "Offer 上传和自动识别",
            "COE / OSHC 文件预览",
            "学生信息表在线填写",
            "学生信息表提交后生成 Word",
            "Word 文件命名规则",
            "合同生成",
            "合同链接生成",
            "合同微信授权",
            "合同在线签名",
            "已签合同归档到 Application",
            "合同预览和下载",
            "合同删除 / 作废",
            "学校方案生成",
            "学校方案安全查看链接",
            "微信授权查看",
            "水印显示",
            "学校方案家长确认",
            "学校排序确认",
            "最终确认方案归档",
            "页面操作后不自动跳到顶部",
            "手机端安全查看页面",
            "手机端学校对比看板",
            "后台所有按钮操作后的局部刷新逻辑",
        ]
        for feature in required_features:
            self.assertIn(feature, content)
        self.assertIn("Protected", content)
        self.assertIn("Needs Retest", content)

    def test_regression_protection_docs_define_scope_and_forbidden_zones(self):
        protection_doc = (ROOT / "docs" / "REGRESSION_PROTECTION.md").read_text(encoding="utf-8")
        checklist_doc = (ROOT / "tests" / "REGRESSION_CHECKLIST.md").read_text(encoding="utf-8")

        for text in [
            "本次修改范围声明",
            "禁止误改区域",
            "高风险公共文件",
            "修改后回归测试报告",
        ]:
            self.assertIn(text, protection_doc)
        for text in [
            "测试 A：学生信息表流程",
            "测试 B：合同流程",
            "测试 C：学校方案流程",
            "测试 D：文件预览流程",
            "测试 E：页面交互",
        ]:
            self.assertIn(text, checklist_doc)

    def test_regression_check_script_lists_core_suites(self):
        script = ROOT / "scripts" / "regression_check.py"
        result = subprocess.run(
            [sys.executable, str(script), "--student-info", "--contract", "--list"],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("student-info", result.stdout)
        self.assertIn("contract", result.stdout)
        self.assertIn("tests.test_core.CoreTests", result.stdout)
        self.assertIn("reports/regression", result.stdout)


if __name__ == "__main__":
    unittest.main()
