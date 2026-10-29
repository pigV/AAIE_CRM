import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path


NOW = "2026-08-13T03:00:00+10:00"


def build_post_020_fixture(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(
            """
            CREATE TABLE users(
              id INTEGER PRIMARY KEY,
              username TEXT,
              is_active INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE students(student_id TEXT PRIMARY KEY);
            CREATE TABLE student_case_sequences(
              student_id TEXT NOT NULL REFERENCES students(student_id),
              case_type TEXT NOT NULL CHECK(case_type IN ('transfer','progression')),
              next_sequence_no INTEGER NOT NULL CHECK(next_sequence_no > 0),
              updated_at TEXT NOT NULL,
              PRIMARY KEY(student_id,case_type)
            );
            CREATE TABLE student_cases(
              id INTEGER PRIMARY KEY,
              student_id TEXT NOT NULL REFERENCES students(student_id),
              case_type TEXT NOT NULL CHECK(case_type IN ('transfer','progression')),
              sequence_no INTEGER NOT NULL CHECK(sequence_no > 0),
              status TEXT NOT NULL CHECK(status IN (
                'draft','preparing','in_progress','waiting','paused',
                'completed','cancelled','failed','archived'
              )),
              terminal_result TEXT CHECK(
                terminal_result IS NULL OR terminal_result IN ('completed','cancelled','failed')
              ),
              active_slot INTEGER NOT NULL CHECK(active_slot IN (0,1)),
              started_at TEXT,
              paused_at TEXT,
              completed_at TEXT,
              cancelled_at TEXT,
              failed_at TEXT,
              archived_at TEXT,
              created_by INTEGER NOT NULL REFERENCES users(id),
              created_at TEXT NOT NULL,
              updated_by INTEGER NOT NULL REFERENCES users(id),
              updated_at TEXT NOT NULL,
              UNIQUE(student_id,case_type,sequence_no),
              CHECK(
                (status='draft' AND active_slot=0 AND terminal_result IS NULL)
                OR (status IN ('preparing','in_progress','waiting','paused')
                    AND active_slot=1 AND terminal_result IS NULL)
                OR (status IN ('completed','cancelled','failed')
                    AND active_slot=0 AND terminal_result=status)
                OR (status='archived' AND active_slot=0
                    AND terminal_result IN ('completed','cancelled','failed'))
              )
            );
            CREATE UNIQUE INDEX uq_student_cases_one_active_per_student
              ON student_cases(student_id) WHERE active_slot=1;
            CREATE TABLE student_case_assignments(
              id INTEGER PRIMARY KEY,
              case_id INTEGER NOT NULL REFERENCES student_cases(id),
              user_id INTEGER NOT NULL REFERENCES users(id),
              role_type TEXT NOT NULL,
              effective_from TEXT NOT NULL,
              effective_to TEXT,
              created_by INTEGER NOT NULL REFERENCES users(id),
              created_at TEXT NOT NULL,
              ended_by INTEGER REFERENCES users(id)
            );
            CREATE TABLE case_events(
              id INTEGER PRIMARY KEY,
              case_id INTEGER NOT NULL REFERENCES student_cases(id),
              event_type TEXT NOT NULL,
              entity_type TEXT,
              entity_id TEXT,
              actor_type TEXT NOT NULL,
              actor_user_id INTEGER REFERENCES users(id),
              old_value_json TEXT,
              new_value_json TEXT,
              metadata_json TEXT,
              correlation_id TEXT,
              request_id TEXT,
              correction_of_event_id INTEGER REFERENCES case_events(id),
              created_at TEXT NOT NULL
            );
            CREATE TABLE application_cycles(
              id INTEGER PRIMARY KEY,
              cycle_uid TEXT NOT NULL UNIQUE,
              student_id TEXT REFERENCES students(student_id),
              parent_form_token TEXT,
              source_student_case_id INTEGER REFERENCES student_cases(id),
              cycle_type TEXT NOT NULL CHECK(cycle_type IN ('initial','transfer','progression','future')),
              sequence_no INTEGER NOT NULL CHECK(sequence_no > 0),
              status TEXT NOT NULL CHECK(status IN ('active','closed')),
              outcome TEXT CHECK(outcome IS NULL OR outcome IN (
                'granted','refused','completed','withdrawn','cancelled',
                'superseded_by_transfer','superseded_by_progression')),
              opened_at TEXT NOT NULL,
              closed_at TEXT,
              created_by_user_id INTEGER REFERENCES users(id),
              created_by_name TEXT,
              created_at TEXT NOT NULL,
              updated_by_user_id INTEGER REFERENCES users(id),
              updated_by_name TEXT,
              updated_at TEXT NOT NULL,
              CHECK(
                (status='active' AND outcome IS NULL AND closed_at IS NULL)
                OR (status='closed' AND outcome IS NOT NULL AND closed_at IS NOT NULL)
              )
            );
            CREATE UNIQUE INDEX uq_application_cycles_active_student
              ON application_cycles(student_id)
              WHERE status='active' AND student_id IS NOT NULL;
            CREATE UNIQUE INDEX uq_application_cycles_student_sequence
              ON application_cycles(student_id,sequence_no)
              WHERE student_id IS NOT NULL;
            CREATE UNIQUE INDEX uq_application_cycles_student_case
              ON application_cycles(source_student_case_id)
              WHERE source_student_case_id IS NOT NULL;
            CREATE TABLE student_lifecycle_idempotency_receipts(
              id INTEGER PRIMARY KEY,
              student_id TEXT NOT NULL REFERENCES students(student_id),
              operation TEXT NOT NULL,
              idempotency_key TEXT NOT NULL,
              payload_sha256 TEXT NOT NULL,
              result_case_id INTEGER REFERENCES student_cases(id),
              result_cycle_id INTEGER REFERENCES application_cycles(id),
              result_json TEXT NOT NULL,
              actor_user_id INTEGER REFERENCES users(id),
              created_at TEXT NOT NULL,
              UNIQUE(student_id,operation,idempotency_key)
            );
            INSERT INTO users(id,username) VALUES(1,'operator');
            INSERT INTO students(student_id) VALUES('S1');
            """
        )


class StudentLifecycleContractTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "lifecycle.db"
        build_post_020_fixture(self.database_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def service(self):
        from student_lifecycle.service import StudentLifecycleService

        return StudentLifecycleService(
            self.database_path,
            clock=lambda: NOW,
        )

    def create_draft(self, lifecycle_type: str, key: str):
        return self.service().create_lifecycle_draft(
            student_id="S1",
            lifecycle_type=lifecycle_type,
            actor_user_id=1,
            owner_user_id=1,
            idempotency_key=key,
        )

    def test_multiple_transfer_progression_drafts_create_no_cycles(self):
        drafts = (
            self.create_draft("transfer", "draft-transfer-1"),
            self.create_draft("transfer", "draft-transfer-2"),
            self.create_draft("progression", "draft-progression-1"),
            self.create_draft("progression", "draft-progression-2"),
        )

        self.assertEqual(
            [("transfer", 0), ("transfer", 0), ("progression", 0), ("progression", 0)],
            [(draft.lifecycle_type, draft.active_slot) for draft in drafts],
        )
        with sqlite3.connect(self.database_path) as connection:
            self.assertEqual(4, connection.execute("SELECT count(*) FROM student_cases").fetchone()[0])
            self.assertEqual(0, connection.execute("SELECT count(*) FROM application_cycles").fetchone()[0])

    def test_activation_creates_one_cycle_and_replays_same_key(self):
        from student_lifecycle.models import LifecycleActivationDTO

        draft = self.create_draft("transfer", "draft-transfer")

        first = self.service().activate_lifecycle(
            case_id=draft.case_id,
            actor_user_id=1,
            idempotency_key="activate-transfer",
        )
        replay = self.service().activate_lifecycle(
            case_id=draft.case_id,
            actor_user_id=1,
            idempotency_key="activate-transfer",
        )

        self.assertEqual(first, replay)
        self.assertIsInstance(first, LifecycleActivationDTO)
        self.assertEqual(("transfer", 1, "active"), (first.lifecycle_type, first.active_slot, first.cycle_status))
        with sqlite3.connect(self.database_path) as connection:
            self.assertEqual(1, connection.execute("SELECT count(*) FROM application_cycles").fetchone()[0])

    def test_existing_initial_cycle_blocks_transfer_activation_without_mutation(self):
        from student_lifecycle.errors import LifecycleActiveConflictError

        draft = self.create_draft("transfer", "draft-transfer")
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """INSERT INTO application_cycles(
                     cycle_uid,student_id,cycle_type,sequence_no,status,opened_at,
                     created_by_user_id,created_at,updated_at
                   ) VALUES('initial-cycle','S1','initial',1,'active',?,?,?,?)""",
                (NOW, 1, NOW, NOW),
            )

        with self.assertRaises(LifecycleActiveConflictError):
            self.service().activate_lifecycle(
                case_id=draft.case_id,
                actor_user_id=1,
                idempotency_key="activate-transfer",
            )

        with sqlite3.connect(self.database_path) as connection:
            self.assertEqual(("draft", 0), connection.execute(
                "SELECT status,active_slot FROM student_cases WHERE id=?", (draft.case_id,)
            ).fetchone())
            self.assertEqual(0, connection.execute(
                "SELECT count(*) FROM student_lifecycle_idempotency_receipts WHERE operation='activate'"
            ).fetchone()[0])

    def test_existing_active_case_without_cycle_blocks_activation_fail_closed(self):
        from student_lifecycle.errors import LifecycleActiveConflictError

        draft = self.create_draft("progression", "draft-progression")
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """INSERT INTO student_cases(
                     student_id,case_type,sequence_no,status,active_slot,
                     started_at,created_by,created_at,updated_by,updated_at
                   ) VALUES('S1','transfer',99,'preparing',1,?,1,?,1,?)""",
                (NOW, NOW, NOW),
            )

        with self.assertRaises(LifecycleActiveConflictError):
            self.service().activate_lifecycle(
                case_id=draft.case_id,
                actor_user_id=1,
                idempotency_key="activate-with-orphan-active-case",
            )

        with sqlite3.connect(self.database_path) as connection:
            self.assertEqual(("draft", 0), connection.execute(
                "SELECT status,active_slot FROM student_cases WHERE id=?",
                (draft.case_id,),
            ).fetchone())
            self.assertEqual(0, connection.execute(
                "SELECT count(*) FROM application_cycles"
            ).fetchone()[0])

    def test_activation_gateway_failure_rolls_back_case_and_receipt(self):
        from student_lifecycle.errors import LifecycleIntegrityError

        draft = self.create_draft("progression", "draft-progression")
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """CREATE TRIGGER reject_cycle_insert BEFORE INSERT ON application_cycles
                   BEGIN SELECT RAISE(ABORT,'cycle insert rejected'); END"""
            )

        with self.assertRaises(LifecycleIntegrityError):
            self.service().activate_lifecycle(
                case_id=draft.case_id,
                actor_user_id=1,
                idempotency_key="activate-progression",
            )

        with sqlite3.connect(self.database_path) as connection:
            self.assertEqual(("draft", 0), connection.execute(
                "SELECT status,active_slot FROM student_cases WHERE id=?", (draft.case_id,)
            ).fetchone())
            self.assertEqual(0, connection.execute("SELECT count(*) FROM application_cycles").fetchone()[0])
            self.assertEqual(0, connection.execute(
                "SELECT count(*) FROM student_lifecycle_idempotency_receipts WHERE operation='activate'"
            ).fetchone()[0])

    def test_case_cycle_type_mismatch_fails_closed_without_mutation(self):
        from student_lifecycle.errors import LifecycleIntegrityError

        draft = self.create_draft("transfer", "mismatch-draft")
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """INSERT INTO application_cycles(
                     cycle_uid,student_id,source_student_case_id,cycle_type,
                     sequence_no,status,outcome,opened_at,closed_at,
                     created_by_user_id,created_at,updated_at
                   ) VALUES('mismatch','S1',?,'progression',1,'closed',
                            'cancelled',?,?,1,?,?)""",
                (draft.case_id, NOW, NOW, NOW, NOW),
            )

        with self.assertRaises(LifecycleIntegrityError):
            self.service().activate_lifecycle(
                case_id=draft.case_id, actor_user_id=1,
                idempotency_key="activate-mismatch",
            )
        with sqlite3.connect(self.database_path) as connection:
            self.assertEqual(("draft", 0), connection.execute(
                "SELECT status,active_slot FROM student_cases WHERE id=?",
                (draft.case_id,),
            ).fetchone())

    def test_active_cancel_gateway_failure_rolls_back_case_and_receipt(self):
        from student_lifecycle.errors import LifecycleIntegrityError

        draft = self.create_draft("transfer", "cancel-rollback-draft")
        active = self.service().activate_lifecycle(
            case_id=draft.case_id, actor_user_id=1,
            idempotency_key="cancel-rollback-activate",
        )
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """CREATE TRIGGER reject_cycle_close
                   BEFORE UPDATE OF status ON application_cycles
                   BEGIN SELECT RAISE(ABORT,'cycle close rejected'); END"""
            )

        with self.assertRaises(LifecycleIntegrityError):
            self.service().cancel_lifecycle(
                case_id=draft.case_id, actor_user_id=1,
                reason="should roll back", idempotency_key="cancel-rollback",
            )
        with sqlite3.connect(self.database_path) as connection:
            self.assertEqual(("preparing", 1), connection.execute(
                "SELECT status,active_slot FROM student_cases WHERE id=?",
                (draft.case_id,),
            ).fetchone())
            self.assertEqual(("active", None), connection.execute(
                "SELECT status,outcome FROM application_cycles WHERE id=?",
                (active.cycle_id,),
            ).fetchone())
            self.assertEqual(0, connection.execute(
                "SELECT count(*) FROM student_lifecycle_idempotency_receipts WHERE operation='cancel'"
            ).fetchone()[0])

    def test_draft_cancel_is_case_only_and_active_cancel_closes_cycle(self):
        draft_only = self.create_draft("transfer", "draft-only")
        cancelled_draft = self.service().cancel_lifecycle(
            case_id=draft_only.case_id,
            actor_user_id=1,
            reason="draft withdrawn",
            idempotency_key="cancel-draft",
        )
        active_draft = self.create_draft("progression", "active-draft")
        activated = self.service().activate_lifecycle(
            case_id=active_draft.case_id,
            actor_user_id=1,
            idempotency_key="activate-progression",
        )
        cancelled_active = self.service().cancel_lifecycle(
            case_id=active_draft.case_id,
            actor_user_id=1,
            reason="lifecycle withdrawn",
            idempotency_key="cancel-progression",
        )

        self.assertIsNone(cancelled_draft.cycle_id)
        self.assertEqual("cancelled", cancelled_draft.case_status)
        self.assertEqual(activated.cycle_id, cancelled_active.cycle_id)
        self.assertEqual("closed", cancelled_active.cycle_status)
        self.assertEqual("cancelled", cancelled_active.cycle_outcome)

    def test_same_key_changed_payload_is_explicit_conflict(self):
        from student_lifecycle.errors import LifecycleIdempotencyConflictError

        self.create_draft("transfer", "same-draft-key")
        with self.assertRaises(LifecycleIdempotencyConflictError):
            self.create_draft("progression", "same-draft-key")

    def test_activation_receipt_replays_original_result_after_later_transition(self):
        draft = self.create_draft("transfer", "draft-for-stable-replay")
        activated = self.service().activate_lifecycle(
            case_id=draft.case_id,
            actor_user_id=1,
            idempotency_key="activate-for-stable-replay",
        )
        self.service().complete_operational_case(
            case_id=draft.case_id,
            actor_user_id=1,
            idempotency_key="complete-after-activation",
        )

        replay = self.service().activate_lifecycle(
            case_id=draft.case_id,
            actor_user_id=1,
            idempotency_key="activate-for-stable-replay",
        )

        self.assertEqual(activated, replay)
        self.assertEqual("preparing", replay.case_status)
        self.assertEqual(1, replay.active_slot)
        self.assertEqual("active", replay.cycle_status)

    def test_operational_completion_does_not_close_canonical_lifecycle(self):
        draft = self.create_draft("progression", "draft-for-completion")
        active = self.service().activate_lifecycle(
            case_id=draft.case_id, actor_user_id=1,
            idempotency_key="activate-for-completion",
        )
        completed = self.service().complete_operational_case(
            case_id=draft.case_id, actor_user_id=1,
            idempotency_key="complete-operational",
        )

        self.assertEqual("completed", completed.case_status)
        self.assertEqual("active", completed.cycle_status)
        self.assertEqual(active.cycle_id, completed.cycle_id)
        closed = self.service().close_lifecycle(
            case_id=draft.case_id, outcome="completed", actor_user_id=1,
            idempotency_key="close-canonical",
        )
        self.assertEqual(("closed", "completed"), (closed.cycle_status, closed.cycle_outcome))

    def test_canonical_close_rejects_operationally_active_case_without_mutation(self):
        from student_lifecycle.errors import LifecycleActiveConflictError

        draft = self.create_draft("transfer", "draft-for-early-close")
        active = self.service().activate_lifecycle(
            case_id=draft.case_id,
            actor_user_id=1,
            idempotency_key="activate-for-early-close",
        )

        with self.assertRaises(LifecycleActiveConflictError):
            self.service().close_lifecycle(
                case_id=draft.case_id,
                outcome="completed",
                actor_user_id=1,
                idempotency_key="close-too-early",
            )

        unchanged = self.service().get_active_lifecycle("S1")
        self.assertEqual(draft.case_id, unchanged.case_id)
        self.assertEqual("preparing", unchanged.case_status)
        self.assertEqual(1, unchanged.active_slot)
        self.assertEqual(active.cycle_id, unchanged.cycle_id)
        self.assertEqual("active", unchanged.cycle_status)

    def test_queries_keep_drafts_out_of_current_lifecycle(self):
        transfer = self.create_draft("transfer", "query-transfer")
        progression = self.create_draft("progression", "query-progression")
        self.assertIsNone(self.service().get_active_lifecycle("S1"))
        self.service().activate_lifecycle(
            case_id=progression.case_id, actor_user_id=1,
            idempotency_key="query-activate",
        )
        current = self.service().get_active_lifecycle("S1")
        self.assertEqual(progression.case_id, current.case_id)
        self.assertEqual(2, len(self.service().list_student_lifecycles("S1")))
        self.assertEqual("draft", self.service().get_lifecycle_context(transfer.case_id)["context_status"])
        self.assertEqual("current", self.service().get_lifecycle_context(progression.case_id)["context_status"])

    def test_queries_include_initial_cycles_without_student_cases(self):
        with sqlite3.connect(self.database_path) as connection:
            cursor = connection.execute(
                """INSERT INTO application_cycles(
                     cycle_uid,student_id,cycle_type,sequence_no,status,opened_at,
                     created_by_user_id,created_at,updated_at
                   ) VALUES('initial-cycle','S1','initial',1,'active',?,?,?,?)""",
                (NOW, 1, NOW, NOW),
            )
            initial_cycle_id = int(cursor.lastrowid)

        transfer = self.create_draft("transfer", "query-transfer-after-initial")

        current = self.service().get_active_lifecycle("S1")
        self.assertIsNotNone(current)
        self.assertEqual("initial", current.lifecycle_type)
        self.assertIsNone(current.case_id)
        self.assertEqual(initial_cycle_id, current.cycle_id)
        self.assertEqual(1, current.cycle_sequence_no)

        lifecycles = self.service().list_student_lifecycles("S1")
        self.assertEqual(
            [("initial", initial_cycle_id), ("transfer", None)],
            [(item.lifecycle_type, item.cycle_id) for item in lifecycles],
        )
        self.assertEqual(transfer.case_id, lifecycles[1].case_id)

        context = self.service().get_lifecycle_context(cycle_id=initial_cycle_id)
        self.assertEqual("current", context["context_status"])
        self.assertEqual("initial", context["lifecycle_type"])
        self.assertEqual(1, context["cycle_sequence_no"])

    def test_concurrent_activation_allows_exactly_one_active_cycle(self):
        transfer = self.create_draft("transfer", "concurrent-transfer")
        progression = self.create_draft("progression", "concurrent-progression")
        barrier = threading.Barrier(2)
        results = []
        lock = threading.Lock()

        def activate(case_id, key):
            barrier.wait()
            try:
                result = self.service().activate_lifecycle(
                    case_id=case_id, actor_user_id=1, idempotency_key=key
                )
                outcome = ("ok", result.case_id)
            except Exception as exc:
                outcome = (type(exc).__name__, case_id)
            with lock:
                results.append(outcome)

        threads = [
            threading.Thread(target=activate, args=(transfer.case_id, "race-transfer")),
            threading.Thread(target=activate, args=(progression.case_id, "race-progression")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(1, sum(item[0] == "ok" for item in results))
        self.assertEqual(1, sum(item[0] == "LifecycleActiveConflictError" for item in results))
        with sqlite3.connect(self.database_path) as connection:
            self.assertEqual(1, connection.execute(
                "SELECT count(*) FROM application_cycles WHERE status='active'"
            ).fetchone()[0])
