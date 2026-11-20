import unittest
from dashboard_confirmed_stages import STAGES
from dashboard_stage_presentation import stage_color, stage_label, ordered_stage_definitions, stage_badge_html


class DashboardStagePresentationTests(unittest.TestCase):
    def test_existing_phase_colors_do_not_shift_when_transfer_phase_is_inserted(self):
        original = {key: stage_color(key) for key, _, _ in STAGES}
        definitions = [(key, label) for key, label, _ in STAGES] + [('materials_preparation', '资料准备')]
        ordered = ordered_stage_definitions(definitions)
        self.assertLess([k for k, _ in ordered].index('materials_preparation'), [k for k, _ in ordered].index('waiting_school_plan'))
        self.assertEqual(original, {key: stage_color(key) for key, _, _ in STAGES})
        self.assertEqual('#C36A8D', stage_color('11_visa_result'))
        self.assertEqual('#6B7C93', stage_color('10_visa_submission'))
        self.assertEqual(len(STAGES), len(set(original.values())))

    def test_transfer_row_is_named_without_changing_record_identity(self):
        self.assertEqual('转学资料准备', stage_label('materials_preparation', '资料准备'))
        self.assertEqual('转学资料准备', stage_label('materials_preparation', '转学中 · 资料准备'))
        self.assertEqual('等待签证结果', stage_label('11_visa_result', '等待签证结果'))

    def test_no_transfer_record_does_not_create_a_new_zero_or_fake_bucket(self):
        definitions = [(key, label) for key, label, _ in STAGES]
        before = definitions.copy()
        self.assertEqual(before, ordered_stage_definitions(definitions))
        self.assertEqual(before, definitions)

    def test_reorder_preserves_all_keys_and_does_not_mutate_inputs(self):
        definitions = [(key, label) for key, label, _ in STAGES] + [('unknown_transfer', 'Other'), ('materials_preparation', '资料准备')]
        before = definitions.copy()
        ordered = ordered_stage_definitions(definitions)
        self.assertCountEqual([k for k, _ in definitions], [k for k, _ in ordered])
        self.assertEqual(before, definitions)
        self.assertEqual('转学资料准备', dict(ordered)['materials_preparation'])

    def test_badge_and_chart_use_exact_same_key_color(self):
        for key, label, _ in STAGES:
            with self.subTest(key=key):
                html = stage_badge_html(key, label)
                self.assertIn('--application-stage-color:' + stage_color(key), html)
                self.assertIn('dashboard-application-stage', html)
                self.assertIn(label, html)

    def test_legacy_keys_share_the_equivalent_canonical_phase_color(self):
        for old, new in [('waiting_visa_result', '11_visa_result'), ('visa_preparation', '10_visa_submission'), ('waiting_school_offer', '07_school_response')]:
            self.assertEqual(stage_color(old), stage_color(new))

    def test_unknown_and_terminal_states_have_safe_explicit_colors(self):
        self.assertEqual('#64748B', stage_color('unknown-future-stage'))
        self.assertNotEqual(stage_color('visa_granted'), stage_color('visa_refused'))
        self.assertNotEqual(stage_color('contract_link_inactive'), stage_color('visa_granted'))

    def test_known_transfer_phases_are_distinct_and_never_unknown_gray(self):
        keys = ('materials_preparation', 'school_plan', 'school_application', 'offer_received', 'accepted_and_paid', 'coe_update', 'pending_completion')
        colors = [stage_color(key) for key in keys]
        self.assertNotIn('#64748B', colors)
        self.assertEqual(len(keys), len(set(colors)))

    def test_dynamic_labels_and_unknown_keys_are_html_escaped(self):
        html = stage_badge_html('\"><script>x</script>', '<img src=x onerror=alert(1)>')
        self.assertNotIn('<script>', html)
        self.assertNotIn('<img', html)
        self.assertIn('&lt;img', html)
        self.assertIn('--application-stage-color:#64748B', html)

if __name__ == '__main__':
    unittest.main()
