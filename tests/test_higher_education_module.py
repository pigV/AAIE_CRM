import sqlite3
import tempfile
import unittest
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations" / "20260813_018_higher_education_application_module.sql"


def base_connection():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        """
        CREATE TABLE users(id INTEGER PRIMARY KEY);
        CREATE TABLE coe_oshc(id INTEGER PRIMARY KEY,application_cycle_id INTEGER);
        CREATE TABLE students(
          student_id TEXT PRIMARY KEY,
          applying_year_level TEXT,
          form_data_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE parent_form_tokens(token TEXT PRIMARY KEY);
        CREATE TABLE student_cases(id INTEGER PRIMARY KEY);
        CREATE TABLE application_cycles(
          id INTEGER PRIMARY KEY,
          cycle_uid TEXT NOT NULL UNIQUE,
          student_id TEXT REFERENCES students(student_id),
          parent_form_token TEXT REFERENCES parent_form_tokens(token),
          source_student_case_id INTEGER REFERENCES student_cases(id),
          cycle_type TEXT NOT NULL,
          sequence_no INTEGER NOT NULL,
          status TEXT NOT NULL,
          outcome TEXT,
          opened_at TEXT NOT NULL,
          closed_at TEXT,
          created_by_user_id INTEGER,
          created_by_name TEXT,
          created_at TEXT NOT NULL,
          updated_by_user_id INTEGER,
          updated_by_name TEXT,
          updated_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX uq_application_cycles_active_student
          ON application_cycles(student_id)
          WHERE status='active' AND student_id IS NOT NULL;
        CREATE UNIQUE INDEX uq_application_cycles_student_sequence
          ON application_cycles(student_id,sequence_no)
          WHERE student_id IS NOT NULL;
        """
    )
    return conn


def apply_migration(conn):
    conn.executescript(MIGRATION.read_text(encoding="utf-8"))


class HigherEducationMigrationTests(unittest.TestCase):
    def setUp(self):
        self.conn = base_connection()

    def tearDown(self):
        self.conn.close()

    def test_schema_supports_streams_and_pathway_many_to_many(self):
        self.assertTrue(MIGRATION.is_file())
        apply_migration(self.conn)
        student_columns = {
            row[1] for row in self.conn.execute("PRAGMA table_info(students)")
        }
        self.assertTrue({"application_stream", "higher_education_course_type"} <= student_columns)
        provider_columns = {
            row[1] for row in self.conn.execute("PRAGMA table_info(higher_education_providers)")
        }
        self.assertFalse("parent_university_id" in provider_columns)
        relationship_columns = {
            row[1]
            for row in self.conn.execute(
                "PRAGMA table_info(higher_education_provider_relationships)"
            )
        }
        self.assertTrue(
            {
                "university_id", "pathway_provider_id", "relationship_type",
                "effective_from", "effective_to", "active_status", "source_url",
                "evidence", "created_by_user_id", "created_at", "updated_by_user_id",
                "updated_at",
            }
            <= relationship_columns
        )

        self.conn.execute(
            "INSERT INTO higher_education_providers(provider_name,normalized_name,provider_type,review_status,active_status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            ("University A", "university a", "University", "Approved", "Active", "t", "t"),
        )
        university_a = self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        self.conn.execute(
            "INSERT INTO higher_education_providers(provider_name,normalized_name,provider_type,review_status,active_status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            ("University B", "university b", "University", "Approved", "Active", "t", "t"),
        )
        university_b = self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        self.conn.execute(
            "INSERT INTO higher_education_providers(provider_name,normalized_name,provider_type,review_status,active_status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            ("Shared College", "shared college", "Pathway Provider", "Approved", "Active", "t", "t"),
        )
        pathway = self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        for university in (university_a, university_b):
            self.conn.execute(
                "INSERT INTO higher_education_provider_relationships(university_id,pathway_provider_id,relationship_type,active_status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (university, pathway, "Partner", "Active", "t", "t"),
            )
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM higher_education_provider_relationships WHERE pathway_provider_id=?",
                (pathway,),
            ).fetchone()[0],
            2,
        )
        share_columns = {
            row[1] for row in self.conn.execute(
                "PRAGMA table_info(higher_education_temporary_commission_shares)"
            )
        }
        self.assertTrue(
            {"application_id", "package_segment_id", "share_percent", "status", "evidence",
             "created_by_user_id", "reviewed_by_user_id", "created_at", "updated_at"}
            <= share_columns
        )
        commission_columns = {
            row[1] for row in self.conn.execute(
                "PRAGMA table_info(higher_education_commission_lines)"
            )
        }
        self.assertTrue(
            {"application_id", "package_segment_id", "teaching_provider_id", "source_type",
             "source_id", "status", "created_by_user_id", "created_at", "updated_at"}
            <= commission_columns
        )
        coe_columns = {
            row[1] for row in self.conn.execute(
                "PRAGMA table_info(higher_education_coe_links)"
            )
        }
        self.assertTrue(
            {"application_id", "package_segment_id", "coe_oshc_id", "scope",
             "created_by_user_id", "created_at"} <= coe_columns
        )

    def test_one_active_cycle_per_student_per_stream(self):
        apply_migration(self.conn)
        self.conn.execute("INSERT INTO students(student_id) VALUES('S1')")
        values = ("S1", "initial", 1, "active", "t", "t", "t")
        self.conn.execute(
            "INSERT INTO application_cycles(cycle_uid,student_id,cycle_type,sequence_no,status,opened_at,created_at,updated_at,education_stream) VALUES('school',?,?,?,?,?,?,?,'school_education')",
            values,
        )
        self.conn.execute(
            "INSERT INTO application_cycles(cycle_uid,student_id,cycle_type,sequence_no,status,opened_at,created_at,updated_at,education_stream) VALUES('higher',?, ?,2,?,?,?,?,'higher_education')",
            ("S1", "initial", "active", "t", "t", "t"),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO application_cycles(cycle_uid,student_id,cycle_type,sequence_no,status,opened_at,created_at,updated_at,education_stream) VALUES('higher-2',?, ?,3,?,?,?,?,'higher_education')",
                ("S1", "initial", "active", "t", "t", "t"),
            )

    def test_official_seed_contains_australian_universities_and_pathways(self):
        apply_migration(self.conn)
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM higher_education_providers WHERE provider_type='University' AND review_status='Approved'").fetchone()[0],
            42,
        )
        self.assertGreaterEqual(
            self.conn.execute("SELECT count(*) FROM higher_education_providers WHERE provider_type='Pathway Provider' AND review_status='Approved'").fetchone()[0],
            3,
        )
        self.assertGreaterEqual(
            self.conn.execute("SELECT count(*) FROM higher_education_provider_relationships").fetchone()[0],
            3,
        )


class HigherEducationRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.conn = base_connection()
        apply_migration(self.conn)
        self.conn.execute("INSERT INTO users(id) VALUES(1)")
        self.conn.execute("INSERT INTO students(student_id) VALUES('S1')")
        self.conn.execute(
            "INSERT INTO application_cycles(cycle_uid,student_id,cycle_type,sequence_no,status,opened_at,created_at,updated_at,education_stream) VALUES('he', 'S1','initial',1,'active','t','t','t','higher_education')"
        )
        self.cycle_id = self.conn.execute("SELECT id FROM application_cycles").fetchone()[0]

    def tearDown(self):
        self.conn.close()

    def test_provider_relationship_plan_and_application_invariants(self):
        from higher_education_repository import (
            HigherEducationConflict,
            HigherEducationRepository,
            HigherEducationValidationError,
        )

        repo = HigherEducationRepository(self.conn)
        university = repo.create_provider("Example University", "University", 1, "t")
        pathway = repo.create_provider("Shared College", "Pathway Provider", 1, "t")
        repo.link_pathway(university, pathway, "Direct", 1, "t", source_url="https://example.test")
        with self.assertRaises(HigherEducationValidationError):
            repo.link_pathway(pathway, university, "Partner", 1, "t")

        plan_id = repo.create_plan(self.cycle_id, 1, "t")
        item_id = repo.add_plan_item(plan_id, university, "Bachelor", "Bachelor of Arts", "", 1, "t")
        repo.confirm_plan(plan_id, 1, "t")
        with self.assertRaises(HigherEducationValidationError):
            repo.submit_plan_item(item_id, "Direct", None, 1, "t")
        application_id = repo.submit_plan_item(item_id, "Direct", "2027-02", 1, "t")
        with self.assertRaises(HigherEducationConflict):
            repo.create_application(self.cycle_id, university, "Bachelor", "Another Course", "2027-07", "Direct", 1, "t")

        package_provider = repo.create_provider("Package College", "Pathway Provider", 1, "t")
        package_application = repo.create_application(self.cycle_id, pathway, "Package", "Foundation + Bachelor", "2027-02", "Direct", 1, "t")
        repo.add_package_segment(package_application, 1, package_provider, "Foundation", "Foundation", "2027-02", 1, "t")
        repo.add_package_segment(package_application, 2, university, "Bachelor", "Bachelor of Arts", "2028-02", 1, "t")
        with self.assertRaises(HigherEducationConflict):
            repo.add_package_segment(package_application, 2, university, "Bachelor", "Duplicate", "2028-02", 1, "t")

        offer_one = repo.record_offer(application_id, "t")
        offer_two = repo.record_offer(package_application, "t")
        repo.accept_offer(offer_one, 1, "t")
        with self.assertRaises(HigherEducationConflict):
            repo.accept_offer(offer_two, 1, "t")

    def test_pending_provider_review_and_course_usage_are_auditable(self):
        from higher_education_repository import (
            HigherEducationConflict,
            HigherEducationRepository,
        )

        repo = HigherEducationRepository(self.conn)
        provider = repo.create_provider("Review University", "University", 1, "t")
        repo.review_provider(provider, "Approved", 1, "t2")
        reviewed = self.conn.execute(
            "SELECT review_status,active_status,updated_by_user_id,updated_at "
            "FROM higher_education_providers WHERE id=?",
            (provider,),
        ).fetchone()
        self.assertEqual(tuple(reviewed), ("Approved", "Active", 1, "t2"))
        with self.assertRaises(HigherEducationConflict):
            repo.review_provider(provider, "Rejected", 1, "t3")

        plan = repo.create_plan(self.cycle_id, 1, "t")
        repo.add_plan_item(plan, provider, "Bachelor", " Bachelor of Arts ", "", 1, "t")
        repo.add_plan_item(plan, provider, "Bachelor", "bachelor   of arts", "", 1, "t2")
        usage = self.conn.execute(
            """SELECT n.normalized_name,p.usage_count,p.last_used_at
               FROM higher_education_provider_course_names p
               JOIN higher_education_course_names n ON n.id=p.course_name_id
               WHERE p.provider_id=?""",
            (provider,),
        ).fetchone()
        self.assertEqual(tuple(usage), ("bachelor of arts", 2, "t2"))

    def test_temporary_share_commission_and_coe_links_require_correct_scope(self):
        from higher_education_repository import (
            HigherEducationConflict,
            HigherEducationRepository,
        )

        repo = HigherEducationRepository(self.conn)
        university = repo.create_provider("Commission University", "University", 1, "t")
        college = repo.create_provider("Commission College", "Pathway Provider", 1, "t")
        application = repo.create_application(
            self.cycle_id, university, "Package", "Foundation + Bachelor", "2027-02",
            "Master Agent", 1, "t", master_agent_id=99,
        )
        segment = repo.add_package_segment(
            application, 1, college, "Foundation", "Foundation", "2027-02", 1, "t"
        )
        share = repo.propose_temporary_commission_share(
            application, 15, 1, "t", package_segment_id=segment, evidence="email"
        )
        with self.assertRaises(HigherEducationConflict):
            repo.create_commission_line(
                application, college, "Temporary Share", share, 1, "t",
                package_segment_id=segment,
            )
        repo.review_temporary_commission_share(share, "Approved", 1, "t2")
        line = repo.create_commission_line(
            application, college, "Temporary Share", share, 1, "t3",
            package_segment_id=segment,
        )
        self.assertGreater(line, 0)

        self.conn.execute(
            "INSERT INTO coe_oshc(id,application_cycle_id) VALUES(7,?)", (self.cycle_id,)
        )
        link = repo.link_coe(application, 7, 1, "t4", package_segment_id=segment)
        self.assertGreater(link, 0)
        with self.assertRaises(HigherEducationConflict):
            repo.link_coe(application, 7, 1, "t5")


