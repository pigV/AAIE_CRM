"""Contract source choices, incomplete records and submission against an in-memory DB."""
import unittest
import ast
from pathlib import Path
from types import SimpleNamespace
from html.parser import HTMLParser

from tests import test_transfer_source_authority as source_fixture
from student_lifecycle import transfer_source_authority as source
from transfer_v2_views import render_transfer_contract_source_fields


class FormParser(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.selects = {}
        self.options = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'select':
            self.selects[attrs['name']] = attrs
        elif tag == 'option':
            self.options.append(attrs)


class ContractSourceSelectorTests(unittest.TestCase):
    def setUp(self):
        self.fixture = source_fixture.SourceAuthorityTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.db = self.fixture.db

    def context(self, student):
        self.assertTrue(hasattr(source, 'contract_source_options'),
                        'Contract form needs read-only diagnostics for missing choices')
        return source.contract_source_options(self.db, student)

    def test_valid_new_intake_is_selected_and_other_branch_stays_empty(self):
        context = self.context('new')
        html = render_transfer_contract_source_fields(context)
        form = FormParser(html)
        self.assertTrue(any(o.get('value') == 'new-intake' and 'selected' in o for o in form.options))
        self.assertEqual('true', form.selects['source_existing_evidence']['data-source-empty'])
        self.assertIn('/students/new?tab=profile', html)

    def test_existing_choice_shows_exact_record_and_submits(self):
        html = render_transfer_contract_source_fields(self.context('existing'))
        form = FormParser(html)
        choice = next(o for o in form.options if o.get('value'))
        self.assertIn('合同 #201', html)
        self.assertIn('selected', choice)
        row = source.ensure_for_submission(self.db, 'existing', 200,
            form={'source_existing_evidence': choice['value']},
            actor_user_id=1, occurred_at='2026-10-31')
        self.assertEqual('aaie_existing', row['source_classification'])

    def test_unlinked_history_is_explained_without_rewriting_it(self):
        self.db.execute('UPDATE contracts SET application_cycle_id=NULL WHERE id=201')
        before = self.db.total_changes
        context = self.context('existing')
        html = render_transfer_contract_source_fields(context)
        self.assertEqual([], context['existing'])
        self.assertIn('合同 #201', html)
        self.assertIn('未关联', html)
        self.assertIn('申请周期', html)
        self.assertIn('/students/existing?tab=contracts', html)
        self.assertEqual(before, self.db.total_changes)
        with self.assertRaises(source.SourceAuthorityError):
            source.identify_for_submission(self.db, 'existing', 200)

    def test_pending_intake_shows_reason_and_parent_form_entry(self):
        self.db.execute("UPDATE parent_form_tokens SET submitted_at=NULL WHERE student_id='new'")
        context = self.context('new')
        html = render_transfer_contract_source_fields(context)
        self.assertIn('尚未提交', html)
        self.assertIn('href="/parent-links"', html)
        self.assertEqual([], context['intakes'])

    def test_wrong_intake_type_and_invalidated_forms_not_silently_missing(self):
        for assignment, expected in [("intake_type='new_student'", '申请类型'),
                                     ("invalidated_at='2026-10-31'", '已撤销')]:
            with self.subTest(assignment=assignment):
                self.db.execute('SAVEPOINT example')
                self.db.execute(f"UPDATE parent_form_tokens SET {assignment} WHERE student_id='new'")
                self.assertIn(expected, render_transfer_contract_source_fields(self.context('new')))
                self.db.execute('ROLLBACK TO example')
                self.db.execute('RELEASE example')

    def test_missing_current_enrolment_has_specific_entry(self):
        self.db.execute("DELETE FROM student_school_enrolments WHERE student_id='existing'")
        html = render_transfer_contract_source_fields(self.context('existing'))
        self.assertIn('当前在读', html)
        self.assertIn('/students/existing/school-enrolments/current', html)

    def test_multiple_parent_choices_can_be_distinguished_and_submitted(self):
        self.db.execute("INSERT INTO parent_form_tokens(token,student_id,intake_type,submitted_at) VALUES('second','new','new_student_transfer','2026-09-07')")
        html = render_transfer_contract_source_fields(self.context('new'))
        self.assertIn('资料 1', html)
        self.assertIn('资料 2', html)
        self.assertFalse(any('selected' in o for o in FormParser(html).options))
        row = source.ensure_for_submission(self.db, 'new', 100,
            form={'source_parent_form_token': 'second'}, actor_user_id=1, occurred_at='2026-10-31')
        self.assertEqual('second', row['parent_form_token'])

    def test_conflicting_sources_are_explained_and_still_rejected(self):
        self.db.execute("INSERT INTO parent_form_tokens(token,student_id,intake_type,submitted_at) VALUES('conflict','existing','new_student_transfer','2026-10-31')")
        html = render_transfer_contract_source_fields(self.context('existing'))
        self.assertIn('冲突', html)
        with self.assertRaises(source.SourceAuthorityError):
            source.identify_for_submission(self.db, 'existing', 200,
                form={'source_parent_form_token': 'conflict'})

    def test_cross_student_records_are_not_offered(self):
        context = self.context('new')
        self.assertNotIn('other-intake', str(context))
        with self.assertRaises(source.SourceAuthorityError):
            source.identify_for_submission(self.db, 'new', 100,
                form={'source_parent_form_token': 'other-intake'})

    def test_saved_source_remains_selected_when_more_forms_exist(self):
        authority = self.fixture.save()
        self.db.execute("INSERT INTO parent_form_tokens(token,student_id,intake_type,submitted_at) VALUES('second','new','new_student_transfer','2026-10-31')")
        html = render_transfer_contract_source_fields(self.context('new'), authority=authority)
        form = FormParser(html)
        self.assertTrue(any(o.get('value') == 'new-intake' and 'selected' in o for o in form.options))
        self.assertEqual('true', form.selects['source_parent_form_token']['data-source-locked'])
        self.assertIn('已确认', html)

    def test_invalid_saved_source_cannot_appear_replaced_by_new_choice(self):
        authority = self.fixture.save()
        self.db.execute("UPDATE parent_form_tokens SET invalidated_at='2026-10-31' WHERE token='new-intake'")
        self.db.execute("INSERT INTO parent_form_tokens(token,student_id,intake_type,submitted_at) VALUES('second','new','new_student_transfer','2026-10-31')")
        html = render_transfer_contract_source_fields(self.context('new'), authority=authority)
        form = FormParser(html)
        self.assertTrue(any(o.get('value') == 'new-intake' and 'selected' in o for o in form.options))
        self.assertFalse(any(o.get('value') == 'second' and 'selected' in o for o in form.options))
        self.assertIn('当前不可用', html)
        with self.assertRaises(source.SourceAuthorityError):
            source.identify_for_submission(self.db, 'new', 100,
                form={'source_parent_form_token': 'second'})
        panel = FormParser(self.panel_html('new'))
        self.assertEqual('true', panel.selects['source_parent_form_token']['data-source-locked'])
        self.assertTrue(any(o.get('value') == 'new-intake' and 'selected' in o for o in panel.options))

    def test_display_text_is_escaped(self):
        context = self.context('existing')
        context['existing'][0]['current_school_name'] = '<script>alert(1)</script>'
        html = render_transfer_contract_source_fields(context)
        self.assertNotIn('<script>', html)
        self.assertIn('&lt;script&gt;', html)

    def test_contract_panel_shows_choices_even_when_preview_succeeds(self):
        self.assertIn('value="21:20:201:0:0"', self.panel_html('existing'))

    def panel_html(self, student):
        # Execute the actual read-only source section without importing app runtime.
        from tests.test_transfer_source_routes import _static_method_node
        root = Path(__file__).resolve().parents[1]
        trees = {}
        def load_tree(module):
            if module not in trees:
                path = root.joinpath(*module.split('.')).with_suffix('.py')
                trees[module] = ast.parse(path.read_text()) if path.exists() else None
            return trees[module]
        method = _static_method_node('contract_panel', load_tree)
        boundary = next(i for i, stmt in enumerate(method.body)
                        if isinstance(stmt, ast.If) and isinstance(stmt.test, ast.Name)
                        and stmt.test.id == 'published_mode')
        method.body = method.body[:boundary] + [ast.Return(value=ast.Name(id='transfer_source_fields', ctx=ast.Load()))]
        import sqlite3
        import transfer_v2_views
        namespace = dict(
            db=SimpleNamespace(connect=lambda: self.db, _release_connection=lambda connection: None),
            can_generate_contract=lambda user: True, can_manage_transfer_contract=lambda user: True,
            restrict_contract_creation_for_transfer_context=lambda *args: False,
            published_contract_generation_active=lambda: True,
            transfer_source_authority=source, transfer_v2_views=transfer_v2_views,
            transfer_contract_authority=SimpleNamespace(commercials=lambda *args, **kwargs: {'base_fee':'2400','final_fee':'2400'}),
            now_iso=lambda: '2026-10-31', format_money=str, money_decimal=str, sqlite3=sqlite3,
        )
        exec(compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])), 'app.py', 'exec'), namespace)
        return namespace['contract_panel'](SimpleNamespace(user={'id':1}), {'student_id':student}, [], [])


if __name__ == '__main__':
    unittest.main()
