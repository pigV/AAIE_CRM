import sqlite3
import tempfile
import unittest
from pathlib import Path

from application_cycle import create_parent_form_cycle, create_student_cycle
from tests.test_workflow_application_cycles_migration import (
    NOW,
    apply_workflow_migration,
    build_legacy_fixture,
)
from workflow_facts import load_workflow_facts


class WorkflowFactsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "facts.db"
        self.conn = build_legacy_fixture(self.database_path)
        self.conn.row_factory = sqlite3.Row
        apply_workflow_migration(self.conn)

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def test_loads_stage_one_workflow_case_without_student(self):
        cycle = create_parent_form_cycle(
            self.conn,
            "parent-token-1",
            NOW,
            1,
            cycle_uid="cycle-stage-one",
        )

        facts = load_workflow_facts(self.conn, [cycle.id])[cycle.id]

        self.assertEqual("workflow_case", facts.entity_kind)
        self.assertTrue(facts.parent_form_exists)
        self.assertFalse(facts.student_exists)

    def test_loads_cycle_owned_offer_acceptance_facts_without_legacy_stage(self):
        cycle = create_student_cycle(
            self.conn,
            "S1",
            "initial",
            NOW,
            1,
            cycle_uid="cycle-facts",
        )
        self.conn.execute(
            """
            UPDATE contracts
            SET application_cycle_id=?,contract_status='FullyExecuted',fully_executed_at=?
            WHERE id=10
            """,
            (cycle.id, NOW),
        )
        self.conn.execute(
            "UPDATE school_plan_reports SET application_cycle_id=?,cycle_is_current=1 WHERE id=20",
            (cycle.id,),
        )
        self.conn.execute(
            "INSERT INTO school_plan_report_links(id,report_id,status,created_at,confirmed_at) VALUES(80,20,'confirmed',?,?)",
            (NOW, NOW),
        )
        self.conn.execute(
            "INSERT INTO school_plan_report_confirmations(id,report_id,student_id,confirmed_at) VALUES(90,20,'S1',?)",
            (NOW,),
        )
        self.conn.execute(
            """
            INSERT INTO private_school_applications(
              application_cycle_id,student_id,school_name_snapshot,status,
              submitted_at,created_by_user_id,created_at,updated_at
            ) VALUES(?,'S1','Private A','offer_received',?,1,?,?)
            """,
            (cycle.id, NOW, NOW, NOW),
        )
        private_id = self.conn.execute(
            "SELECT id FROM private_school_applications"
        ).fetchone()[0]
        self.conn.execute(
            """
            INSERT INTO offer_received_events(
              application_cycle_id,student_id,source_kind,private_application_id,
              school_name_snapshot,external_offer_key,idempotency_key,received_at,
              recorded_by_user_id,created_at
            ) VALUES(?,'S1','private',?,'Private A','offer-1','facts:offer-1',?,1,?)
            """,
            (cycle.id, private_id, NOW, NOW),
        )
        offer_id = self.conn.execute(
            "SELECT id FROM offer_received_events"
        ).fetchone()[0]
        self.conn.execute(
            """
            INSERT INTO cycle_final_acceptances(
              application_cycle_id,student_id,offer_received_event_id,
              school_name_snapshot,signed_offer_completed_at,payment_completed_at,
              selected_at,selected_by_user_id,created_at,updated_at
            ) VALUES(?,'S1',?,'Private A',?,?,?,1,?,?)
            """,
            (cycle.id, offer_id, NOW, NOW, NOW, NOW, NOW),
        )

        facts = load_workflow_facts(self.conn, [cycle.id])[cycle.id]

        self.assertTrue(facts.contract_signed)
        self.assertTrue(facts.plan_confirmed)
        self.assertEqual(1, facts.submitted_application_count)
        self.assertEqual(1, facts.actionable_offer_count)
        self.assertTrue(facts.signed_offer_completed)
        self.assertTrue(facts.payment_completed)
        self.assertFalse(facts.submitted_back_to_final_school)

    def test_progression_required_components_aggregate_without_fake_primary(self):
        self.conn.execute(
            "INSERT INTO student_cases(id,student_id,case_type) VALUES(70,'S1','progression')"
        )
        cycle = create_student_cycle(
            self.conn, "S1", "progression", NOW, 1,
            source_student_case_id=70,
            cycle_uid="cycle-progression-facts",
        )
        self.conn.executescript(
            """
            CREATE TABLE progression_pathway_acceptances(
              id INTEGER PRIMARY KEY, application_cycle_id INTEGER NOT NULL,
              status TEXT NOT NULL
            );
            CREATE TABLE progression_document_links(
              id INTEGER PRIMARY KEY, status TEXT NOT NULL
            );
            CREATE TABLE progression_pathway_components(
              id INTEGER PRIMARY KEY, pathway_id INTEGER NOT NULL,
              active_final INTEGER NOT NULL, component_role TEXT NOT NULL,
              accepted_progression_offer_id INTEGER,
              canonical_offer_event_id INTEGER,
              signed_offer_document_link_id INTEGER,
              payment_document_link_id INTEGER,
              coe_record_id INTEGER
            );
            INSERT INTO progression_pathway_acceptances
              VALUES(1,1,'accepted');
            INSERT INTO progression_document_links VALUES
              (10,'confirmed'),(11,'confirmed'),
              (20,'confirmed'),(21,'received');
            INSERT INTO progression_pathway_components VALUES
              (1,1,1,'required',100,200,10,20,300),
              (2,1,1,'required',101,201,11,21,301);
            """
        )

        facts = load_workflow_facts(self.conn, [cycle.id])[cycle.id]

        self.assertTrue(facts.final_acceptance_selected)
        self.assertTrue(facts.signed_offer_completed)
        self.assertFalse(facts.payment_completed)
        self.assertTrue(facts.coe_exists)
        self.conn.execute(
            "UPDATE progression_document_links SET status='confirmed' WHERE id=21"
        )
        ready = load_workflow_facts(self.conn, [cycle.id])[cycle.id]
        self.assertTrue(ready.payment_completed)

    def test_progression_contract_gate_reads_exact_case_ownership(self):
        self.conn.execute(
            "INSERT INTO student_cases(id,student_id,case_type) VALUES(70,'S1','progression')"
        )
        cycle = create_student_cycle(
            self.conn, "S1", "progression", NOW, 1,
            source_student_case_id=70,
            cycle_uid="cycle-progression-contract-gate",
        )
        contract_columns = {
            row[1] for row in self.conn.execute("PRAGMA table_info(contracts)")
        }
        if "contract_type" not in contract_columns:
            self.conn.execute("ALTER TABLE contracts ADD COLUMN contract_type TEXT")
        self.conn.executescript(
            """
            CREATE TABLE progression_cases(
              case_id INTEGER PRIMARY KEY,
              business_type TEXT,
              source_contract_id INTEGER
            );
            CREATE TABLE progression_pathway_acceptances(
              id INTEGER PRIMARY KEY, application_cycle_id INTEGER,
              status TEXT
            );
            CREATE TABLE progression_document_links(
              id INTEGER PRIMARY KEY, status TEXT
            );
            CREATE TABLE progression_pathway_components(
              id INTEGER PRIMARY KEY, pathway_id INTEGER,
              active_final INTEGER, component_role TEXT,
              accepted_progression_offer_id INTEGER,
              canonical_offer_event_id INTEGER,
              signed_offer_document_link_id INTEGER,
              payment_document_link_id INTEGER,
              coe_record_id INTEGER
            );
            INSERT INTO progression_cases
              VALUES(70,'school_progression',10);
            UPDATE contracts
               SET student_id='S1',contract_type='LocalStudentApplicationContract',
                   contract_status='FullyExecuted'
             WHERE id=10;
            """
        )

        facts = load_workflow_facts(self.conn, [cycle.id])[cycle.id]

        self.assertEqual("progression", facts.cycle_type)
        self.assertEqual(70, facts.source_case_id)
        self.assertEqual("complete", facts.contract_gate_status)

    def test_batch_query_count_does_not_grow_with_cycle_count(self):
        first = create_parent_form_cycle(
            self.conn,
            "parent-token-1",
            NOW,
            1,
            cycle_uid="cycle-one",
        )
        self.conn.execute("INSERT INTO parent_form_tokens(token) VALUES('parent-token-2')")
        second = create_parent_form_cycle(
            self.conn,
            "parent-token-2",
            NOW,
            1,
            cycle_uid="cycle-two",
        )

        def select_count(cycle_ids):
            statements = []
            self.conn.set_trace_callback(statements.append)
            try:
                load_workflow_facts(self.conn, cycle_ids)
            finally:
                self.conn.set_trace_callback(None)
            return sum(
                1 for statement in statements if statement.lstrip().upper().startswith("SELECT")
            )

        self.assertEqual(select_count([first.id]), select_count([first.id, second.id]))

    def test_manual_result_authority_drives_student_progress_without_canonical_event(self):
        cycle = create_student_cycle(
            self.conn, "S2", "initial", NOW, 1,
            cycle_uid="cycle-result-authority",
        )
        self.conn.execute(
            "INSERT INTO visa_cases(id,student_id,application_cycle_id,result_status) VALUES(51,'S2',?,'Pending')",
            (cycle.id,),
        )
        self.conn.execute(
            "CREATE TABLE visa_case_documents(id INTEGER PRIMARY KEY,visa_case_id INTEGER,archived_at TEXT)"
        )
        migration = Path(__file__).resolve().parents[1] / "migrations/20260914_039_visa_result_status_authority.sql"
        self.conn.executescript(migration.read_text())
        from visa_result_status import record_manual_result
        record_manual_result(
            self.conn, 51, "Granted", actor_user_id=1,
            occurred_at=NOW, reason="Staff verified result",
        )

        facts = load_workflow_facts(self.conn, [cycle.id])[cycle.id]

        self.assertEqual("grant", facts.visa_decision)
        self.assertEqual(0, self.conn.execute("SELECT count(*) FROM visa_decision_events WHERE application_cycle_id=?", (cycle.id,)).fetchone()[0])

    def test_manual_pending_or_withdrawn_clears_prior_canonical_current_projection(self):
        cycle = create_student_cycle(
            self.conn, "S2", "initial", NOW, 1,
            cycle_uid="cycle-current-authority",
        )
        case_id = 61
        self.conn.execute(
            "INSERT INTO visa_cases(id,student_id,application_cycle_id,result_status) VALUES(?,'S2',?,'Granted')",
            (case_id, cycle.id),
        )
        self.conn.execute(
            "INSERT INTO student_documents(id,student_id,document_type,uploaded_at) VALUES(61,'S2','VisaGrantLetter',?)",
            (NOW,),
        )
        self.conn.execute(
            "INSERT INTO visa_decision_events(application_cycle_id,student_id,visa_case_id,student_document_id,decision_type,confirmed_at,idempotency_key,recorded_by_user_id,created_at) VALUES(?,'S2',61,61,'grant',?,'fixture-current-authority',1,?)",
            (cycle.id, NOW, NOW),
        )
        self.conn.execute(
            "CREATE TABLE visa_case_documents(id INTEGER PRIMARY KEY,visa_case_id INTEGER,archived_at TEXT)"
        )
        migration = Path(__file__).resolve().parents[1] / "migrations/20260914_039_visa_result_status_authority.sql"
        self.conn.executescript(migration.read_text())
        from visa_result_status import record_manual_result
        for result in ("Pending", "Withdrawn"):
            with self.subTest(result=result):
                record_manual_result(
                    self.conn, case_id, result, actor_user_id=1,
                    occurred_at=NOW, reason="Staff corrected the current result",
                )

                facts = load_workflow_facts(self.conn, [cycle.id])[cycle.id]

                self.assertIsNone(facts.visa_decision)


if __name__ == "__main__":
    unittest.main()
