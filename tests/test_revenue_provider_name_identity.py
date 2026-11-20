"""Real CoE legal provider suffixes must not obscure or merge schools."""
import unittest
from revenue.school_courses import _provider_school_key

class ProviderNameIdentityTests(unittest.TestCase):
    def test_explicit_legal_suffix_and_cricos_match_real_school(self):
        expected = _provider_school_key('Avalon College')
        for name in ('Avalon College Pty Ltd [04076C]', 'Avalon College PTY. LTD.',
                     'Avalon College [04076C]'):
            with self.subTest(name=name):
                self.assertEqual(expected, _provider_school_key(name))

    def test_distinguishing_names_and_non_cricos_brackets_are_preserved(self):
        expected = _provider_school_key('Avalon College')
        for name in ('Avalon English College Pty Ltd [04076C]',
                     'Avalon College [Melbourne]', 'Other Pty Ltd [04076C]',
                     'Avalon College Pty Ltd trading as Other'):
            with self.subTest(name=name):
                self.assertNotEqual(expected, _provider_school_key(name))
