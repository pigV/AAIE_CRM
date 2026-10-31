"""Run via the Staging sandbox; fixtures never use the running business DB."""
from contextlib import chdir
from pathlib import Path
import unittest

import app
from tests import test_core

isolated_upload_review_regression_environment = test_core.isolated_upload_review_regression_environment

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT/'migrations/20261031_043_offer_language_school.sql'


class OfferLanguagePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.helpers = test_core.CoreTests()
        self.cwd = chdir(ROOT/'STAGING_DATA')
        self.cwd.__enter__()
        self.addCleanup(self.cwd.__exit__, None, None, None)

    def review(self, env, language='Manual Language College'):
        return self.helpers._create_isolated_review(
            env, 'Offer', [('offer.txt','text/plain',b'School: Main College')],
            form={'language_school_name': language})

    def test_manual_language_prefills_review_and_survives_confirmation(self):
        with isolated_upload_review_regression_environment('language-prefill') as env:
            env['db'].execute(MIGRATION.read_text())
            token = self.review(env)
            handler = self.helpers._upload_review_handler(env['admin'])
            handler.upload_review('GET', env['student_id'], token)
            self.assertIn('name="extract_language_school_name_0" value="Manual Language College"', handler.response_html)
            self.helpers._confirm_isolated_review(env, token, 'Offer', ['Offer'],
                {'extract_school_name_0':'Main College','extract_language_school_name_0':'Manual Language College'})
            row = env['db'].one('SELECT * FROM offers')
            self.assertEqual(row['language_school_name'], 'Manual Language College')
            self.assertIn('name="language_school_name" value="Manual Language College"', handler.offer_panel(env['student_id'], [row]))

    def test_explicit_clear_overrides_manual_value(self):
        with isolated_upload_review_regression_environment('language-clear') as env:
            env['db'].execute(MIGRATION.read_text())
            token = self.review(env)
            self.helpers._confirm_isolated_review(env, token, 'Offer', ['Offer'],
                {'extract_school_name_0':'Main College','extract_language_school_name_0':''})
            self.assertEqual(env['db'].one('SELECT language_school_name FROM offers')[0], '')

    def test_untouched_manual_blank_keeps_extracted_language(self):
        with isolated_upload_review_regression_environment('language-auto') as env:
            env['db'].execute(MIGRATION.read_text())
            text = b'Course Details\nEnglish Language Centre:\nLanguage College\nSchool:\nMain College\nAddress:\nExample Street'
            token = self.helpers._create_isolated_review(env, 'Offer',
                [('offer.txt','text/plain',text)], form={'language_school_name':''})
            handler = self.helpers._upload_review_handler(env['admin'])
            handler.upload_review('GET', env['student_id'], token)
            self.assertIn('name="extract_language_school_name_0" value="Language College"', handler.response_html)

    def test_missing_migration_rejects_before_document_or_review_is_saved(self):
        with isolated_upload_review_regression_environment('language-preflight') as env:
            token = self.review(env)
            before = env['db'].one('SELECT count(*) FROM student_documents')[0]
            handler = self.helpers._confirm_isolated_review(env, token, 'Offer', ['Offer'],
                {'extract_language_school_name_0':'Language College'})
            self.assertEqual(handler.response_status, 400)
            self.assertEqual(env['db'].one('SELECT count(*) FROM student_documents')[0], before)
            self.assertEqual(env['db'].one('SELECT status FROM upload_review_items')[0], 'NeedsReview')

    def test_manual_create_update_and_reload(self):
        with isolated_upload_review_regression_environment('language-update') as env:
            env['db'].execute(MIGRATION.read_text())
            handler = self.helpers._upload_review_handler(env['admin'],
                {'school_name':'Main College','language_school_name':'Language College'})
            handler.files = []
            handler.offer_create('POST', env['student_id'])
            row = env['db'].one('SELECT * FROM offers')
            self.assertEqual(row['language_school_name'], 'Language College')
            handler.form = {'language_school_name':'Updated College'}
            handler.offer_update('POST', env['student_id'], row['id'])
            self.assertEqual(env['db'].one('SELECT language_school_name FROM offers')[0], 'Updated College')
            handler.form = {'language_school_name':''}
            handler.offer_update('POST', env['student_id'], row['id'])
            self.assertEqual(env['db'].one('SELECT language_school_name FROM offers')[0], '')


if __name__ == '__main__':
    unittest.main()
