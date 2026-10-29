"""Startup defaults must not overwrite existing administrator-managed values."""
import tempfile
import unittest
from pathlib import Path

from database import Database


class StartupSeedPreservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='startup-seed-')
        self.addCleanup(self.temp.cleanup)
        self.db = Database(Path(self.temp.name) / 'synthetic.sqlite3')
        self.db.initialize()

    def test_existing_manual_configuration_survives_two_initializations(self):
        cases = [
            ('modules', "module_key='revenue_commission'", {'module_name_en': 'Manual Revenue', 'is_active': 0}),
            ('school_cities', "city_name='Melbourne' AND state='VIC'", {'sort_order': 999, 'is_active': 0}),
            ('data_sources', "source_name='ACARA School Profile Data' AND state='ALL'", {'source_url': 'https://synthetic.invalid/manual', 'auto_check_enabled': 0}),
            ('state_fee_schedules', "state='VIC' AND schedule_name='VIC Government School Fees 2026'", {'source_name': 'Manual verified source', 'source_url': 'https://synthetic.invalid/fees', 'is_active': 0}),
            ('school_suburb_data', "source_type='Sample' AND suburb_id IN (SELECT id FROM school_suburbs WHERE suburb='South Yarra' AND state='VIC')", {'notes': 'Manual reviewed note'}),
            ('academic_results_update_rules', "state='VIC'", {'enabled': 0, 'update_frequency': 'Manual only'}),
            ('user_module_permissions', "module_key='reminders'", {'can_manage_rules': 0}),
            ('school_sectors', "sector_key='public'", {'sector_name_en': 'Manual sector', 'sort_order': 999, 'is_active': 0}),
            ('school_feature_categories', "category_key='sports'", {'category_name_en': 'Manual sports', 'description': 'Manual description', 'is_active': 0}),
            ('school_feature_tags', "tag_key='boarding'", {'tag_name_zh': 'Manual boarding', 'color': '#112233', 'is_active': 0}),
            ('school_suburb_data_sources', "source_name='Manual AAIE Entry'", {'source_url': 'https://synthetic.invalid/suburbs', 'enabled': 0, 'notes': 'Manual source'}),
            ('school_geocode_cache', "normalized_query='melbournecbd' AND state='VIC'", {'latitude': -37.1, 'longitude': 145.1, 'geocode_source': 'Manual checked'}),
            ('school_suburbs', "suburb='South Yarra' AND state='VIC'", {'country': 'Manual country'}),
        ]
        expected = []
        for table, predicate, values in cases:
            self.assertEqual(len(self.db.all(f'SELECT * FROM {table} WHERE {predicate}')), 1, table)
            self.db.execute(f"UPDATE {table} SET {','.join(key+'=?' for key in values)} WHERE {predicate}", tuple(values.values()))
            row = dict(self.db.one(f'SELECT * FROM {table} WHERE {predicate}'))
            expected.append((table, row['id'], row))
        for run in (1, 2):
            self.db.initialize()
            for table, row_id, before in expected:
                with self.subTest(run=run, table=table):
                    self.assertEqual(dict(self.db.one(f'SELECT * FROM {table} WHERE id=?', (row_id,))), before)

    def test_existing_access_revocation_is_not_promoted_from_action_bits(self):
        self.db.execute("UPDATE user_module_permissions SET can_access=0,can_view=0,can_create=1 WHERE module_key='reminders'")
        before = dict(self.db.one("SELECT * FROM user_module_permissions WHERE module_key='reminders'"))
        for _ in range(2):
            self.db.initialize()
            self.assertEqual(dict(self.db.one("SELECT * FROM user_module_permissions WHERE module_key='reminders'")), before)

    def test_nonempty_school_facts_survive_normalization(self):
        self.db.execute("UPDATE schools SET accommodation_type='Manual verified accommodation',offers_ib='Unknown',interview_required='Unknown' WHERE school_name='Melbourne High School'")
        before = dict(self.db.one("SELECT * FROM schools WHERE school_name='Melbourne High School'"))
        for _ in range(2):
            self.db.initialize()
            after = dict(self.db.one("SELECT * FROM schools WHERE school_name='Melbourne High School'"))
            for field in ('accommodation_type', 'offers_ib', 'interview_required'):
                self.assertEqual(after[field], before[field], field)

    def test_missing_school_values_receive_only_safe_defaults(self):
        self.db.execute("UPDATE schools SET accommodation_type='',offers_ib='',interview_required='' WHERE school_name='Melbourne High School'")
        self.db.initialize()
        before = dict(self.db.one("SELECT * FROM schools WHERE school_name='Melbourne High School'"))
        self.assertEqual((before['accommodation_type'], before['offers_ib'], before['interview_required']), ('Unknown', 'No', 'Unknown'))
        self.db.initialize()
        after = dict(self.db.one("SELECT * FROM schools WHERE school_name='Melbourne High School'"))
        self.assertEqual(after, before)

    def test_documented_content_deny_boundary_remains(self):
        role = self.db.one("SELECT id FROM roles WHERE name='Manager'")
        user_id = self.db.execute("INSERT INTO users(username,display_name,password_hash,role_id,is_active,created_at) VALUES('synthetic-manager','Synthetic','unused',?,1,'2026-01-01')", (role['id'],))
        self.db.initialize()
        self.db.execute("UPDATE user_module_permissions SET can_access=1,can_view=1,can_create_topics=1 WHERE user_id=? AND module_key='content_center'", (user_id,))
        self.db.initialize()
        row = self.db.one("SELECT * FROM user_module_permissions WHERE user_id=? AND module_key='content_center'", (user_id,))
        self.assertEqual((row['can_access'], row['can_view'], row['can_create_topics']), (0, 0, 0))

    def test_existing_role_permissions_are_not_regranted(self):
        self.db.execute("UPDATE roles SET permissions_json='[]' WHERE name='Admin'")
        for _ in range(2):
            self.db.initialize()
            self.assertEqual(self.db.one("SELECT permissions_json FROM roles WHERE name='Admin'")['permissions_json'], '[]')

    def test_missing_tag_category_does_not_overwrite_manual_group(self):
        self.db.execute("UPDATE school_feature_tags SET category_id=NULL,tag_group='Manual verified group' WHERE tag_key='boarding'")
        for _ in range(2):
            self.db.initialize()
            self.assertEqual(self.db.one("SELECT tag_group FROM school_feature_tags WHERE tag_key='boarding'")['tag_group'], 'Manual verified group')

    def test_missing_suburb_link_does_not_overwrite_manual_status(self):
        self.db.execute("UPDATE schools SET suburb_id=NULL,suburb_data_status='Manual verified status' WHERE school_name='Melbourne High School'")
        for _ in range(2):
            self.db.initialize()
            self.assertEqual(self.db.one("SELECT suburb_data_status FROM schools WHERE school_name='Melbourne High School'")['suburb_data_status'], 'Manual verified status')

    def test_missing_setting_default_is_created_existing_value_preserved(self):
        self.db.execute("DELETE FROM system_settings WHERE key='auto_backup_time'")
        self.db.execute("UPDATE system_settings SET value='false' WHERE key='auto_backup_enabled'")
        self.db.initialize()
        self.assertEqual(self.db.setting('auto_backup_time'), '23:30')
        self.assertEqual(self.db.setting('auto_backup_enabled'), 'false')

    def test_missing_module_permission_gets_complete_default(self):
        self.db.execute("DELETE FROM user_module_permissions WHERE module_key='reminders'")
        self.db.initialize()
        row = self.db.one("SELECT * FROM user_module_permissions WHERE module_key='reminders'")
        self.assertEqual(row['can_manage_rules'], 1)
        self.assertEqual(row['can_create'], 1)
        self.db.execute("UPDATE user_module_permissions SET can_manage_rules=0,can_create=0 WHERE id=?", (row['id'],))
        self.db.initialize()
        after = self.db.one('SELECT * FROM user_module_permissions WHERE id=?', (row['id'],))
        self.assertEqual(after['can_manage_rules'], 0)
        self.assertEqual(after['can_create'], 0)


if __name__ == '__main__':
    unittest.main()
