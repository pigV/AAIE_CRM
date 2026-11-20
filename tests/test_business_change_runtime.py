import sqlite3
import unittest
from business_changes.runtime import enabled,SETTING
from business_changes.schema import install,SchemaUnavailable


class RolloutTests(unittest.TestCase):
    def setUp(self):
        self.c=sqlite3.connect(':memory:');self.addCleanup(self.c.close);self.c.execute('PRAGMA foreign_keys=ON')
        self.c.execute('CREATE TABLE system_settings(key PRIMARY KEY,value)')

    def test_additive_tables_alone_do_not_activate_unfinished_integrations(self):
        self.c.execute('BEGIN');install(self.c);self.c.commit();self.assertFalse(enabled(self.c))

    def test_explicit_enable_requires_complete_verified_schema(self):
        self.c.execute('INSERT INTO system_settings VALUES(?,?)',(SETTING,'enabled'));self.c.commit()
        with self.assertRaises(SchemaUnavailable):enabled(self.c)
        self.c.execute('BEGIN');install(self.c);self.c.commit();self.assertTrue(enabled(self.c))

    def test_unknown_flag_state_cannot_silently_fall_back(self):
        self.c.execute('INSERT INTO system_settings VALUES(?,?)',(SETTING,'probably'))
        with self.assertRaises(ValueError):enabled(self.c)
