import unittest
from visa_workspace_candidate import document_workspace_html


class RetainedVisaCorrectionViewTests(unittest.TestCase):
    def render(self,**values):
        result=dict(effective_result='Pending',recorded_result='Granted',manual_override=True,
                    authority='manual',auto_detected_result=None,conflict=False)
        result.update(values)
        return document_workspace_html(77,'synthetic-csrf',[],[],[],result,compact_result_controls=True)

    def test_unverified_manual_grant_is_retained_in_the_correction_form(self):
        html=self.render()
        self.assertIn('<option selected>Granted</option>',html)
        self.assertIn('原人工结果记录',html)

    def test_provisional_machine_grant_does_not_preselect_an_official_grant_correction(self):
        html=self.render(manual_override=False,authority='auto')
        self.assertIn('<option selected>Pending</option>',html)
        self.assertNotIn('原人工结果记录',html)