class HigherEducationParentIntakeTests(unittest.TestCase):
    def test_parent_schema_has_no_default_stream_and_conditional_course_type(self):
        from fields import FIELD_MAP
        from app import missing_student_fields
        from ui import parent_form

        self.assertEqual(
            FIELD_MAP["application_stream"]["options"],
            ["school_education|中小学申请", "higher_education|高等教育申请"],
        )
        self.assertEqual(
            FIELD_MAP["higher_education_course_type"]["options"],
            ["Foundation|预科", "Diploma", "Bachelor|本科", "Postgraduate|研究生", "Package|打包课程"],
        )
        blank = {name: "filled" for name in FIELD_MAP}
        blank["application_stream"] = ""
        self.assertIn("application_stream", missing_student_fields(blank))

        higher = dict(blank, application_stream="higher_education", higher_education_course_type="", applying_year_level="")
        missing = missing_student_fields(higher)
        self.assertIn("higher_education_course_type", missing)
        self.assertNotIn("applying_year_level", missing)

        school = dict(blank, application_stream="school_education", applying_year_level="", higher_education_course_type="")
        missing = missing_student_fields(school)
        self.assertIn("applying_year_level", missing)
        self.assertNotIn("higher_education_course_type", missing)

        html = parent_form("token", "2026-08-20", values={})
        self.assertIn('name="application_stream"', html)
        self.assertIn('<option value="">请选择</option>', html)
        self.assertIn('data-visible-for-stream="higher_education"', html)

    def test_application_cycle_api_allows_parallel_streams(self):
        from application_cycle import ensure_active_application_cycle

        conn = base_connection()
        apply_migration(conn)
        conn.execute("INSERT INTO students(student_id) VALUES('S2')")
        school = ensure_active_application_cycle(
            conn, "S2", "initial", "t", None, education_stream="school_education"
        )
        higher = ensure_active_application_cycle(
            conn, "S2", "initial", "t", None, education_stream="higher_education"
        )
        self.assertNotEqual(school.id, higher.id)
        self.assertEqual(school.education_stream, "school_education")
        self.assertEqual(higher.education_stream, "higher_education")


