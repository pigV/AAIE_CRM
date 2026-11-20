import sqlite3
import unittest
from business_changes.identities import stable_command_identity,find_command_identity


class StableIdentityTests(unittest.TestCase):
    def setUp(self):
        self.c=sqlite3.connect(':memory:');self.c.row_factory=sqlite3.Row;self.addCleanup(self.c.close)
        self.c.execute('CREATE TABLE transfer_v2_command_receipts(event_uid TEXT PRIMARY KEY)')
    def find(self,prefix):return find_command_identity(self.c,'transfer_v2_command_receipts','event_uid',stable_command_identity(prefix))
    def test_legacy_identity_lookup_is_readonly_and_keeps_exact_id(self):
        identity='command-key:'+'1'*32;self.c.execute('INSERT INTO transfer_v2_command_receipts VALUES(?)',(identity,));before=self.c.total_changes
        self.assertEqual(identity,self.find('command-key')['event_uid']);self.assertEqual(before,self.c.total_changes)
    def test_prefix_keys_colons_and_sql_wildcards_do_not_collide(self):
        for identity in ('command-key:longer:'+'1'*32,'commandXkey:'+'2'*32):self.c.execute('INSERT INTO transfer_v2_command_receipts VALUES(?)',(identity,))
        self.assertIsNone(self.find('command-key'));self.assertIsNone(self.find('command_key'));self.assertIsNone(self.find('command%key'))
    def test_duplicate_legacy_commands_are_reported_instead_of_arbitrarily_chosen(self):
        for suffix in ('1'*32,'2'*32):self.c.execute('INSERT INTO transfer_v2_command_receipts VALUES(?)',('command-key:'+suffix,))
        with self.assertRaises(ValueError):self.find('command-key')
    def test_nonhex_factory_suffix_does_not_hide_an_actual_legacy_match(self):
        for suffix in ('x'*32,'y'*32,'1'*32):self.c.execute('INSERT INTO transfer_v2_command_receipts VALUES(?)',('command-key:'+suffix,))
        self.assertEqual('command-key:'+'1'*32,self.find('command-key')['event_uid'])
