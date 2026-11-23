import sqlite3
import unittest
import offer_acceptance


class OfferSchoolChoicesTests(unittest.TestCase):
    def setUp(self):
        self.c=sqlite3.connect(':memory:'); self.c.row_factory=sqlite3.Row
        self.addCleanup(self.c.close)
        self.c.executescript('''CREATE TABLE schools(id INTEGER PRIMARY KEY,school_name TEXT,city_id INTEGER,sector_key TEXT,state TEXT,deleted_at TEXT);
        CREATE TABLE school_cities(id INTEGER PRIMARY KEY,city_name TEXT,state TEXT,is_active INTEGER);
        INSERT INTO school_cities VALUES(1,'Melbourne','VIC',1);
        INSERT INTO schools VALUES(40,'Doncaster Secondary College',NULL,'public','VIC',NULL);
        INSERT INTO schools VALUES(500,'East Doncaster Secondary College',1,'public','VIC',NULL);
        INSERT INTO schools VALUES(501,'Deleted Doncaster',1,'public','VIC','2026-01-01');''')

    def test_legacy_null_city_school_is_searchable_without_mutating_school_master(self):
        choices=offer_acceptance.offer_school_choices(self.c)
        self.assertEqual([40,500],sorted(r['id'] for r in choices))
        school=next(r for r in choices if r['id']==40)
        self.assertEqual(('Melbourne',1,'public'),(school['city_name'],school['city_id'],school['sector_key']))
        self.assertIsNone(self.c.execute('SELECT city_id FROM schools WHERE id=40').fetchone()[0])

    def test_ambiguous_city_is_visible_but_marked_for_review_not_silently_assigned(self):
        self.c.execute("INSERT INTO school_cities VALUES(2,'Geelong','VIC',1)")
        row=next(r for r in offer_acceptance.offer_school_choices(self.c) if r['id']==40)
        self.assertIsNone(row['city_id'])
        self.assertIn('待完善',row['city_name'])
        self.assertNotIn(501,[r['id'] for r in offer_acceptance.offer_school_choices(self.c)])