class HigherEducationWorkspaceTests(unittest.TestCase):
    def test_workspace_is_responsive_and_contract_not_applicable(self):
        from higher_education_views import render_higher_education_workspace

        html = render_higher_education_workspace(
            providers=[{"id": 1, "provider_name": "Example University", "provider_type": "University", "review_status": "Approved", "active_status": "Active"}],
            relationships=[], plans=[], applications=[], csrf="csrf", can_manage=True,
        )
        self.assertIn("高等教育申请", html)
        self.assertIn("无需合同／不适用", html)
        self.assertIn('class="he-card-grid"', html)
        self.assertIn('data-mobile-card="provider"', html)
        self.assertIn("升学路径提供机构", html)

    def test_confirmed_plan_exposes_submission_but_draft_does_not(self):
        from higher_education_views import render_higher_education_workspace

        common = dict(
            providers=[], relationships=[], applications=[], csrf="csrf", can_manage=True,
            cycles=[{"id": 11, "student_id": "S11"}],
            plan_items=[{
                "id": 7, "plan_id": 3, "course_type": "Bachelor",
                "course_name": "Bachelor of Arts", "intake": "2027-02",
            }],
        )
        draft = render_higher_education_workspace(
            plans=[{"id": 3, "application_cycle_id": 11, "status": "Draft"}], **common
        )
        confirmed = render_higher_education_workspace(
            plans=[{"id": 3, "application_cycle_id": 11, "status": "Confirmed"}], **common
        )
        self.assertIn('/higher-education/plans/3/confirm', draft)
        self.assertNotIn('/higher-education/plan-items/7/submit', draft)
        self.assertIn('/higher-education/plan-items/7/submit', confirmed)
        self.assertIn('name="submission_channel"', confirmed)

    def test_application_cards_expose_package_offer_coe_and_share_actions(self):
        from higher_education_views import render_higher_education_workspace

        html = render_higher_education_workspace(
            providers=[], relationships=[], plans=[], csrf="csrf", can_manage=True,
            applications=[{
                "id": 9, "application_cycle_id": 11, "provider_name": "University X",
                "course_type": "Package", "course_name": "Foundation + Bachelor",
                "intake": "2027-02", "submission_channel": "Master Agent", "status": "Submitted",
            }],
            offers=[{"id": 4, "application_id": 9, "status": "Received"}],
            temporary_shares=[{"id": 6, "application_id": 9, "status": "Pending", "share_percent": 12}],
            can_review=True, can_view_legacy_commission=True,
        )
        self.assertNotIn('/higher-education/applications/9/package-segments', html)
        self.assertIn('/higher-education/applications/9/temporary-shares', html)
        self.assertIn('/higher-education/temporary-shares/6/review', html)
        self.assertIn('/higher-education/applications/9/coe-links', html)
        self.assertIn('/higher-education/offers/4/accept', html)
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn(r'r"^/higher-education/applications/(\d+)/package-segments$"', source)
        self.assertIn(r'r"^/higher-education/temporary-shares/(\d+)/review$"', source)
        self.assertIn(r'r"^/higher-education/applications/(\d+)/coe-links$"', source)


