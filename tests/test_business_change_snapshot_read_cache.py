import sqlite3
import unittest
from snapshot_read_cache import read_cache_scope,memoized_read


class SnapshotReadCacheTests(unittest.TestCase):
    def connection(self):
        c=sqlite3.connect(':memory:');self.addCleanup(c.close)
        c.execute('CREATE TABLE sample(value)');c.execute('INSERT INTO sample VALUES(1)');c.commit()
        return c

    def test_writer_and_nontransactional_scope_are_rejected(self):
        c=self.connection()
        with self.assertRaises(RuntimeError),read_cache_scope(c):pass
        c.execute('BEGIN')
        with self.assertRaises(RuntimeError),read_cache_scope(c):pass

    def test_writer_always_sees_its_own_new_values(self):
        c=self.connection();c.execute('BEGIN')
        load=lambda:c.execute('SELECT value FROM sample').fetchone()[0]
        self.assertEqual(1,memoized_read(c,'sample',load));c.execute('UPDATE sample SET value=2')
        self.assertEqual(2,memoized_read(c,'sample',load))

    def test_cache_is_dropped_on_exception_and_next_read_is_fresh(self):
        c=self.connection();c.execute('PRAGMA query_only=ON');c.execute('BEGIN')
        with self.assertRaises(ValueError):
            with read_cache_scope(c):
                self.assertEqual(1,memoized_read(c,'value',lambda:1));raise ValueError('end read')
        c.rollback();c.execute('BEGIN')
        with read_cache_scope(c):self.assertEqual(2,memoized_read(c,'value',lambda:2))

    def test_nested_other_connection_does_not_share_or_destroy_outer_read(self):
        a=self.connection();b=self.connection()
        for c in (a,b):c.execute('PRAGMA query_only=ON');c.execute('BEGIN')
        with read_cache_scope(a):
            self.assertEqual('A',memoized_read(a,'key',lambda:'A'))
            self.assertEqual('uncached B',memoized_read(b,'key',lambda:'uncached B'))
            with read_cache_scope(b):self.assertEqual('B',memoized_read(b,'key',lambda:'B'))
            self.assertEqual('A',memoized_read(a,'key',lambda:self.fail('outer cache lost')))

    def test_failed_loader_is_not_cached_as_success(self):
        c=self.connection();c.execute('PRAGMA query_only=ON');c.execute('BEGIN')
        with read_cache_scope(c):
            with self.assertRaises(ValueError):memoized_read(c,'key',lambda:(_ for _ in ()).throw(ValueError()))
            self.assertEqual(2,memoized_read(c,'key',lambda:2))
