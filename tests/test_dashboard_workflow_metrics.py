import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from application_cycle import create_parent_form_cycle, create_student_cycle
from dashboard_workflow_metrics import load_dashboard_workflow_metrics
from tests.test_workflow_application_cycles_migration import (
    NOW,
    apply_workflow_migration,
    build_legacy_fixture,
)


class DashboardWorkflowMetricsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "metrics.db"
        self.conn = build_legacy_fixture(self.database_path)
        self.conn.row_factory = sqlite3.Row
        apply_workflow_migration(self.conn)

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def _student_offer_cycle(self):
        cycle = create_student_cycle(
            self.conn, "S1", "initial", NOW, 1, cycle_uid="metrics-offers"
        )
        private_id = int(
            self.conn.execute(
                """
                INSERT INTO private_school_applications(
                  application_cycle_id,student_id,school_name_snapshot,status,
                  submitted_at,created_by_user_id,created_at,updated_at
                ) VALUES(?,'S1','Metric School','offer_received',?,1,?,?)
                """,
                (cycle.id, NOW, NOW, NOW),
            ).lastrowid
        )
        return cycle, private_id

    def test_active_count_includes_pre_student_workflow_case(self):
        create_parent_form_cycle(
            self.conn, "parent-token-1", NOW, 1, cycle_uid="metrics-case"
        )

        metrics = load_dashboard_workflow_metrics(self.conn, today=date(2026, 8, 7))

        self.assertEqual(1, metrics.active_workflow_count)
        self.assertEqual(1, metrics.stage_distribution.by_key["01_parent_form"].count)
        self.assertEqual("workflow_case", metrics.active_records[0].entity_kind)

    def test_active_cycle_count_is_not_mislabeled_as_unique_student_count(self):
        self._student_offer_cycle()
        create_parent_form_cycle(
            self.conn, "parent-token-1", NOW, 1, cycle_uid="metrics-unowned-case"
        )

        metrics = load_dashboard_workflow_metrics(self.conn, today=date(2026, 8, 7))

        self.assertEqual(2, metrics.active_workflow_count)
        self.assertEqual(1, metrics.unique_active_student_count)

    def test_active_by_cycle_type_counts_projection_and_excludes_closed_cycles(self):
        self.conn.executemany(
            "INSERT INTO student_cases(id,student_id,case_type) VALUES(?,?,?)",
            (
                (101, "S1", "transfer"),
                (102, "S2", "progression"),
                (103, "S1", "transfer"),
            ),
        )
        create_student_cycle(
            self.conn, "S1", "transfer", NOW, 1, cycle_uid="active-transfer",
            source_student_case_id=101,
        )
        create_student_cycle(
            self.conn, "S2", "progression", NOW, 1,
            cycle_uid="active-progression",
            source_student_case_id=102,
        )
        self.conn.execute(
            """INSERT INTO application_cycles(
                 cycle_uid,student_id,source_student_case_id,cycle_type,
                 sequence_no,status,outcome,opened_at,closed_at,
                 created_by_user_id,created_at,updated_at
               ) VALUES('closed-transfer','S1',103,'transfer',2,'closed',
                        'completed',?,?,1,?,?)""",
            (NOW, NOW, NOW, NOW),
        )

        metrics = load_dashboard_workflow_metrics(
            self.conn, today=date(2026, 8, 7)
        )

        self.assertEqual(
            {"initial": 0, "transfer": 1, "progression": 1},
            metrics.active_by_cycle_type,
        )
        self.assertEqual(2, metrics.active_workflow_count)

    def test_offer_metric_counts_real_events_not_students_or_documents(self):
        cycle, private_id = self._student_offer_cycle()
        for number in range(5):
            self.conn.execute(
                """
                INSERT INTO offer_received_events(
                  application_cycle_id,student_id,source_kind,private_application_id,
                  school_name_snapshot,external_offer_key,idempotency_key,received_at,
                  recorded_by_user_id,created_at
                ) VALUES(?,'S1','private',?,'Metric School',?,?,?,1,?)
                """,
                (
                    cycle.id,
                    private_id,
                    f"week-offer-{number}",
                    f"week-idem-{number}",
                    f"2026-08-0{3 + number}T10:00:00+10:00",
                    NOW,
                ),
            )
        self.conn.execute(
            """
            INSERT INTO offer_received_events(
              application_cycle_id,student_id,source_kind,private_application_id,
              school_name_snapshot,external_offer_key,idempotency_key,received_at,
              recorded_by_user_id,created_at
            ) VALUES(?,'S1','private',?,'Metric School','old','old-idem',?,1,?)
            """,
            (cycle.id, private_id, "2026-08-02T23:59:59+10:00", NOW),
        )

        metrics = load_dashboard_workflow_metrics(self.conn, today=date(2026, 8, 7))

        self.assertEqual(5, metrics.week_offer_count)

    def test_contract_metric_counts_cycle_once_with_exclusive_week_boundary(self):
        cycle, _ = self._student_offer_cycle()
        self.conn.execute(
            """
            UPDATE contracts
            SET application_cycle_id=?,contract_status='FullyExecuted',fully_executed_at=?
            WHERE id=10
            """,
            (cycle.id, "2026-08-03T00:00:00+10:00"),
        )
        self.conn.execute(
            """
            INSERT INTO contracts(student_id,contract_status,fully_executed_at,application_cycle_id)
            VALUES('S1','FullyExecuted','2026-08-09T23:59:59+10:00',?)
            """,
            (cycle.id,),
        )

        metrics = load_dashboard_workflow_metrics(self.conn, today=date(2026, 8, 7))

        self.assertEqual(1, metrics.signed_this_week)
        self.assertEqual(1, metrics.signed_this_month)

    def test_grant_counts_first_event_and_refusal_only_closes_cycle(self):
        grant_cycle = self._insert_closed_cycle("S1", "grant-cycle", "granted", 1)
        refusal_cycle = self._insert_closed_cycle("S2", "refusal-cycle", "refused", 1)
        self._insert_visa_decision(grant_cycle, "S1", "grant", 61, 71)
        self._insert_visa_decision(refusal_cycle, "S2", "refusal", 62, 72)

        metrics = load_dashboard_workflow_metrics(self.conn, today=date(2026, 8, 7))

        self.assertEqual(1, metrics.week_grant_count)
        self.assertEqual(1, metrics.month_grant_count)

    def test_signed_institution_structure_uses_final_acceptance_only(self):
        cycle, private_id = self._student_offer_cycle()
        offer_id = int(
            self.conn.execute(
                """
                INSERT INTO offer_received_events(
                  application_cycle_id,student_id,source_kind,private_application_id,
                  school_name_snapshot,external_offer_key,idempotency_key,received_at,
                  recorded_by_user_id,created_at
                ) VALUES(?,'S1','private',?,'Final School','accepted','accepted-idem',?,1,?)
                """,
                (cycle.id, private_id, NOW, NOW),
            ).lastrowid
        )
        self.conn.execute(
            """
            INSERT INTO cycle_final_acceptances(
              application_cycle_id,student_id,offer_received_event_id,
              school_name_snapshot,selected_at,selected_by_user_id,created_at,updated_at
            ) VALUES(?,'S1',?,'Final School',?,1,?,?)
            """,
            (cycle.id, offer_id, NOW, NOW, NOW),
        )

        metrics = load_dashboard_workflow_metrics(self.conn, today=date(2026, 8, 7))

        self.assertEqual(1, len(metrics.signed_institution_structure))
        self.assertEqual("Final School", metrics.signed_institution_structure[0].school_name)
        self.assertEqual(1, metrics.signed_institution_structure[0].cycle_count)

    def test_utc_sunday_timestamp_on_melbourne_monday_is_next_week(self):
        cycle, _ = self._student_offer_cycle()
        self.conn.execute(
            """UPDATE contracts
               SET application_cycle_id=?,contract_status='FullyExecuted',
                   fully_executed_at='2026-08-16T15:30:00Z'
               WHERE id=10""",
            (cycle.id,),
        )

        metrics = load_dashboard_workflow_metrics(
            self.conn, today=date(2026, 8, 16)
        )

        self.assertEqual(0, metrics.signed_this_week)
        self.assertEqual(1, metrics.signed_this_month)

    def test_grant_kpi_uses_decision_date_not_evidence_or_recorded_timestamp(self):
        self.conn.execute(
            "ALTER TABLE visa_decision_events ADD COLUMN decision_date TEXT"
        )
        grant_cycle = self._insert_closed_cycle("S1", "grant-date-cycle", "granted", 1)
        self.conn.execute(
            "INSERT INTO visa_cases(id,student_id,result_status,application_cycle_id) VALUES(63,'S1','grant',?)",
            (grant_cycle,),
        )
        self.conn.execute(
            "INSERT INTO student_documents(id,student_id,document_type,uploaded_at,is_active) VALUES(73,'S1','VisaGrantLetter','2026-07-30T10:00:00+10:00',1)"
        )
        self.conn.execute(
            """INSERT INTO visa_decision_events(
                 application_cycle_id,student_id,visa_case_id,student_document_id,
                 decision_type,confirmed_at,idempotency_key,recorded_by_user_id,
                 created_at,decision_date
               ) VALUES(?,'S1',63,73,'grant','2026-07-30T10:00:00+10:00',
                        'decision-business-date',1,?,'2026-08-05')""",
            (grant_cycle, NOW),
        )

        metrics = load_dashboard_workflow_metrics(self.conn, today=date(2026, 8, 7))

        self.assertEqual(1, metrics.week_grant_count)
        self.assertEqual(1, metrics.month_grant_count)

    def test_offer_invalidation_only_excludes_explicit_misrecord_correction(self):
        cycle, private_id = self._student_offer_cycle()
        offer_ids = []
        for number in range(3):
            offer_ids.append(int(self.conn.execute(
                """INSERT INTO offer_received_events(
                     application_cycle_id,student_id,source_kind,private_application_id,
                     school_name_snapshot,external_offer_key,idempotency_key,received_at,
                     recorded_by_user_id,created_at
                   ) VALUES(?,'S1','private',?,'Metric School',?,?,?,1,?)""",
                (
                    cycle.id,
                    private_id,
                    f"state-offer-{number}",
                    f"state-idem-{number}",
                    "2026-08-05T10:00:00+10:00",
                    NOW,
                ),
            ).lastrowid))
        self.conn.executescript(
            """
            CREATE TABLE offer_received_event_state_events(
              id INTEGER PRIMARY KEY,
              offer_received_event_id INTEGER NOT NULL,
              state TEXT NOT NULL,
              occurred_at TEXT NOT NULL
            );
            """
        )
        self.conn.executemany(
            """INSERT INTO offer_received_event_state_events(
                 offer_received_event_id,state,occurred_at
               ) VALUES(?,?,?)""",
            [
                (offer_ids[0], "VOID", "2026-08-06T09:00:00+10:00"),
                (offer_ids[1], "INVALID_RECORD", "2026-08-06T10:00:00+10:00"),
            ],
        )

        metrics = load_dashboard_workflow_metrics(self.conn, today=date(2026, 8, 7))

        self.assertEqual(2, metrics.week_offer_count)

    def _insert_closed_cycle(self, student_id, uid, outcome, sequence_no):
        return int(
            self.conn.execute(
                """
                INSERT INTO application_cycles(
                  cycle_uid,student_id,cycle_type,sequence_no,status,outcome,
                  opened_at,closed_at,created_by_user_id,created_at,updated_at
                ) VALUES(?,?,'initial',?,'closed',?,?,?,1,?,?)
                """,
                (uid, student_id, sequence_no, outcome, NOW, NOW, NOW, NOW),
            ).lastrowid
        )

    def _insert_visa_decision(self, cycle_id, student_id, decision_type, visa_id, doc_id):
        document_type = "VisaGrantLetter" if decision_type == "grant" else "VisaRefusalLetter"
        self.conn.execute(
            "INSERT INTO visa_cases(id,student_id,result_status,application_cycle_id) VALUES(?,?,?,?)",
            (visa_id, student_id, decision_type, cycle_id),
        )
        self.conn.execute(
            "INSERT INTO student_documents(id,student_id,document_type,uploaded_at,is_active) VALUES(?,?,?,?,1)",
            (doc_id, student_id, document_type, "2026-08-05T10:00:00+10:00"),
        )
        self.conn.execute(
            """
            INSERT INTO visa_decision_events(
              application_cycle_id,student_id,visa_case_id,student_document_id,
              decision_type,confirmed_at,idempotency_key,recorded_by_user_id,created_at
            ) VALUES(?,?,?,?,?,?,?,1,?)
            """,
            (
                cycle_id,
                student_id,
                visa_id,
                doc_id,
                decision_type,
                "2026-08-05T10:00:00+10:00",
                f"decision-{cycle_id}",
                NOW,
            ),
        )


if __name__ == "__main__":
    unittest.main()