class HigherEducationDryRunTests(unittest.TestCase):
    def test_dry_run_maps_definite_students_and_queues_uncertain_without_writes(self):
        from higher_education_migration import build_dry_run

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE students(student_id TEXT PRIMARY KEY,applying_year_level TEXT,source_token TEXT,form_data_json TEXT);
            CREATE TABLE contracts(id INTEGER PRIMARY KEY,student_id TEXT);
            CREATE TABLE application_cycles(id INTEGER PRIMARY KEY,student_id TEXT,status TEXT,cycle_type TEXT);
            INSERT INTO students VALUES('S1','本科','t1','{}');
            INSERT INTO students VALUES('S2','研究生','t2','{}');
            INSERT INTO students VALUES('S3','预科+本科','t3','{}');
            INSERT INTO students VALUES('S4','不确定','t4','{}');
            INSERT INTO students VALUES('S5','Year 10','t5','{}');
            INSERT INTO contracts(student_id) VALUES('S1');
            """
        )
        before = conn.total_changes
        report = build_dry_run(conn)
        self.assertEqual(report["definite_count"], 3)
        self.assertEqual(report["manual_review_count"], 1)
        self.assertEqual(
            {row["student_id"]: row["target_course_type"] for row in report["definite"]},
            {"S1": "Bachelor", "S2": "Postgraduate", "S3": "Package"},
        )
        self.assertTrue(report["definite"][0]["proposed_cycle"])
        self.assertEqual(report["manual_review"][0]["skip_reason"], "uncertain_requires_manual_review")
        self.assertEqual(report["excluded_legacy_cycle_repairs"], 89)
        self.assertEqual(conn.total_changes, before)


class HigherEducationWorkflowAdapterTests(unittest.TestCase):
    def test_facts_are_cycle_scoped_and_contract_is_not_applicable(self):
        from higher_education_workflow_adapter import contract_applicable, higher_education_facts

        conn = base_connection()
        apply_migration(conn)
        conn.execute("INSERT INTO students(student_id) VALUES('S8')")
        conn.execute("INSERT INTO application_cycles(cycle_uid,student_id,cycle_type,sequence_no,status,opened_at,created_at,updated_at,education_stream) VALUES('he8','S8','initial',1,'active','t','t','t','higher_education')")
        cycle_id = conn.execute("SELECT id FROM application_cycles").fetchone()[0]
        conn.execute("INSERT INTO users(id) VALUES(1)")
        repo = __import__("higher_education_repository").HigherEducationRepository(conn)
        provider = repo.create_provider("University X", "University", 1, "t")
        plan = repo.create_plan(cycle_id, 1, "t")
        item = repo.add_plan_item(plan, provider, "Bachelor", "Bachelor of Arts", "2027-02", 1, "t")
        repo.confirm_plan(plan, 1, "t")
        application = repo.submit_plan_item(item, "Direct", "2027-02", 1, "t")
        offer = repo.record_offer(application, "t")
        repo.accept_offer(offer, 1, "t")
        agent_provider = repo.create_provider("University Y", "University", 1, "t")
        agent_application = repo.create_application(
            cycle_id, agent_provider, "Bachelor", "Bachelor of Science", "2027-07",
            "Master Agent", 1, "t", master_agent_id=88,
        )
        share = repo.propose_temporary_commission_share(
            agent_application, 10, 1, "t", evidence="written approval request"
        )
        facts = higher_education_facts(conn, cycle_id)
        self.assertTrue(facts["plan_confirmed"])
        self.assertEqual(facts["submitted_applications"], 2)
        self.assertTrue(facts["offer_accepted"])
        self.assertTrue(facts["temporary_share_pending"])
        self.assertEqual(facts["commission_line_count"], 0)
        self.assertFalse(facts["commission_claimable"])
        self.assertFalse(contract_applicable(conn, cycle_id))

        repo.review_temporary_commission_share(share, "Approved", 1, "t2")
        line = repo.create_commission_line(
            agent_application, agent_provider, "Temporary Share", share, 1, "t2"
        )
        conn.execute(
            "UPDATE higher_education_commission_lines SET status='Claimable' WHERE id=?", (line,)
        )
        facts = higher_education_facts(conn, cycle_id)
        self.assertFalse(facts["temporary_share_pending"])
        self.assertEqual(facts["commission_line_count"], 1)
        self.assertTrue(facts["commission_claimable"])


if __name__ == "__main__":
    unittest.main()
