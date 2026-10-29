"""Exact Visa 039 gated rollback integration on synthetic data only."""
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
from tools.production_activation_control import (
    ActivationController, ReleasePlan, RollbackTarget,
)
from tools.production_gated_release_plan import GatedReleasePlan
from tools.production_graceful_control import Observation
from tools.production_release_transaction import (
    MigrationStep, ReleaseSnapshot, database_evidence, migration_versions,
)
from tools.production_write_gate_session import finish_gate


ROOT = Path(__file__).resolve().parents[1]
VISA_SOURCE = ROOT
VISA_SHA = subprocess.run(["/usr/bin/git", "-C", str(ROOT), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
PREVIOUS_SHA = "a" * 40


class ClosedGateBackend:
    def __init__(self, gate, database, previous, candidate):
        self.gate = gate
        self.database = database
        self.current = previous
        self.previous = previous
        self.candidate = candidate
        self.jobs = {"parent": None, "admin": None}
        self.clock = 0.0
        self.auth_written = False

    def bind(self, target): self.target = target
    def head(self): return self.current
    def monotonic(self): return self.clock
    def sleep(self, value): self.clock += value
    def capture_owned(self, role, observation): pass
    def inspect(self, role):
        pid = self.jobs[role]
        if pid is None:
            return Observation(True, None, (), None, False, 0)
        return Observation(True, pid, (pid,), "synthetic-birth-" + str(pid), True, 0, "python")
    def health_success(self, role, payload): return payload.get("ok") is True
    def health(self, role):
        failed_candidate = self.current == self.candidate and role == "parent"
        return {"ok": not failed_candidate, "business_write_gate": self.gate.status()}
    def start(self, role):
        self.jobs[role] = (201 if role == "parent" else 202) if self.current == self.candidate else (301 if role == "parent" else 302)
        if self.current == self.candidate and not self.auth_written:
            with closing(sqlite3.connect(self.database)) as connection:
                connection.execute("UPDATE users SET last_login_at='candidate-login' WHERE id=1")
                connection.execute(
                    "INSERT INTO audit_logs(id,user_id,username,action_type,module_name,module_key,status,created_at) "
                    "VALUES(2,1,'synthetic','login_success','auth','auth','success','candidate-login')"
                )
                connection.commit()
            self.auth_written = True
    def stop_failed(self, role, original_pid, expected_signature, timeout): self.jobs[role] = None
    def assert_quiet(self, services):
        if any(self.jobs[role] is not None for role in services):
            raise RuntimeError("synthetic_services_not_quiet")
    def restore_disabled(self): pass


class Exact039Delegate(ReleasePlan):
    def __init__(self, *, target, snapshot, migration_step, backend, evidence_dir):
        super().__init__(target)
        self.snapshot = snapshot
        self.migration_step = migration_step
        self.backend = backend
        self.evidence_dir = evidence_dir
        self.post_cutover_evidence = None

    def validate(self):
        self.backend.assert_quiet(self.target.services)
        self.snapshot.verify()
        self.verify(self.target.previous_sha)

    def cutover(self):
        self.migration_step.apply(self.snapshot.database)
        self.post_cutover_evidence = database_evidence(self.snapshot.database)
        self.backend.current = self.target.candidate_sha

    def restore(self):
        self.snapshot.restore(assert_quiescent=lambda: self.backend.assert_quiet(self.target.services))
        self.backend.current = self.target.previous_sha

    def verify(self, head):
        if self.backend.current != head:
            raise RuntimeError("synthetic_runtime_head_mismatch")
        expected = self.target.previous_migrations
        if head == self.target.candidate_sha:
            expected += self.migration_step.versions
        if migration_versions(self.snapshot.database) != expected:
            raise RuntimeError("synthetic_registry_mismatch")


class Visa039GatedIntegrationTests(unittest.TestCase):
    def setUp(self):
        base = ROOT / "SAFEBOX/visa-039-release-controller-20260914/integration"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "migration-source"
        (self.source / "migrations/checks").mkdir(parents=True)
        shutil.copyfile(VISA_SOURCE / "migration_runner.py", self.source / "migration_runner.py")
        self.git("init", "-q")
        self.git("config", "user.name", "Visa 039 Isolation")
        self.git("config", "user.email", "visa-039@invalid.local")

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

    def test_exact_039_startup_failure_rolls_back_under_closed_gate_then_finish_opens(self):
        source_head = subprocess.run(
            ["/usr/bin/git", "-C", str(VISA_SOURCE), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        self.assertEqual(source_head, VISA_SHA)
        subprocess.run(
            ["/usr/bin/git", "-C", str(VISA_SOURCE), "diff", "--quiet", "HEAD", "--"],
            check=True,
        )
        subprocess.run(
            ["/usr/bin/git", "-C", str(VISA_SOURCE), "diff", "--cached", "--quiet", "--"],
            check=True,
        )
        self.package("20260909_037", "synthetic_predecessor_one", "CREATE TABLE synthetic_037(id INTEGER PRIMARY KEY);")
        self.package("20260909_038", "synthetic_predecessor_two", "CREATE TABLE synthetic_038(id INTEGER PRIMARY KEY);")
        predecessor_head = self.commit("genuine predecessor applications")
        database = self.database()
        MigrationStep(self.source, predecessor_head, ("20260909_037", "20260909_038")).apply(database)

        visa_sql = (VISA_SOURCE / "migrations/20260914_039_visa_result_status_authority.sql").read_bytes()
        visa_check = (VISA_SOURCE / "migrations/checks/20260914_039_visa_result_status_authority_forward_check.sql").read_bytes()
        (self.source / "migrations/20260914_039_visa_result_status_authority.sql").write_bytes(visa_sql)
        (self.source / "migrations/checks/20260914_039_visa_result_status_authority_forward_check.sql").write_bytes(visa_check)
        candidate_head = self.commit("exact Visa Candidate 039 SQL")
        step = MigrationStep(
            self.source, candidate_head, ("20260914_039",),
            required_predecessors=("20260909_037", "20260909_038"),
            allow_already_applied=True,
        )

        gate = Gate(database, self.root / "gate")
        gate.initialize_open()
        closed = gate.enable()
        gate_checks = []
        def assert_closed():
            state = gate.status(); gate_checks.append(state)
            if state != closed:
                raise RuntimeError("gate_not_closed")
            return state

        backups = self.root / "backups"; backups.mkdir()
        snapshot = ReleaseSnapshot.create(
            database, backups / "release", backup_root=backups,
            assert_quiescent=lambda: None,
        )
        target = RollbackTarget(
            PREVIOUS_SHA, candidate_head, ("parent", "admin"),
            (("parent", 101), ("admin", 102)), str(self.root), (), (),
            ("20260909_037", "20260909_038"), snapshot.manifest_sha256,
            "restore_backup_after_migration", (("parent", False), ("admin", False)),
        )
        backend = ClosedGateBackend(gate, database, PREVIOUS_SHA, candidate_head)
        delegate = Exact039Delegate(
            target=target, snapshot=snapshot, migration_step=step, backend=backend,
            evidence_dir=self.root / "plan",
        )
        delegate.evidence_dir.mkdir()
        plan = GatedReleasePlan(delegate, assert_gate=assert_closed)
        def event(item):
            assert_closed()
            if item["state"] == "START_CANDIDATE":
                plan.mark_application_attempt()
        result = ActivationController(
            backend, timeout_seconds=1, stop_timeout_seconds=1,
            poll_seconds=0.25, event_sink=event,
        ).run(plan)
        self.assertEqual(result["status"], "ROLLED_BACK")
        self.assertTrue(backend.auth_written, result)
        self.assertIn("START_CANDIDATE", result["states"])
        self.assertTrue(plan.application_attempted)
        self.assertEqual(
            json.loads((snapshot.manifest.parent / "RESTORE_ATTEMPT.json").read_text())["kind"],
            "auth_preserving_gated_restore",
        )
        self.assertEqual(gate.status(), closed)
        self.assertGreater(len(gate_checks), 10)
        self.assertEqual(migration_versions(database), ("20260909_037", "20260909_038"))
        with closing(sqlite3.connect(database)) as connection:
            self.assertIsNone(connection.execute("SELECT 1 FROM sqlite_master WHERE name='visa_result_status_events'").fetchone())
            self.assertIsNone(connection.execute("SELECT 1 FROM sqlite_master WHERE name='visa_result_status_current'").fetchone())
            self.assertEqual(connection.execute("SELECT value FROM business").fetchone()[0], "unchanged")
            self.assertEqual(connection.execute("SELECT last_login_at FROM users WHERE id=1").fetchone()[0], "candidate-login")
            self.assertEqual(connection.execute("SELECT count(*) FROM audit_logs").fetchone()[0], 2)

        aggregate = plan.verify_runtime_database(PREVIOUS_SHA)
        release_result = {
            **result, "sha": PREVIOUS_SHA,
            "aggregate_evidence": aggregate["aggregate_evidence"],
        }
        class Lease:
            def assert_held(self): pass
        adapter = SimpleNamespace(
            phase="visa", active_plan=plan, backend=backend, gate=gate,
            snapshot={"source_commit": candidate_head}, lease=Lease(),
            completion_sha256=None, assert_gate=assert_closed,
            _authority=lambda head, source: (head, source),
        )
        smoke_path = self.root / "READONLY_SMOKE.json"
        smoke = {
            "status": "PASS", "production_sha": PREVIOUS_SHA,
            "read_only": True, "required_checks_passed": True,
        }
        smoke_path.write_text(json.dumps(smoke))
        final = finish_gate(adapter, release_result, smoke)
        self.assertEqual(final["status"], "ROLLED_BACK")
        self.assertEqual(gate.status()["state"], "OPEN")
        self.assertTrue(final["normal_business_operations_restored"])


if __name__ == "__main__": unittest.main()
