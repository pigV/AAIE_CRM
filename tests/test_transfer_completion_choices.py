from dataclasses import replace
from unittest import TestCase
from tests.test_transfer_v2_ui import TransferV2UiTests
import student_lifecycle.transfer_v2_models as models
from transfer_v2_views import render_transfer_v2_detail

class TransferCompletionChoicesTests(TestCase):
    def test_missing_or_invalid_choices_cannot_default_to_updates(self):
        for value in ({},{'update_current_school':'1'},dict(update_current_school='yes',update_current_year_level='0',close_unfinished_branches='1')):
            with self.subTest(value=value),self.assertRaises(ValueError):
                models.validate_completion_choices(value)

    def test_explicit_yes_and_no_remain_separate(self):
        values=dict(update_current_school='0',update_current_year_level='1',close_unfinished_branches='0')
        self.assertEqual(models.validate_completion_choices(values),dict(update_current_school=False,update_current_year_level=True,close_unfinished_branches=False))

    def test_new_cycle_completion_forces_school_transition_and_keeps_two_choices(self):
        fixture=TransferV2UiTests()
        projection=replace(fixture.projection(stage='pending_completion'),next_action='确认结束转学',ready_to_complete=True)
        html=render_transfer_v2_detail(fixture.overview(),projection,csrf='csrf',can_edit=True,documents_by_branch={})
        self.assertIn('<input type="hidden" name="update_current_school" value="1">',html)
        self.assertNotIn('<select name="update_current_school" required>',html)
        for name in ('update_current_year_level','close_unfinished_branches'):
            self.assertIn(f'<select name="{name}" required>',html)
        self.assertIn('旧当前在读转为历史',html)
        self.assertIn('已接受学校转为新当前在读',html)
        self.assertIn('是否更新当前年级',html)
        self.assertIn('是否关闭其他未完成目标学校',html)
