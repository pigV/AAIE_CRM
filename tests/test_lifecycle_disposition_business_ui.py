import sqlite3
import unittest
from unittest.mock import patch
from types import SimpleNamespace


class LifecycleDispositionBusinessUiTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            CREATE TABLE application_cycles(
              id INTEGER PRIMARY KEY,student_id TEXT,cycle_type TEXT,
              status TEXT,source_student_case_id INTEGER
            );
            CREATE TABLE visas(
              id INTEGER PRIMARY KEY,student_id TEXT,visa_type TEXT,
              visa_status TEXT,visa_grant_date TEXT,visa_expiry_date TEXT,
              created_at TEXT
            );
            CREATE TABLE visa_cases(
              id INTEGER PRIMARY KEY,student_id TEXT,legacy_visa_id INTEGER,
              application_cycle_id INTEGER
            );
            CREATE TABLE coe_oshc(
              id INTEGER PRIMARY KEY,student_id TEXT,document_kind TEXT,
              oshc_provider TEXT,oshc_company_name TEXT,oshc_expiry_date TEXT,
              application_cycle_id INTEGER,created_at TEXT
            );
            CREATE TABLE payments(
              id INTEGER PRIMARY KEY,student_id TEXT,payment_category TEXT,
              amount REAL,currency TEXT,paid_date TEXT,
              application_cycle_id INTEGER,created_at TEXT
            );
            CREATE TABLE student_documents(
              id INTEGER PRIMARY KEY,student_id TEXT,folder_category TEXT,
              document_type TEXT,original_name TEXT,uploaded_at TEXT,
              application_cycle_id INTEGER
            );
            CREATE TABLE transfer_cases(case_id INTEGER PRIMARY KEY);
            CREATE TABLE transfer_school_applications(
              id INTEGER PRIMARY KEY,case_id INTEGER,school_name_snapshot TEXT,
              application_status TEXT,created_at TEXT,is_final INTEGER
            );
            CREATE TABLE student_cases(
              id INTEGER PRIMARY KEY,student_id TEXT,status TEXT
            );
            CREATE TABLE progression_cases(case_id INTEGER PRIMARY KEY);
            CREATE TABLE progression_target_applications(
              id INTEGER PRIMARY KEY,case_id INTEGER,
              institution_name_snapshot TEXT,course_name TEXT,
              application_status TEXT,created_at TEXT
            );
            CREATE TABLE higher_education_applications(
              id INTEGER PRIMARY KEY,application_cycle_id INTEGER,
              course_name TEXT,status TEXT,created_at TEXT
            );
            CREATE TABLE lifecycle_module_disposition_events(
              id INTEGER PRIMARY KEY,event_uid TEXT,application_cycle_id INTEGER,
              module_key TEXT,disposition TEXT,reason TEXT,
              source_record_type TEXT,source_record_id INTEGER,
              actor_user_id INTEGER,occurred_at TEXT,created_at TEXT
            );

            INSERT INTO application_cycles VALUES
              (1,'S1','initial','completed',NULL),
              (2,'S1','transfer','closed',20),
              (7,'S1','transfer','active',70),
              (8,'S2','initial','completed',NULL);
            INSERT INTO visas VALUES
              (31,'S1','Student Visa 500','Granted','2026-03-15','2028-03-15','2026-03-15'),
              (32,'S1','Student Visa 500','Granted','2024-02-10','2026-02-10','2024-02-10'),
              (33,'S1','Student Visa 500','Pending',NULL,NULL,'2026-08-25'),
              (34,'S2','Student Visa 500','Granted','2025-01-01','2027-01-01','2025-01-01');
            INSERT INTO visa_cases VALUES
              (41,'S1',31,2),(42,'S1',32,1),(43,'S1',33,7),(44,'S2',34,8);
            INSERT INTO coe_oshc VALUES
              (51,'S1','OSHC','Bupa',NULL,'2028-02-28',2,'2026-03-10'),
              (52,'S1','COE',NULL,NULL,NULL,2,'2026-03-10'),
              (53,'S1','OSHC','Allianz',NULL,'2029-02-28',7,'2026-08-25');
            INSERT INTO payments VALUES
              (61,'S1','School fee',1200,'AUD','2026-03-20',2,'2026-03-20');
            INSERT INTO student_documents VALUES
              (71,'S1','Homestay + Airport Pickup','HomestayConfirmation',
               'Homestay confirmation.pdf','2026-03-21',2);
            INSERT INTO lifecycle_module_disposition_events VALUES
              (1,'visa-existing',7,'visa','existing',
               '系统沿用：Visa Grant 2026-03-15 · Student Visa 500',
               'visa',31,1,'2026-08-25T10:00:00','2026-08-25T10:00:00'),
              (2,'payment-na',7,'payments','not_applicable',
               '本轮无需额外付款',NULL,NULL,1,
               '2026-08-25T10:01:00','2026-08-25T10:01:00');
            """
        )

    def tearDown(self):
        self.connection.close()

    def test_candidate_discovery_only_returns_same_student_historical_records(self):
        from student_lifecycle.disposition_ui import load_existing_candidates

        visa = load_existing_candidates(self.connection, 7, "visa")
        self.assertEqual(
            [
                "Visa Grant 2026-03-15 · Student Visa 500",
                "Visa Grant 2024-02-10 · Student Visa 500",
            ],
            [item.label for item in visa],
        )
        self.assertEqual(["visa:31", "visa:32"], [item.choice_key for item in visa])

        oshc = load_existing_candidates(self.connection, 7, "coe_oshc")
        self.assertEqual(["OSHC · Bupa · 至 2028-02-28"], [item.label for item in oshc])
        self.assertEqual((), load_existing_candidates(self.connection, 7, "offer"))

    def test_unique_existing_candidate_is_bound_without_record_id_input(self):
        from student_lifecycle.disposition_ui import resolve_business_submission

        resolved = resolve_business_submission(
            self.connection,
            application_cycle_id=7,
            module_key="coe_oshc",
            handling="reuse",
            existing_choice=None,
            reason=None,
        )
        self.assertEqual(
            ("existing", "oshc", 51),
            (
                resolved.disposition,
                resolved.source_record_type,
                resolved.source_record_id,
            ),
        )
        self.assertEqual("系统沿用：OSHC · Bupa · 至 2028-02-28", resolved.reason)

    def test_multiple_existing_candidates_require_readable_choice(self):
        from student_lifecycle.disposition_ui import (
            BusinessDispositionSelectionError,
            resolve_business_submission,
        )

        with self.assertRaisesRegex(
            BusinessDispositionSelectionError, "existing_choice_required"
        ):
            resolve_business_submission(
                self.connection,
                application_cycle_id=7,
                module_key="visa",
                handling="reuse",
                existing_choice=None,
                reason=None,
            )
        resolved = resolve_business_submission(
            self.connection,
            application_cycle_id=7,
            module_key="visa",
            handling="reuse",
            existing_choice="visa:31",
            reason=None,
        )
        self.assertEqual(("visa", 31), (resolved.source_record_type, resolved.source_record_id))

    def test_business_statuses_hide_technical_disposition_language(self):
        from student_lifecycle.disposition_ui import business_status_label

        cases = (
            ("applications", "required_pending", (), None, "待补充"),
            ("offer", "required_pending", (), None, "等待新录取通知"),
            ("coe_oshc", "existing_validated", ("oshc",), "OSHC · Bupa", "沿用现有学生保险 · 等待新入学确认 · OSHC · Bupa"),
            ("visa", "existing_validated", ("visa",), "Visa Grant 2026-03-15", "沿用现有签证 · Visa Grant 2026-03-15"),
            ("payments", "not_applicable_audited", (), None, "无需处理"),
            ("homestay_airport", "required_satisfied", ("homestay_airport",), None, "已完成"),
        )
        for module_key, state, components, source_label, expected in cases:
            progress = SimpleNamespace(
                module_key=module_key,
                state=state,
                satisfied=state in {"existing_validated", "not_applicable_audited", "required_satisfied"},
                satisfied_components=components,
            )
            self.assertEqual(expected, business_status_label(progress, source_label))

    def test_business_control_uses_only_readable_choices_and_never_renders_ids(self):
        from student_lifecycle.disposition_ui import (
            ExistingRecordCandidate,
            render_business_disposition_control,
        )

        module = {
            "key": "visa",
            "disposition": "required",
            "reason": None,
            "source_record_type": None,
            "source_record_id": None,
            "existing_candidates": (
                ExistingRecordCandidate("visa:31", "visa", 31, "Visa Grant 2026-03-15 · Student Visa 500"),
                ExistingRecordCandidate("visa:32", "visa", 32, "Visa Grant 2024-02-10 · Student Visa 500"),
            ),
        }
        html = render_business_disposition_control(
            module,
            {"student_id": "S1", "cycle_id": 7, "csrf": "csrf-token"},
            event_uid="event-1",
        )
        self.assertIn("调整本次处理", html)
        self.assertIn("本次需要", html)
        self.assertIn("沿用现有", html)
        self.assertIn("Visa Grant 2026-03-15 · Student Visa 500", html)
        self.assertIn('name="existing_choice"', html)
        for forbidden in (
            "设置处理方式", "记录处理方式", "Required", "Existing来源",
            "Existing记录ID", "source_record_type", "source_record_id",
        ):
            self.assertNotIn(forbidden, html)

    def test_na_reason_is_conditional_and_offer_has_no_redundant_control(self):
        from student_lifecycle.disposition_ui import render_business_disposition_control

        payment = {
            "key": "payments", "disposition": "required", "reason": None,
            "source_record_type": None, "source_record_id": None,
            "existing_candidates": (),
        }
        html = render_business_disposition_control(
            payment,
            {"student_id": "S1", "cycle_id": 7, "csrf": "csrf-token"},
            event_uid="event-2",
        )
        self.assertIn("无需处理", html)
        self.assertIn('data-na-reason hidden', html)
        offer = dict(payment, key="offer")
        self.assertEqual(
            "",
            render_business_disposition_control(
                offer,
                {"student_id": "S1", "cycle_id": 7, "csrf": "csrf-token"},
                event_uid="event-3",
            ),
        )

    def test_cycle_module_projection_is_business_wording_with_readable_source(self):
        from student_lifecycle.module_progress import load_cycle_module_progress
        from student_progression_summary import load_lifecycle_workflow_modules

        progress = load_cycle_module_progress(self.connection, 7)
        projection = SimpleNamespace(
            modules_by_key={item.module_key: item for item in progress}
        )
        with patch(
            "student_progression_summary.load_lifecycle_projection_map",
            return_value={7: projection},
        ):
            modules = {
                item["key"]: item
                for item in load_lifecycle_workflow_modules(self.connection, 7)
            }
        self.assertEqual(
            "沿用现有签证 · Visa Grant 2026-03-15 · Student Visa 500",
            modules["visa"]["status"],
        )
        self.assertEqual("无需处理", modules["payments"]["status"])
        self.assertEqual("等待新录取通知", modules["offer"]["status"])
        self.assertEqual(2, len(modules["visa"]["existing_candidates"]))
        for module in modules.values():
            for forbidden in ("Required", "Existing ·", "N/A", "Blocked"):
                self.assertNotIn(forbidden, module["status"])


if __name__ == "__main__":
    unittest.main()
