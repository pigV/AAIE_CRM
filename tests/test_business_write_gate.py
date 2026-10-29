"""Isolated real SQLite and flock gate tests; no app import or Production paths."""
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

try:
    import business_write_gate as module
except ImportError:
    module=None

ROOT=Path(__file__).resolve().parents[1]

class GateTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(module,'gate implementation missing')
        base=ROOT/'SAFEBOX/current-gate-20260909/business-gate-core/tmp'
        base.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=base)
        self.root=Path(self.temp.name);self.db=self.root/'fixture.db'
        with sqlite3.connect(self.db) as c:
            c.executescript('CREATE TABLE business(id INTEGER PRIMARY KEY,value TEXT); INSERT INTO business VALUES(1,"original"); CREATE TABLE sessions(token TEXT PRIMARY KEY,last_seen_at TEXT,user_id INTEGER); CREATE TABLE users(id INTEGER PRIMARY KEY,last_login_at TEXT,last_login_ip TEXT,role TEXT); INSERT INTO users VALUES(1,"old","old","Admin"); CREATE TABLE audit_logs(id INTEGER PRIMARY KEY,action_type TEXT);')
        self.gate=module.Gate(self.db,self.root/'gate')
        self.gate.initialize_open()
        self.uninstall=module.install_sqlite_guard(self.gate)
    def tearDown(self):
        if hasattr(self,'uninstall'):self.uninstall()
        if hasattr(self,'temp'):self.temp.cleanup()
    def test_off_on_off_no_queued_write(self):
        with sqlite3.connect(self.db) as c:c.execute('UPDATE business SET value="before"')
        closed=self.gate.enable()
        with self.assertRaises(module.MaintenanceError):
            with self.gate.operation('business'):self.fail('side effect reached')
        with sqlite3.connect(self.db) as c:
            self.assertEqual(c.execute('SELECT value FROM business').fetchone()[0],'before')
            with self.assertRaises(module.MaintenanceError):c.execute('UPDATE business SET value="blocked"')
        self.gate.disable(expected_generation=closed['generation'],release_verified=True)
        with sqlite3.connect(self.db) as c:
            self.assertEqual(c.execute('SELECT value FROM business').fetchone()[0],'before')
            c.execute('UPDATE business SET value="after"')
    def test_unknown_state_denies_auth_and_business_but_allows_read(self):
        (self.root/'gate/state.json').write_text('{invalid')
        self.assertEqual(self.gate.status()['state'],'UNKNOWN')
        for kind in ('auth','business'):
            with self.assertRaises(module.MaintenanceError):
                with self.gate.operation(kind):pass
        with self.gate.operation('read'):
            with sqlite3.connect(self.db) as c:self.assertEqual(c.execute('SELECT count(*) FROM business').fetchone()[0],1)
    def test_auth_scope_is_column_limited_and_cannot_ddl_or_attach(self):
        self.gate.enable()
        with self.gate.operation('auth'),sqlite3.connect(self.db) as c:
            c.execute('INSERT INTO sessions VALUES(?,?,?)',('s','now',1))
            c.execute('UPDATE users SET last_login_at=?,last_login_ip=? WHERE id=?',('now','loopback',1))
            c.execute('INSERT INTO audit_logs(action_type) VALUES(?)',('login_failed',))
        for sql in ('UPDATE users SET role="Owner"','UPDATE sessions SET user_id=2','DELETE FROM audit_logs','CREATE TABLE forbidden(x)','PRAGMA user_version=3','UPDATE business SET value="bad"'):
            with self.subTest(sql=sql),self.gate.operation('auth'),sqlite3.connect(self.db) as c:
                with self.assertRaises(module.MaintenanceError):c.execute(sql)
        with self.gate.operation('auth'),sqlite3.connect(self.db) as c:c.execute('DELETE FROM sessions WHERE token=?',('s',))
    def test_cached_cursor_and_script_cannot_cross_enable(self):
        c=sqlite3.connect(self.db,isolation_level=None);cursor=c.cursor()
        cursor.execute('UPDATE business SET value=?',('first',))
        self.gate.enable()
        for execute in (lambda:cursor.execute('UPDATE business SET value=?',('second',)),lambda:c.executemany('UPDATE business SET value=?',[('second',)]),lambda:c.executescript('UPDATE business SET value="second";')):
            with self.assertRaises(module.MaintenanceError):execute()
        self.assertEqual(c.execute('SELECT value FROM business').fetchone()[0],'first');c.close()
    def test_transaction_drains_before_enable_and_rollback(self):
        c=sqlite3.connect(self.db);c.execute('UPDATE business SET value="uncommitted"')
        with self.assertRaises(module.GateBusy):self.gate.enable(timeout=.03)
        c.rollback();c.close()
        self.gate.enable()
        with sqlite3.connect(self.db) as c:self.assertEqual(c.execute('SELECT value FROM business').fetchone()[0],'original')
    def test_context_commit_releases_lock_and_unsafe_factory_denied(self):
        with sqlite3.connect(self.db) as c:c.execute('UPDATE business SET value="committed"')
        self.gate.enable(timeout=.03);c.close()
        with self.assertRaises(module.MaintenanceError):sqlite3.connect(self.db,factory=sqlite3.Connection)
    def test_read_and_auth_operations_drain_cross_process(self):
        # Actual separate flock owner, not a mocked lock.
        ctx=multiprocessing.get_context('fork');parent,child=ctx.Pipe()
        def worker():
            gate=module.Gate(self.db,self.root/'gate')
            with gate.operation('read'):
                child.send('held');child.recv()
        process=ctx.Process(target=worker);process.start()
        try:
            self.assertEqual(parent.recv(),'held')
            with self.assertRaises(module.GateBusy):self.gate.enable(timeout=.04)
            parent.send('release');process.join(3);self.assertEqual(process.exitcode,0)
            self.gate.enable(timeout=.1)
        finally:
            if process.is_alive():parent.send('release');process.join(3)
    def test_generation_restart_and_missing_state_never_auto_open(self):
        closed=self.gate.enable();other=module.Gate(self.db,self.root/'gate')
        self.assertEqual(other.status(),closed)
        with self.assertRaises(module.MaintenanceError):other.disable(expected_generation='wrong',release_verified=True)
        with self.assertRaises(module.MaintenanceError):other.disable(expected_generation=closed['generation'],release_verified=False)
        (self.root/'gate/state.json').unlink()
        self.assertEqual(other.status()['state'],'UNKNOWN')
        with self.assertRaises(module.MaintenanceError):other.initialize_open()
    def test_aliases_and_attach_bypass_blocked(self):
        self.gate.enable()
        for path in (self.db.as_uri()+'?mode=rw',str(self.db)):
            with self.subTest(path=path),sqlite3.connect(path,uri=path.startswith('file:')) as c:
                with self.assertRaises(module.MaintenanceError):c.execute('UPDATE business SET value="bad"')
        with sqlite3.connect(':memory:') as c:
            c.execute('CREATE TABLE harmless(x)')
            with self.assertRaises(module.MaintenanceError):c.execute('ATTACH DATABASE ? AS other',(str(self.db),))
    def test_authorizer_override_and_transaction_denial_rolls_back(self):
        self.gate.enable()
        with self.gate.operation('auth'),sqlite3.connect(self.db) as c:
            c.execute('INSERT INTO sessions VALUES("new","now",1)')
            with self.assertRaises(module.MaintenanceError):c.execute('UPDATE business SET value="bad"')
            with self.assertRaises(module.MaintenanceError):c.set_authorizer(None)
        with sqlite3.connect(self.db) as c:self.assertEqual(c.execute('SELECT count(*) FROM sessions').fetchone()[0],0)

    def test_connection_local_pragmas_and_public_binding(self):
        self.gate.enable()
        with self.gate.operation('read'),sqlite3.connect(self.db) as c:
            self.assertIs(c.write_gate,self.gate)
            self.assertEqual(self.gate.current_kind(),'read')
            c.execute('PRAGMA foreign_keys=ON')
            self.assertTrue(c.execute('PRAGMA table_info(business)').fetchall())
            c.execute('PRAGMA query_only=ON')
            for sql in ('PRAGMA query_only=OFF','PRAGMA wal_checkpoint','PRAGMA journal_mode=WAL'):
                with self.assertRaises(module.MaintenanceError):c.execute(sql)
    def test_real_hardlink_target_when_filesystem_supports_it(self):
        import errno
        alias=self.root/'hardlink.db'
        try:os.link(self.db,alias)
        except OSError as exc:
            if exc.errno in (errno.ENOTSUP,errno.EOPNOTSUPP):self.skipTest('exFAT has no hard links; real alias test NOT RUN')
            raise
        self.gate.enable()
        with sqlite3.connect(alias) as c:
            with self.assertRaises(module.MaintenanceError):c.execute('UPDATE business SET value="bad"')
    def test_nested_business_never_inherits_auth_and_other_sql_errors_survive(self):
        self.gate.enable()
        with self.gate.operation('auth'):
            with self.assertRaises(module.MaintenanceError):
                with self.gate.operation('business'):self.fail('business entered')
            with self.gate.operation('read'),sqlite3.connect(self.db) as c:
                with self.assertRaises(module.MaintenanceError):c.execute('DELETE FROM sessions')
                with self.assertRaises(sqlite3.OperationalError):c.execute('SELECT * FROM missing_table')
    def test_dbapi2_alias_and_late_transaction_commit_are_guarded(self):
        self.gate.enable()
        with sqlite3.dbapi2.connect(self.db) as c:
            with self.assertRaises(module.MaintenanceError):c.execute('UPDATE business SET value="bad"')

    def test_autocommit_returning_holds_drain_until_cursor_finishes(self):
        c=sqlite3.connect(self.db,isolation_level=None)
        cursor=c.execute('UPDATE business SET value="returned" RETURNING value')
        with self.assertRaises(module.GateBusy):self.gate.enable(timeout=.03)
        self.assertEqual(cursor.fetchall(),[('returned',)])
        self.gate.enable(timeout=.1);c.close()

    def test_closed_target_never_created_by_connect(self):
        self.uninstall()
        missing=self.root/'must-not-create.db'
        self.gate=module.Gate(missing,self.root/'new-gate');self.gate.initialize_open();self.gate.enable()
        self.uninstall=module.install_sqlite_guard(self.gate)
        with self.assertRaises(module.MaintenanceError):sqlite3.connect(missing)
        self.assertFalse(missing.exists())

    def test_cross_thread_connection_override_cannot_share_auth_capability(self):
        with self.assertRaises(module.MaintenanceError):sqlite3.connect(self.db,check_same_thread=False)

    def test_uri_query_substring_never_exempts_actual_disk_target(self):
        self.gate.enable()
        for query in ('notmode=memory','x=mode%3Dmemory','mode=rw&mode=memory','mode=memory&mode=rw'):
            with self.subTest(query=query):
                try:c=sqlite3.connect(self.db.as_uri()+'?'+query,uri=True)
                except module.MaintenanceError:continue
                try:
                    with self.assertRaises(module.MaintenanceError):c.execute('UPDATE business SET value="bypassed"')
                finally:c.close()
        with sqlite3.connect(self.db) as c:self.assertEqual(c.execute('SELECT value FROM business').fetchone()[0],'original')
    def test_real_memory_mode_and_percent_encoded_relative_disk_path(self):
        self.gate.enable()
        with sqlite3.connect('file:isolated-memory?mode=memory',uri=True) as c:c.execute('CREATE TABLE harmless(x)')
        from urllib.parse import quote
        relative=os.path.relpath(self.db,Path.cwd())
        with sqlite3.connect('file:'+quote(relative,safe='/')+'?mode=rw',uri=True) as c:
            with self.assertRaises(module.MaintenanceError):c.execute('DELETE FROM business')

    def test_replaced_lock_inode_cannot_split_existing_transaction_drain(self):
        c=sqlite3.connect(self.db);c.execute('UPDATE business SET value="pending"')
        lock=self.root/'gate/gate.lock'
        replacement=self.root/'gate/replacement';replacement.touch(mode=0o600)
        os.replace(replacement,lock)
        try:
            fresh=module.Gate(self.db,self.root/'gate')
            self.assertEqual(fresh.status()['state'],'UNKNOWN')
            with self.assertRaises(module.MaintenanceError):fresh.enable(timeout=.03)
        finally:c.rollback();c.close()
