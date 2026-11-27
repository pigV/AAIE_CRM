from html.parser import HTMLParser
import unittest
from tests.test_visa_material_workspace_ui import Handler, rows
from visa_material_workspace_ui import render_material_workspace


class MaterialSplitUITests(unittest.TestCase):
    def render(self):
        return render_material_workspace(Handler(),'S1',{'id':7},rows(),panel_id='material-checklist',title='材料清单',description='材料资料',show_smart_batch=True)

    def test_inline_preview_is_added_without_replacing_existing_controls(self):
        html=self.render()
        self.assertIn('data-material-preview',html)
        self.assertIn('data-material-preview-image',html)
        self.assertIn('data-material-preview-prev',html)
        self.assertIn('data-material-preview-next',html)
        self.assertIn('data-material-preview-fullscreen',html)
        self.assertIn('data-material-select="2"',html)
        self.assertIn('data-preview-open',html)
        self.assertIn('data-preview-src="/documents/9/preview?size=large"',html)
        self.assertIn('action="/students/S1/visa-cases/7/material-items/1/files"',html)
        self.assertIn('data-material-save',html)

    def test_all_five_counts_and_original_state_wording_remain(self):
        html=self.render()
        for value in ['required_unfinished','unfinished','all','uploaded','not_applicable']:
            self.assertIn(f'data-quick-filter="{value}"',html)
        for text in ['必交未完成','全部未完成','全部材料','已收到','不适用','已收到不代表审核通过','data-material-search','data-category-mobile','data-material-clear']:
            self.assertIn(text,html)

    def test_fileless_materials_still_selectable_and_initial_preview_has_no_remote_url(self):
        html=self.render()
        self.assertIn('data-material-select="1"',html)
        self.assertIn('选择材料查看原件',html)
        self.assertNotIn('<iframe',html)
        self.assertIn('notes_1',html)
        self.assertNotIn('src="https://',html)
