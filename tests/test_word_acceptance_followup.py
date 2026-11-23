import unittest
from html.parser import HTMLParser
from unittest.mock import patch

import app
import student_progression_summary as progression
from student_lifecycle.progression_contract_gate import ProgressionContractGateDecision
from tests import test_business_change_school_chain as chain


class FormNodes(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.nodes = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.nodes.append((tag, dict(attrs)))


class ProgressionGatePresentationTests(unittest.TestCase):
    def gate(self):
        return ProgressionContractGateDecision(None, None, False, False,
                                              'unclassified', '请先明确升学业务类型')

    def test_no_progression_or_history_does_not_display_an_entry_gate_as_current_business(self):
        for state in ('empty', 'history'):
            with self.subTest(state=state):
                gate = self.gate()
                summary = progression.StudentProgressionSummary('S1', state, '暂无在办升学', contract_gate=gate)
                html = progression.render_student_progression_summary(summary)
                self.assertNotIn('请先明确升学业务类型', html)
                self.assertNotIn('合同门禁', html)
                self.assertFalse(summary.contract_gate.allowed)

    def test_active_and_draft_progression_keep_the_real_gate_with_explicit_scope(self):
        for state in ('active', 'draft'):
            with self.subTest(state=state):
                summary = progression.StudentProgressionSummary('S1', state, '办理中', contract_gate=self.gate())
                html = progression.render_student_progression_summary(summary)
                self.assertIn('请先明确升学业务类型', html)
                self.assertIn('升学业务合同要求', html)
                self.assertFalse(summary.contract_gate.allowed)


class ExistingCoEReviewPresentationTests(unittest.TestCase):
    def setUp(self):
        self.f = chain.SchoolMaterialChainTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.test_plan_application_offer_payment_coe_and_evidence_withdrawal_use_current_cycle()
        self.record = self.f.db.one("SELECT id FROM coe_oshc WHERE student_id=? AND document_kind='COE'", (self.f.sid,))

    def render(self, sid):
        self.f.h.send_html = lambda html: html
        with patch.object(app, 'page', side_effect=lambda title, body, *args: body), patch.object(app.nas, 'status', return_value={}, create=True):
            return self.f.h.student_coe_course_confirmation('GET', sid, str(self.record['id']))

    def test_existing_coe_review_has_one_submit_and_same_record_original_preview(self):
        html = self.render(self.f.sid)
        nodes = FormNodes(html).nodes
        submits = [attrs for tag, attrs in nodes if tag == 'button' and attrs.get('type', 'submit') == 'submit']
        self.assertEqual(1, len(submits))
        self.assertEqual(1, html.count('确认课程学费'))
        self.assertNotIn('保存并确认所选文件', html)
        previews = [attrs for tag, attrs in nodes if 'data-review-preview-root' in attrs]
        self.assertEqual(1, len(previews))
        self.assertEqual(f"/coe-oshc/{self.record['id']}/preview?size=large", previews[0]['data-preview-src'])
        self.assertEqual(f"/coe-oshc/{self.record['id']}/download", previews[0]['data-download-url'])
        self.assertIn('upload-review-page', html)
        self.assertIn('data-review-fields-pane', html)
        self.assertIn('data-review-preview-fullscreen', html)
        self.assertIn('data-review-preview-next', html)
        self.assertEqual(1, len([1 for tag, _ in nodes if tag == 'form']))

    def test_existing_coe_review_does_not_expose_another_students_original(self):
        self.assertEqual(404, self.render('UNRELATED-STUDENT')[0])

    def test_rejected_confirmation_displays_safe_feedback_on_the_review_page(self):
        self.f.h.send_html = lambda html: html
        self.f.h.query = {'message': ['请选择并核对课程 <unsafe>'], 'kind': ['error']}
        with patch.object(app.nas, 'status', return_value={}, create=True):
            html = self.f.h.student_coe_course_confirmation('GET', self.f.sid, str(self.record['id']))
        self.assertIn('请选择并核对课程 &lt;unsafe&gt;', html)
        self.assertNotIn('请选择并核对课程 <unsafe>', html)
