"""Exact migration 039 success path under the production-equivalent write gate.

All files, SQLite state and process authorities are synthetic Staging fixtures.
"""
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
from types import SimpleNamespace
import unittest

from business_write_gate import Gate
from tools.production_activation_control import ActivationController, RollbackTarget
from tools.production_gated_release_plan import GatedReleasePlan
from tools.production_release_transaction import MigrationStep, ReleaseSnapshot, migration_versions
from tools.production_write_gate_session import finish_gate
from tests.test_production_visa_039_gated_integration import (
    ClosedGateBackend, Exact039Delegate, PREVIOUS_SHA, ROOT, VISA_SHA, VISA_SOURCE,
)


class HealthyBackend(ClosedGateBackend):
    def health(self, role):
        return {"ok": True, "business_write_gate": self.gate.status()}


class Exact039SuccessTests(unittest.TestCase):
    def setUp(self):
        base = ROOT / "SAFEBOX/release-control-staging-20260914/evidence/visa-039-success"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "migration-source"
        (self.source / "migrations/checks").mkdir(parents=True)
        shutil.copyfile(VISA_SOURCE / "migration_runner.py", self.source / "migration_runner.py")
        self.git("init", "-q")
        self.git("config", "user.name", "Visa 039 Success Isolation")
        self.git("config", "user.email", "visa-039-success@invalid.local")

    def git(self, *args):
        return subprocess.run(
            ["/usr/bin/git", "-C", str(self.source), *args], check=True,
            capture_output=True, text=True,
        ).stdout.strip()

    def package(self, version, name, sql, check="SELECT 1;"):
        stem = version + "_" + name
        (self.source / "migrations" / (stem + ".sql")).write_text(sql)
        (self.source / "migrations/checks" / (stem + "_forward_check.sql")).write_text(check)

    def commit(self, message):
        self.git("add", "migration_runner.py", "migrations")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def database(self):
        path = self.root / "database/aaie_crm.db"
        path.parent.mkdir()
        with closing(sqlite3.connect(path)) as connection:
            connection.executescript("""
                PRAGMA foreign_keys=ON;
                CREATE TABLE users(id INTEGER PRIMARY KEY,username TEXT,password_hash TEXT,role_id INTEGER,last_login_at TEXT,last_login_ip TEXT);
                CREATE TABLE sessions(token TEXT PRIMARY KEY,user_id INTEGER REFERENCES users(id),csrf_token TEXT,expires_at TEXT,created_at TEXT,last_seen_at TEXT);
                CREATE TABLE audit_logs(id INTEGER PRIMARY KEY,user_id INTEGER,username TEXT,action_type TEXT,module_name TEXT,module_key TEXT,record_id TEXT,file_path TEXT,old_value TEXT,new_value TEXT,student_id TEXT,contract_id TEXT,status TEXT,error_message TEXT,created_at TEXT);
                CREATE TABLE business(id INTEGER PRIMARY KEY,value TEXT);
                CREATE TABLE visa_cases(id INTEGER PRIMARY KEY);
                CREATE TABLE visa_case_documents(id INTEGER PRIMARY KEY,visa_case_id INTEGER REFERENCES visa_cases(id),archived_at TEXT);
                INSERT INTO users VALUES(1,'synthetic','hash',1,NULL,NULL);
                INSERT INTO sessions VALUES('before',1,'csrf','future','past','past');
                INSERT INTO audit_logs(id,action_type,module_name,module_key,created_at) VALUES(1,'update','business','business','past');
                INSERT INTO business VALUES(1,'unchanged');
            """)
        return path

    def test_exact_039_success_closes_gate_migrates_starts_pair_and_reopens(self):
        source_head = self.git_external(VISA_SOURCE, "rev-parse", "HEAD")
        self.assertEqual(VISA_SHA, source_head)
        self.assertEqual(
            (VISA_SOURCE / "migrations/20260914_039_visa_result_status_authority.sql").read_bytes(),
            (ROOT / "migrations/20260914_039_visa_result_status_authority.sql").read_bytes(),
        )
        self.package("20260909_037", "synthetic_predecessor_one", "CREATE TABLE synthetic_037(id INTEGER PRIMARY KEY);")
        self.package("20260909_038", "synthetic_predecessor_two", "CREATE TABLE synthetic_038(id INTEGER PRIMARY KEY);")
        predecessor = self.commit("genuine predecessor applications")
        database = self.database()
        MigrationStep(self.source, predecessor, ("20260909_037", "20260909_038")).apply(database)
        for relative in (
            "migrations/20260914_039_visa_result_status_authority.sql",
            "migrations/checks/20260914_039_visa_result_status_authority_forward_check.sql",
        ):
            target = self.source / relative
            target.write_bytes((VISA_SOURCE / relative).read_bytes())
        candidate = self.commit("exact Visa Candidate 039 SQL")
        step = MigrationStep(
            self.source, candidate, ("20260914_039",),
            required_predecessors=("20260909_037", "20260909_038"),
            allow_already_applied=True,
        )
        gate = Gate(database, self.root / "gate")
        gate.initialize_open()
        closed = gate.enable()
        checks = []
        def assert_closed():
            state = gate.status(); checks.append(state)
            if state != closed:
                raise RuntimeError("gate_not_closed")
            return state
        backups = self.root / "backups"; backups.mkdir()
        snapshot = ReleaseSnapshot.create(database, backups / "release", backup_root=backups, assert_quiescent=lambda: None)
        target = RollbackTarget(
            PREVIOUS_SHA, candidate, ("parent", "admin"), (("parent", 101), ("admin", 102)),
            str(self.root), (), (), ("20260909_037", "20260909_038"),
            snapshot.manifest_sha256, "restore_backup_after_migration",
            (("parent", False), ("admin", False)),
        )
        backend = HealthyBackend(gate, database, PREVIOUS_SHA, candidate)
        delegate = Exact039Delegate(target=target, snapshot=snapshot, migration_step=step, backend=backend, evidence_dir=self.root / "plan")
        delegate.evidence_dir.mkdir()
        plan = GatedReleasePlan(delegate, assert_gate=assert_closed)
        def event(item):
            assert_closed()
            if item["state"] == "START_CANDIDATE":
                plan.mark_application_attempt()
        result = ActivationController(backend, timeout_seconds=1, stop_timeout_seconds=1, poll_seconds=0.25, event_sink=event).run(plan)
        self.assertEqual("READY", result["status"])
        self.assertEqual(candidate, backend.head())
        result["sha"] = candidate
        self.assertTrue(plan.application_attempted)
        self.assertGreater(len(checks), 10)
        self.assertEqual(("20260909_037", "20260909_038", "20260914_039"), migration_versions(database))
        aggregate = plan.verify_runtime_database(candidate)
        result["aggregate_evidence"] = aggregate["aggregate_evidence"]
        class Lease:
            def assert_held(self): pass
        adapter = SimpleNamespace(
            phase="visa", active_plan=plan, backend=backend, gate=gate,
            snapshot={"source_commit": candidate}, lease=Lease(),
            completion_sha256=None, assert_gate=assert_closed,
            _authority=lambda head, source: (head, source),
        )
        def capture_running(plan, label):
            from tools.wal_aware_isolated_snapshot import create_and_verify
            destination = self.root / "finish-capture/snapshot.db"
            receipt = create_and_verify(database, destination, assert_quiescent=assert_closed)
            return {**receipt, "isolated_database_path": str(destination)}
        adapter.capture_running_database_evidence = capture_running
        smoke = {"status": "PASS", "production_sha": candidate, "read_only": True, "required_checks_passed": True}
        final = finish_gate(adapter, result, smoke)
        self.assertEqual("OPEN", gate.status()["state"])
        self.assertTrue(final["normal_business_operations_restored"])
        self.assertEqual("unchanged", sqlite3.connect(database).execute("SELECT value FROM business").fetchone()[0])

    @staticmethod
    def git_external(repo, *args):
        return subprocess.run(
            ["/usr/bin/git", "-C", str(repo), *args], check=True,
            capture_output=True, text=True,
        ).stdout.strip()


if __name__ == "__main__":
    unittest.main()
