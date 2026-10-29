import sqlite3
import unittest
import json
import tempfile
from pathlib import Path


class SchoolPlanScopeTests(unittest.TestCase):
    """Catches scope rules leaking back into routes or accepting mixed plans."""

    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            CREATE TABLE school_cities (
              id INTEGER PRIMARY KEY,
              city_name TEXT NOT NULL,
              state TEXT,
              is_active INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE schools (
              id INTEGER PRIMARY KEY,
              school_name TEXT NOT NULL,
              city_id INTEGER,
              sector_key TEXT,
              state TEXT
            );
            INSERT INTO school_cities(id, city_name, state, is_active) VALUES
              (1, 'Melbourne', 'VIC', 1),
              (2, 'Sydney', 'NSW', 1);
            INSERT INTO schools(id, school_name, city_id, sector_key, state) VALUES
              (11, 'Melbourne Public A', 1, 'public', 'VIC'),
              (12, 'Melbourne Public B', 1, 'public', 'VIC'),
              (13, 'Melbourne Private A', 1, 'private', 'VIC'),
              (21, 'Sydney Public A', 2, 'public', 'NSW'),
              (31, 'Missing City School', NULL, 'public', ''),
              (32, 'Invalid Sector School', 1, 'government', 'VIC'),
              (41, 'Serpell Primary School', NULL, 'public', 'VIC'),
              (42, 'Templestowe College', NULL, 'public', 'vic'),
              (43, 'Glen Eira College', NULL, 'public', ' VIC ');
            """
        )

    def tearDown(self):
        self.connection.close()

    def test_same_city_public_scope_and_duplicate_ids_are_deduplicated(self):
        from school_plan_lifecycle import PlanScope, infer_plan_scope

        scope = infer_plan_scope(self.connection, [11, 12, 11])

        self.assertEqual(scope, PlanScope(city_id=1, city_name="Melbourne", sector="public"))

    def test_same_city_private_scope_is_supported(self):
        from school_plan_lifecycle import PlanScope, infer_plan_scope

        scope = infer_plan_scope(self.connection, [13])

        self.assertEqual(scope, PlanScope(city_id=1, city_name="Melbourne", sector="private"))

    def test_empty_selection_is_rejected(self):
        from school_plan_lifecycle import PlanScopeError, infer_plan_scope

        with self.assertRaises(PlanScopeError) as caught:
            infer_plan_scope(self.connection, [])

        self.assertEqual(caught.exception.code, "empty_selection")
        self.assertEqual(caught.exception.school_names, ())

    def test_unknown_school_id_is_rejected_without_guessing(self):
        from school_plan_lifecycle import PlanScopeError, infer_plan_scope

        with self.assertRaises(PlanScopeError) as caught:
            infer_plan_scope(self.connection, [11, 999])

        self.assertEqual(caught.exception.code, "unknown_school")
        self.assertEqual(caught.exception.school_names, ("学校记录 #999",))
        self.assertIn("#999", str(caught.exception))

    def test_missing_city_is_rejected_with_school_name(self):
        from school_plan_lifecycle import PlanScopeError, infer_plan_scope

        with self.assertRaises(PlanScopeError) as caught:
            infer_plan_scope(self.connection, [31])

        self.assertEqual(caught.exception.code, "unresolved_city")
        self.assertEqual(caught.exception.school_names, ("Missing City School",))

    def test_three_legacy_vic_schools_resolve_to_melbourne_without_writing_school_rows(self):
        """Catches regressions that force staff to repair every legacy VIC row manually."""
        from school_plan_lifecycle import PlanScope, PlanScopeError, infer_plan_scope

        try:
            scope = infer_plan_scope(self.connection, [41, 42, 43])
        except PlanScopeError as exc:
            self.fail(f"legacy VIC school scope must resolve safely: {exc}")

        self.assertEqual(
            scope,
            PlanScope(city_id=1, city_name="Melbourne", sector="public"),
        )
        stored_city_ids = self.connection.execute(
            "SELECT city_id FROM schools WHERE id IN (41,42,43) ORDER BY id"
        ).fetchall()
        self.assertEqual([row[0] for row in stored_city_ids], [None, None, None])

    def test_multiple_active_cities_for_one_state_are_not_guessed(self):
        """Catches unsafe state fallback after the city registry gains a second VIC city."""
        from school_plan_lifecycle import PlanScopeError, infer_plan_scope

        self.connection.execute(
            "INSERT INTO school_cities(id,city_name,state,is_active) VALUES(3,'Geelong','VIC',1)"
        )

        with self.assertRaises(PlanScopeError) as caught:
            infer_plan_scope(self.connection, [41])

        self.assertEqual(caught.exception.code, "unresolved_city")

    def test_invalid_sector_is_rejected_with_school_name(self):
        from school_plan_lifecycle import PlanScopeError, infer_plan_scope

        with self.assertRaises(PlanScopeError) as caught:
            infer_plan_scope(self.connection, [32])

        self.assertEqual(caught.exception.code, "unresolved_sector")
        self.assertEqual(caught.exception.school_names, ("Invalid Sector School",))

    def test_mixed_city_selection_is_rejected_with_conflicting_schools(self):
        from school_plan_lifecycle import PlanScopeError, infer_plan_scope

        with self.assertRaises(PlanScopeError) as caught:
            infer_plan_scope(self.connection, [11, 21])

        self.assertEqual(caught.exception.code, "mixed_city")
        self.assertEqual(
            caught.exception.school_names,
            ("Melbourne Public A", "Sydney Public A"),
        )

    def test_mixed_sector_selection_is_rejected_with_conflicting_schools(self):
        from school_plan_lifecycle import PlanScopeError, infer_plan_scope

        with self.assertRaises(PlanScopeError) as caught:
            infer_plan_scope(self.connection, [11, 13])

        self.assertEqual(caught.exception.code, "mixed_sector")
        self.assertEqual(
            caught.exception.school_names,
            ("Melbourne Public A", "Melbourne Private A"),
        )


class SchoolPlanActivationTests(unittest.TestCase):
    """Catches global supersession and partial-write regressions."""

    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript(
            """
            CREATE TABLE school_cities (id INTEGER PRIMARY KEY, city_name TEXT NOT NULL);
            CREATE TABLE schools (
              id INTEGER PRIMARY KEY,
              school_name TEXT NOT NULL,
              city_id INTEGER,
              sector_key TEXT
            );
            CREATE TABLE school_plan_reports (
              id INTEGER PRIMARY KEY,
              student_id TEXT NOT NULL,
              selected_school_ids TEXT NOT NULL,
              report_version INTEGER NOT NULL,
              status TEXT NOT NULL DEFAULT 'generated',
              city_id INTEGER,
              city_name_snapshot TEXT,
              plan_sector TEXT,
              is_current INTEGER NOT NULL DEFAULT 0,
              superseded_by_report_id INTEGER,
              scope_review_status TEXT NOT NULL DEFAULT 'pending',
              invalidated_at TEXT,
              invalidated_reason TEXT,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE school_plan_report_links (
              id INTEGER PRIMARY KEY,
              report_id INTEGER NOT NULL,
              status TEXT NOT NULL DEFAULT 'active',
              invalidated_at TEXT,
              invalidated_reason TEXT
            );
            CREATE TABLE school_plan_report_events (
              id INTEGER PRIMARY KEY,
              report_id INTEGER NOT NULL,
              event_type TEXT NOT NULL,
              event_by_user_id INTEGER,
              event_by_name TEXT,
              event_at TEXT NOT NULL,
              metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE UNIQUE INDEX uq_school_plan_current_public_city
              ON school_plan_reports(student_id, city_id)
              WHERE plan_sector='public' AND is_current=1;
            INSERT INTO school_cities(id, city_name) VALUES (1, 'Melbourne'), (2, 'Sydney');
            INSERT INTO schools(id, school_name, city_id, sector_key) VALUES
              (11, 'Melbourne Public A', 1, 'public'),
              (12, 'Melbourne Public B', 1, 'public'),
              (13, 'Melbourne Private A', 1, 'private'),
              (14, 'Melbourne Private B', 1, 'private'),
              (21, 'Sydney Public A', 2, 'public');
            """
        )
        self.actor = {"id": 7, "display_name": "Lifecycle Tester"}

    def tearDown(self):
        self.connection.close()

    def create_report(self, report_id, school_ids, version):
        self.connection.execute(
            """INSERT INTO school_plan_reports(
                 id,student_id,selected_school_ids,report_version,status,updated_at
               ) VALUES(?,?,?,?,?,?)""",
            (report_id, "S001", json.dumps(school_ids), version, "generated", "2026-08-06T12:00:00+10:00"),
        )
        self.connection.execute(
            "INSERT INTO school_plan_report_links(id,report_id,status) VALUES(?,?,?)",
            (report_id, report_id, "active"),
        )
        self.connection.commit()
        return report_id

    def report(self, report_id):
        return self.connection.execute(
            "SELECT * FROM school_plan_reports WHERE id=?", (report_id,)
        ).fetchone()

    def test_public_plans_are_current_per_city_and_only_same_city_is_superseded(self):
        from school_plan_lifecycle import activate_created_report

        melbourne_v1 = self.create_report(101, [11], 1)
        sydney_v1 = self.create_report(102, [21], 2)
        melbourne_v2 = self.create_report(103, [12], 3)

        self.assertEqual(activate_created_report(self.connection, melbourne_v1, self.actor).superseded_report_ids, ())
        self.assertEqual(activate_created_report(self.connection, sydney_v1, self.actor).superseded_report_ids, ())
        result = activate_created_report(self.connection, melbourne_v2, self.actor)

        self.assertEqual(result.superseded_report_ids, (melbourne_v1,))
        self.assertEqual(self.report(melbourne_v1)["status"], "superseded")
        self.assertEqual(self.report(melbourne_v1)["is_current"], 0)
        self.assertEqual(self.report(melbourne_v1)["superseded_by_report_id"], melbourne_v2)
        self.assertEqual(self.report(melbourne_v2)["is_current"], 1)
        self.assertEqual(self.report(sydney_v1)["is_current"], 1)
        old_link = self.connection.execute(
            "SELECT * FROM school_plan_report_links WHERE report_id=?", (melbourne_v1,)
        ).fetchone()
        self.assertEqual(old_link["status"], "superseded")

    def test_private_plans_in_same_city_remain_current_together(self):
        from school_plan_lifecycle import activate_created_report

        private_v1 = self.create_report(201, [13], 1)
        private_v2 = self.create_report(202, [14], 2)

        first = activate_created_report(self.connection, private_v1, self.actor)
        second = activate_created_report(self.connection, private_v2, self.actor)

        self.assertEqual(first.superseded_report_ids, ())
        self.assertEqual(second.superseded_report_ids, ())
        self.assertEqual(self.report(private_v1)["status"], "active")
        self.assertEqual(self.report(private_v1)["is_current"], 1)
        self.assertEqual(self.report(private_v2)["status"], "active")
        self.assertEqual(self.report(private_v2)["is_current"], 1)

    def test_link_invalidation_failure_rolls_back_every_report_change(self):
        from school_plan_lifecycle import activate_created_report

        old_report = self.create_report(301, [11], 1)
        new_report = self.create_report(302, [12], 2)
        activate_created_report(self.connection, old_report, self.actor)
        self.connection.executescript(
            """
            CREATE TRIGGER fail_link_invalidation
            BEFORE UPDATE OF status ON school_plan_report_links
            WHEN NEW.status='superseded'
            BEGIN
              SELECT RAISE(ABORT, 'synthetic link failure');
            END;
            """
        )

        with self.assertRaisesRegex(sqlite3.IntegrityError, "synthetic link failure"):
            activate_created_report(self.connection, new_report, self.actor)

        self.assertEqual(self.report(old_report)["status"], "active")
        self.assertEqual(self.report(old_report)["is_current"], 1)
        self.assertIsNone(self.report(old_report)["superseded_by_report_id"])
        self.assertEqual(self.report(new_report)["status"], "generated")
        self.assertEqual(self.report(new_report)["is_current"], 0)
        self.assertIsNone(self.report(new_report)["city_id"])


class SchoolPlanGenerationAdapterTests(unittest.TestCase):
    """Catches the generation route bypassing the lifecycle service."""

    def test_post_create_route_delegates_to_shared_generator(self):
        import app as app_module

        handler = object.__new__(app_module.CRMHandler)
        sentinel = object()
        handler.school_plan_generate = lambda: sentinel

        self.assertIs(handler.school_plan_report_create("POST"), sentinel)

    def test_generation_failure_stays_on_create_page_without_detail_redirect(self):
        import app as app_module

        old_db = app_module.db

        class MissingStudentDatabase:
            @staticmethod
            def one(_sql, _params=()):
                return None

        try:
            app_module.db = MissingStudentDatabase()
            handler = object.__new__(app_module.CRMHandler)
            handler.form = {"student_id": "MISSING"}
            captured = {}
            handler.flash_redirect = lambda location, message, kind="success": captured.update(
                {"location": location, "message": message, "kind": kind}
            )
            handler.toast_redirect = lambda *_args, **_kwargs: self.fail(
                "failed generation must not use the success detail redirect"
            )

            handler.school_plan_generate()

            self.assertEqual(captured["location"], "/school-plan-reports/create")
            self.assertEqual(captured["kind"], "error")
        finally:
            app_module.db = old_db

    def test_generated_report_is_scoped_and_supersedes_only_same_city_public_plan(self):
        import app as app_module
        from database import Database

        old_db = app_module.db
        try:
            with tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                database = Database(root / "aaie_crm.db")
                database.initialize()
                app_module.db = database
                stamp = "2026-08-06T12:00:00+10:00"
                database.execute(
                    """INSERT INTO students(
                         student_id,passport_first_name,passport_last_name,current_status,
                         created_date,updated_at,form_data_json
                       ) VALUES(?,?,?,?,?,?,?)""",
                    ("SCOPE001", "Scope", "Student", "New", stamp, stamp, "{}"),
                )
                melbourne_id = database.execute(
                    "INSERT INTO school_cities(city_name,state,country,sort_order,is_active) VALUES(?,?,?,?,?)",
                    ("Test Melbourne", "VIC", "Australia", 1, 1),
                )
                sydney_id = database.execute(
                    "INSERT INTO school_cities(city_name,state,country,sort_order,is_active) VALUES(?,?,?,?,?)",
                    ("Test Sydney", "NSW", "Australia", 2, 1),
                )
                melbourne_school = database.execute(
                    """INSERT INTO schools(
                         school_name,city_id,sector_key,school_type,created_at,updated_at
                       ) VALUES(?,?,?,?,?,?)""",
                    ("Test Melbourne Public", melbourne_id, "public", "Government", stamp, stamp),
                )
                sydney_school = database.execute(
                    """INSERT INTO schools(
                         school_name,city_id,sector_key,school_type,created_at,updated_at
                       ) VALUES(?,?,?,?,?,?)""",
                    ("Test Sydney Public", sydney_id, "public", "Government", stamp, stamp),
                )
                old_melbourne = database.execute(
                    """INSERT INTO school_plan_reports(
                         student_id,application_id,report_version,status,selected_school_ids,
                         city_id,city_name_snapshot,plan_sector,is_current,scope_review_status,
                         created_at,updated_at,snapshot_json
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "SCOPE001", "SCOPE001", 1, "active", json.dumps([melbourne_school]),
                        melbourne_id, "Test Melbourne", "public", 1, "resolved", stamp, stamp, "{}",
                    ),
                )
                old_sydney = database.execute(
                    """INSERT INTO school_plan_reports(
                         student_id,application_id,report_version,status,selected_school_ids,
                         city_id,city_name_snapshot,plan_sector,is_current,scope_review_status,
                         created_at,updated_at,snapshot_json
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "SCOPE001", "SCOPE001", 2, "active", json.dumps([sydney_school]),
                        sydney_id, "Test Sydney", "public", 1, "resolved", stamp, stamp, "{}",
                    ),
                )
                database.execute(
                    """INSERT INTO school_plan_report_links(
                         report_id,student_id,application_id,token_hash,token_last_4,share_url,
                         status,expires_at,created_at
                       ) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (old_melbourne, "SCOPE001", "SCOPE001", "hash-old", "old1", "https://old", "active", stamp, stamp),
                )

                handler = object.__new__(app_module.CRMHandler)
                handler.form = {
                    "student_id": "SCOPE001",
                    "school_ids": [str(melbourne_school)],
                    "report_modules": ["school_basic_info"],
                }
                handler.user = {"id": 1, "display_name": "Admin", "username": "admin", "role_name": "Admin"}
                handler.client_address = ("127.0.0.1", 12345)
                handler.headers = {"User-Agent": "unit-test"}
                handler.school_plan_build_snapshot = lambda student, school_ids, modules, version: {
                    "student": dict(student),
                    "schools": [],
                    "selected_modules": modules,
                    "report_version": version,
                }
                handler.school_plan_report_folder = lambda student_id, application_id: root
                handler.school_plan_render_html = lambda snapshot: "<html>test</html>"
                handler.school_plan_render_pdf = lambda snapshot, path: path.write_bytes(b"%PDF-test")
                redirect_race = {}

                def create_share_link_with_newer_report(report_id, regenerate, days):
                    redirect_race["generated_report_id"] = report_id
                    redirect_race["newer_report_id"] = database.execute(
                        """INSERT INTO school_plan_reports(
                             student_id,application_id,report_version,status,selected_school_ids,
                             city_id,city_name_snapshot,plan_sector,is_current,scope_review_status,
                             created_at,updated_at,snapshot_json
                           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            "SCOPE001", "SCOPE001", 99, "generated", json.dumps([sydney_school]),
                            sydney_id, "Test Sydney", "public", 0, "resolved", stamp, stamp, "{}",
                        ),
                    )
                    return {"share_url": f"https://example.test/{report_id}"}

                handler.school_plan_create_share_link = create_share_link_with_newer_report
                captured = {}
                handler.toast_redirect = lambda location, message, **kwargs: captured.update(
                    {"location": location, "message": message, **kwargs}
                )
                handler.flash_redirect = lambda location, message, kind="success": captured.update(
                    {"location": location, "message": message, "kind": kind}
                )

                handler.school_plan_generate()

                new_report = database.one(
                    "SELECT * FROM school_plan_reports WHERE id=?",
                    (redirect_race["generated_report_id"],),
                )
                old_melbourne_row = database.one(
                    "SELECT * FROM school_plan_reports WHERE id=?", (old_melbourne,)
                )
                old_sydney_row = database.one(
                    "SELECT * FROM school_plan_reports WHERE id=?", (old_sydney,)
                )
                self.assertEqual(new_report["city_id"], melbourne_id)
                self.assertEqual(new_report["plan_sector"], "public")
                self.assertEqual(new_report["is_current"], 1)
                self.assertEqual(old_melbourne_row["status"], "superseded")
                self.assertEqual(old_melbourne_row["superseded_by_report_id"], new_report["id"])
                self.assertEqual(old_sydney_row["status"], "active")
                self.assertEqual(old_sydney_row["is_current"], 1)
                self.assertEqual(new_report["id"], redirect_race["generated_report_id"])
                self.assertGreater(redirect_race["newer_report_id"], new_report["id"])
                self.assertEqual(
                    captured["location"],
                    f"/school-plan-reports/{redirect_race['generated_report_id']}",
                )
                self.assertNotEqual(
                    captured["location"],
                    f"/school-plan-reports/{redirect_race['newer_report_id']}",
                )
        finally:
            app_module.db = old_db


class SchoolPlanDetailViewTests(unittest.TestCase):
    """Catches the exact generated report losing its acceptance fields."""

    def test_detail_page_shows_exact_report_identity_and_keeps_return_to_list(self):
        import app as app_module

        report = {
            "id": 17,
            "student_id": "DETAIL001",
            "report_title": "School Plan Report",
            "report_version": 3,
            "status": "generated",
            "file_path": "/isolated/test-report.pdf",
            "created_by_name": "Test Consultant",
            "created_at": "2026-08-12T12:00:00+10:00",
        }

        class DetailDatabase:
            @staticmethod
            def one(sql, params=()):
                if "FROM school_plan_reports" in sql:
                    self.assertEqual(params, (17,))
                    return report
                if "FROM school_plan_report_links" in sql:
                    self.assertEqual(params, (17,))
                    return {"share_url": "https://example.test/detail-17"}
                raise AssertionError(f"unexpected detail query: {sql}")

        class TestNas:
            @staticmethod
            def status():
                return {"mode": "test"}

        old_db = app_module.db
        old_nas = app_module.nas
        old_page = app_module.page
        old_can_access_module = app_module.can_access_module
        try:
            app_module.db = DetailDatabase()
            app_module.nas = TestNas()
            app_module.page = lambda _title, body, *_args, **_kwargs: body
            app_module.can_access_module = lambda _user, module: module == "school_plan_reports"
            handler = object.__new__(app_module.CRMHandler)
            handler.user = {"id": 1, "role_name": "Admin"}
            handler.csrf = "test-csrf"
            handler.school_plan_snapshot = lambda _report: {
                "student": {"passport_first_name": "Exact", "passport_last_name": "Student"},
                "schools": [{"id": 101}, {"id": 102}],
                "selected_modules": ["school_basic_info"],
            }
            handler.school_plan_report_share_management_html = lambda _report: "<div>share</div>"
            handler.school_plan_report_snapshot_summary_html = lambda _report, _snapshot: "<div>summary</div>"
            handler.message = lambda: ("", "success")
            captured = {}
            handler.send_html = lambda html, *_args, **_kwargs: captured.update({"html": html})

            handler.school_plan_report_detail("GET", 17)

            html = captured["html"]
            self.assertIn("School Plan Report Detail", html)
            self.assertIn("Exact Student · v3", html)
            self.assertIn("<span>Schools</span><strong>2</strong>", html)
            self.assertIn("<span>Generated By</span><strong>Test Consultant</strong>", html)
            self.assertIn('href="/school-plan-reports">返回列表</a>', html)
        finally:
            app_module.db = old_db
            app_module.nas = old_nas
            app_module.page = old_page
            app_module.can_access_module = old_can_access_module


class SchoolPlanParentDecisionTests(unittest.TestCase):
    """Catches public ranking/private selection rules being mixed together."""

    def setUp(self):
        self.schools = (
            {"school_id": 11, "school_name": "School A", "rank_key": "school_1"},
            {"school_id": 12, "school_name": "School B", "rank_key": "school_2"},
            {"school_id": 13, "school_name": "School C", "rank_key": "school_3"},
        )

    def test_public_decision_requires_complete_unique_ranking(self):
        from school_plan_lifecycle import normalize_parent_decision

        decision = normalize_parent_decision(
            {"plan_sector": "public"},
            self.schools,
            {"rank_school_1": "2", "rank_school_2": "1", "rank_school_3": "3"},
        )

        self.assertEqual(decision.mode, "ranked")
        self.assertEqual(
            [(item.school_id, item.rank_order) for item in decision.schools],
            [(12, 1), (11, 2), (13, 3)],
        )

    def test_public_decision_rejects_missing_duplicate_and_out_of_range_ranks(self):
        from school_plan_lifecycle import ParentDecisionError, normalize_parent_decision

        bad_forms = (
            {"rank_school_1": "1", "rank_school_2": "2"},
            {"rank_school_1": "1", "rank_school_2": "1", "rank_school_3": "3"},
            {"rank_school_1": "1", "rank_school_2": "2", "rank_school_3": "4"},
        )
        for form in bad_forms:
            with self.subTest(form=form), self.assertRaises(ParentDecisionError):
                normalize_parent_decision({"plan_sector": "public"}, self.schools, form)

    def test_private_decision_accepts_one_or_more_selected_schools_without_ranking(self):
        from school_plan_lifecycle import normalize_parent_decision

        decision = normalize_parent_decision(
            {"plan_sector": "private"},
            self.schools,
            {"selected_school_ids": ["11", "13"]},
        )

        self.assertEqual(decision.mode, "selected")
        self.assertEqual([item.school_id for item in decision.schools], [11, 13])
        self.assertTrue(all(item.rank_order is None for item in decision.schools))

    def test_private_decision_rejects_empty_or_outside_selection(self):
        from school_plan_lifecycle import ParentDecisionError, normalize_parent_decision

        with self.assertRaises(ParentDecisionError):
            normalize_parent_decision({"plan_sector": "private"}, self.schools, {})
        with self.assertRaises(ParentDecisionError):
            normalize_parent_decision(
                {"plan_sector": "private"},
                self.schools,
                {"selected_school_ids": ["11", "999"]},
            )


class SchoolPlanParentConfirmationWriteTests(unittest.TestCase):
    """Catches partial confirmation writes and missing canonical decisions."""

    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript(
            """
            CREATE TABLE school_plan_reports (
              id INTEGER PRIMARY KEY,
              student_id TEXT NOT NULL,
              city_id INTEGER,
              city_name_snapshot TEXT,
              plan_sector TEXT,
              status TEXT NOT NULL,
              confirmed_at TEXT,
              confirmed_by_name TEXT,
              confirmation_ip TEXT,
              confirmation_user_agent TEXT,
              confirmation_note TEXT,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE school_plan_report_links (
              id INTEGER PRIMARY KEY,
              report_id INTEGER NOT NULL,
              confirmed_at TEXT
            );
            CREATE TABLE school_plan_report_confirmations (
              id INTEGER PRIMARY KEY,
              report_id INTEGER NOT NULL,
              viewer_id INTEGER,
              student_id TEXT NOT NULL,
              application_id TEXT NOT NULL,
              link_id INTEGER NOT NULL,
              confirmed_at TEXT NOT NULL,
              confirmed_ip TEXT,
              user_agent TEXT,
              wechat_openid TEXT,
              wechat_unionid TEXT,
              wechat_nickname TEXT,
              signature_image_path TEXT NOT NULL,
              confirmation_statement TEXT,
              status TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE school_plan_report_school_rankings (
              id INTEGER PRIMARY KEY,
              confirmation_id INTEGER NOT NULL,
              report_id INTEGER NOT NULL,
              school_id INTEGER,
              school_name TEXT NOT NULL,
              rank_order INTEGER NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE school_plan_confirmed_schools (
              id INTEGER PRIMARY KEY,
              confirmation_id INTEGER NOT NULL,
              report_id INTEGER NOT NULL,
              student_id TEXT NOT NULL,
              school_id INTEGER,
              school_name_snapshot TEXT NOT NULL,
              city_id INTEGER,
              city_name_snapshot TEXT,
              plan_sector TEXT NOT NULL,
              decision_mode TEXT NOT NULL,
              rank_order INTEGER,
              is_selected INTEGER NOT NULL,
              submission_status TEXT NOT NULL DEFAULT 'not_submitted',
              submitted_at TEXT,
              submitted_by_user_id INTEGER,
              submitted_by_name TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              UNIQUE(confirmation_id, school_id, school_name_snapshot)
            );
            CREATE TABLE school_plan_report_events (
              id INTEGER PRIMARY KEY,
              report_id INTEGER,
              link_id INTEGER,
              event_type TEXT NOT NULL,
              event_by_user_id INTEGER,
              event_by_name TEXT,
              event_at TEXT NOT NULL,
              ip_address TEXT,
              user_agent TEXT,
              metadata_json TEXT NOT NULL
            );
            INSERT INTO school_plan_reports(
              id,student_id,city_id,city_name_snapshot,plan_sector,status,updated_at
            ) VALUES(1,'S001',10,'Melbourne','public','active','before');
            INSERT INTO school_plan_report_links(id,report_id) VALUES(5,1);
            """
        )
        self.context = {
            "report_id": 1,
            "viewer_id": 9,
            "student_id": "S001",
            "application_id": "S001",
            "link_id": 5,
            "confirmed_at": "2026-08-06T13:00:00+10:00",
            "confirmed_ip": "127.0.0.1",
            "user_agent": "unit-test",
            "wechat_openid": "openid",
            "wechat_unionid": "unionid",
            "wechat_nickname": "Parent",
            "signature_image_path": "/tmp/signature.png",
            "confirmation_statement": "confirmed",
            "confirmed_by_name": "Parent",
        }
        self.schools = (
            {"school_id": 11, "school_name": "School A", "rank_key": "school_1"},
            {"school_id": 12, "school_name": "School B", "rank_key": "school_2"},
        )

    def tearDown(self):
        self.connection.close()

    def test_public_confirmation_writes_legacy_and_canonical_rows_atomically(self):
        from school_plan_lifecycle import normalize_parent_decision, save_parent_confirmation

        decision = normalize_parent_decision(
            {"plan_sector": "public"},
            self.schools,
            {"rank_school_1": "2", "rank_school_2": "1"},
        )
        confirmation_id = save_parent_confirmation(self.connection, self.context, decision)

        legacy = self.connection.execute(
            "SELECT school_id,rank_order FROM school_plan_report_school_rankings ORDER BY rank_order"
        ).fetchall()
        canonical = self.connection.execute(
            "SELECT school_id,decision_mode,rank_order FROM school_plan_confirmed_schools ORDER BY rank_order"
        ).fetchall()
        self.assertGreater(confirmation_id, 0)
        self.assertEqual([(row["school_id"], row["rank_order"]) for row in legacy], [(12, 1), (11, 2)])
        self.assertEqual(
            [(row["school_id"], row["decision_mode"], row["rank_order"]) for row in canonical],
            [(12, "ranked", 1), (11, "ranked", 2)],
        )
        self.assertEqual(
            self.connection.execute("SELECT status FROM school_plan_reports WHERE id=1").fetchone()[0],
            "confirmed",
        )

    def test_confirmation_calls_workflow_v2_lifecycle_gateway_when_schema_is_available(self):
        from unittest.mock import patch
        from school_plan_lifecycle import normalize_parent_decision, save_parent_confirmation

        self.connection.execute("CREATE TABLE application_cycles(id INTEGER PRIMARY KEY)")
        decision = normalize_parent_decision(
            {"plan_sector": "public"},
            self.schools,
            {"rank_school_1": "1", "rank_school_2": "2"},
        )
        with patch("application_cycle.attach_school_plan_confirmation") as attach:
            confirmation_id = save_parent_confirmation(self.connection, self.context, decision)

        attach.assert_called_once_with(
            self.connection,
            confirmation_id,
            occurred_at=self.context["confirmed_at"],
            actor_user_id=None,
            actor_name="Parent",
        )

    def test_event_failure_rolls_back_confirmation_and_report_status(self):
        from school_plan_lifecycle import normalize_parent_decision, save_parent_confirmation

        decision = normalize_parent_decision(
            {"plan_sector": "public"},
            self.schools,
            {"rank_school_1": "1", "rank_school_2": "2"},
        )
        self.connection.executescript(
            """
            CREATE TRIGGER fail_confirmation_event
            BEFORE INSERT ON school_plan_report_events
            BEGIN
              SELECT RAISE(ABORT, 'synthetic confirmation event failure');
            END;
            """
        )

        with self.assertRaisesRegex(sqlite3.IntegrityError, "synthetic confirmation event failure"):
            save_parent_confirmation(self.connection, self.context, decision)

        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM school_plan_report_confirmations").fetchone()[0],
            0,
        )
        self.assertEqual(
            self.connection.execute("SELECT status FROM school_plan_reports WHERE id=1").fetchone()[0],
            "active",
        )


class SchoolPlanParentUIAdapterTests(unittest.TestCase):
    """Catches the parent page rendering public ranking controls for private plans."""

    def test_private_plan_renders_multi_select_confirmation_controls(self):
        import app as app_module
        from database import Database

        old_db = app_module.db
        try:
            with tempfile.TemporaryDirectory() as folder:
                database = Database(Path(folder) / "aaie_crm.db")
                database.initialize()
                app_module.db = database
                stamp = "2026-08-06T14:00:00+10:00"
                database.execute(
                    """INSERT INTO students(
                         student_id,passport_first_name,passport_last_name,current_status,
                         created_date,updated_at,form_data_json
                       ) VALUES(?,?,?,?,?,?,?)""",
                    ("PRIVATE001", "Private", "Student", "New", stamp, stamp, "{}"),
                )
                report_id = database.execute(
                    """INSERT INTO school_plan_reports(
                         student_id,application_id,report_version,status,selected_school_ids,
                         city_name_snapshot,plan_sector,is_current,scope_review_status,
                         created_at,updated_at,snapshot_json
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "PRIVATE001", "PRIVATE001", 1, "active", "[11,12]",
                        "Melbourne", "private", 1, "resolved", stamp, stamp, "{}",
                    ),
                )
                handler = object.__new__(app_module.CRMHandler)
                handler.user = {"id": 1, "display_name": "Admin"}
                handler.client_address = ("127.0.0.1", 12345)
                handler.headers = {"User-Agent": "unit-test"}
                handler.path = "/school-plan-share/test"
                handler.send_html = lambda html, *args, **kwargs: html
                viewer = handler.school_plan_create_report_viewer(
                    report_id, "Primary Parent", "Parent", "", "", can_confirm=True, is_primary=True
                )
                link_data = handler.school_plan_create_share_link(
                    report_id, regenerate=True, days=7, viewer_id=viewer["id"]
                )
                link = handler.school_plan_share_row_by_token(link_data["plain_token"])
                snapshot = {
                    "student": {"student_id": "PRIVATE001"},
                    "schools": [
                        {"basic_info": {"id": 11, "school_name": "Private School A"}},
                        {"basic_info": {"id": 12, "school_name": "Private School B"}},
                    ],
                }
                handler.school_plan_snapshot = lambda source: snapshot
                handler.school_plan_snapshot_report_html = lambda snapshot_arg, report=None: "<html><style></style><body><main>Report</main></body></html>"

                html = handler.school_plan_share_viewer_html(link_data["plain_token"], link)

                self.assertIn("最终私校选择确认", html)
                self.assertIn('name="selected_school_ids" value="11"', html)
                self.assertIn('name="selected_school_ids" value="12"', html)
                self.assertNotIn('name="rank_school_1"', html)
                self.assertIn("可选择一所或多所", html)
        finally:
            app_module.db = old_db


class SchoolPlanSubmissionServiceTests(unittest.TestCase):
    """Catches duplicate, partial, or unaudited school submission changes."""

    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            CREATE TABLE school_plan_confirmed_schools (
              id INTEGER PRIMARY KEY,
              confirmation_id INTEGER NOT NULL,
              report_id INTEGER NOT NULL,
              student_id TEXT NOT NULL,
              school_name_snapshot TEXT NOT NULL,
              submission_status TEXT NOT NULL DEFAULT 'not_submitted',
              submitted_at TEXT,
              submitted_by_user_id INTEGER,
              submitted_by_name TEXT,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE school_plan_school_submission_events (
              id INTEGER PRIMARY KEY,
              confirmed_school_id INTEGER NOT NULL,
              student_id TEXT NOT NULL,
              report_id INTEGER NOT NULL,
              previous_submission_status TEXT NOT NULL,
              new_submission_status TEXT NOT NULL,
              submitted_at TEXT,
              submitted_by_user_id INTEGER,
              submitted_by_name TEXT,
              occurred_at TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TRIGGER write_submission_event
            AFTER UPDATE OF submission_status ON school_plan_confirmed_schools
            WHEN OLD.submission_status <> NEW.submission_status
            BEGIN
              INSERT INTO school_plan_school_submission_events(
                confirmed_school_id,student_id,report_id,
                previous_submission_status,new_submission_status,
                submitted_at,submitted_by_user_id,submitted_by_name,
                occurred_at,created_at
              ) VALUES(
                NEW.id,NEW.student_id,NEW.report_id,
                OLD.submission_status,NEW.submission_status,
                CASE WHEN NEW.submission_status='submitted' THEN NEW.submitted_at END,
                CASE WHEN NEW.submission_status='submitted' THEN NEW.submitted_by_user_id END,
                CASE WHEN NEW.submission_status='submitted' THEN NEW.submitted_by_name END,
                NEW.updated_at,NEW.updated_at
              );
            END;
            INSERT INTO school_plan_confirmed_schools(
              id,confirmation_id,report_id,student_id,school_name_snapshot,updated_at
            ) VALUES(41,7,3,'S001','School A','before');
            """
        )
        self.actor = {"id": 9, "display_name": "Consultant A"}

    def tearDown(self):
        self.connection.close()

    def test_mark_submitted_and_repeated_request_is_idempotent(self):
        from school_plan_lifecycle import set_school_submission

        first = set_school_submission(
            self.connection, 41, True, self.actor, "2026-08-06T15:00:00+10:00"
        )
        second = set_school_submission(
            self.connection, 41, True, self.actor, "2026-08-06T15:05:00+10:00"
        )

        self.assertEqual(first.submission_status, "submitted")
        self.assertEqual(first.submitted_at, "2026-08-06T15:00:00+10:00")
        self.assertFalse(first.unchanged)
        self.assertTrue(second.unchanged)
        self.assertEqual(second.submitted_at, first.submitted_at)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM school_plan_school_submission_events"
            ).fetchone()[0],
            1,
        )

    def test_revert_clears_current_evidence_but_preserves_both_events(self):
        from school_plan_lifecycle import set_school_submission

        set_school_submission(
            self.connection, 41, True, self.actor, "2026-08-06T15:00:00+10:00"
        )
        state = set_school_submission(
            self.connection, 41, False, self.actor, "2026-08-06T16:00:00+10:00"
        )

        self.assertEqual(state.submission_status, "not_submitted")
        self.assertIsNone(state.submitted_at)
        self.assertIsNone(state.submitted_by_user_id)
        self.assertEqual(
            [tuple(row) for row in self.connection.execute(
                """SELECT previous_submission_status,new_submission_status
                     FROM school_plan_school_submission_events ORDER BY id"""
            )],
            [("not_submitted", "submitted"), ("submitted", "not_submitted")],
        )

    def test_missing_confirmed_school_is_rejected(self):
        from school_plan_lifecycle import set_school_submission

        with self.assertRaises(LookupError):
            set_school_submission(self.connection, 999, True, self.actor)

    def test_event_failure_rolls_back_submission_state(self):
        from school_plan_lifecycle import set_school_submission

        self.connection.executescript(
            """
            CREATE TRIGGER fail_submission_event
            BEFORE INSERT ON school_plan_school_submission_events
            BEGIN
              SELECT RAISE(ABORT, 'synthetic submission event failure');
            END;
            """
        )

        with self.assertRaisesRegex(sqlite3.IntegrityError, "synthetic submission event failure"):
            set_school_submission(
                self.connection, 41, True, self.actor, "2026-08-06T15:00:00+10:00"
            )

        row = self.connection.execute(
            "SELECT submission_status,submitted_at FROM school_plan_confirmed_schools WHERE id=41"
        ).fetchone()
        self.assertEqual(tuple(row), ("not_submitted", None))


class SchoolPlanSubmissionRouteTests(unittest.TestCase):
    """Catches cross-student writes at the HTTP adapter boundary."""

    class DbAdapter:
        def __init__(self, connection):
            self.connection = connection

        def one(self, sql, params=()):
            return self.connection.execute(sql, params).fetchone()

        def transaction(self):
            connection = self.connection

            class Transaction:
                def __enter__(self):
                    return connection

                def __exit__(self, exc_type, exc, traceback):
                    if exc_type is None:
                        connection.commit()
                    else:
                        connection.rollback()

            return Transaction()

        def audit(self, *args, **kwargs):
            return None

    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            CREATE TABLE school_plan_confirmed_schools (
              id INTEGER PRIMARY KEY,confirmation_id INTEGER NOT NULL,
              report_id INTEGER NOT NULL,student_id TEXT NOT NULL,
              school_name_snapshot TEXT NOT NULL,
              submission_status TEXT NOT NULL DEFAULT 'not_submitted',
              submitted_at TEXT,submitted_by_user_id INTEGER,
              submitted_by_name TEXT,updated_at TEXT NOT NULL
            );
            CREATE TABLE school_plan_reports (
              id INTEGER PRIMARY KEY,student_id TEXT NOT NULL,
              plan_sector TEXT NOT NULL,is_current INTEGER NOT NULL
            );
            CREATE TABLE school_plan_school_submission_events (
              id INTEGER PRIMARY KEY,confirmed_school_id INTEGER NOT NULL,
              student_id TEXT NOT NULL,report_id INTEGER NOT NULL,
              previous_submission_status TEXT NOT NULL,new_submission_status TEXT NOT NULL,
              submitted_at TEXT,submitted_by_user_id INTEGER,submitted_by_name TEXT,
              occurred_at TEXT NOT NULL,created_at TEXT NOT NULL
            );
            CREATE TRIGGER write_submission_event
            AFTER UPDATE OF submission_status ON school_plan_confirmed_schools
            WHEN OLD.submission_status <> NEW.submission_status
            BEGIN
              INSERT INTO school_plan_school_submission_events(
                confirmed_school_id,student_id,report_id,
                previous_submission_status,new_submission_status,
                submitted_at,submitted_by_user_id,submitted_by_name,
                occurred_at,created_at
              ) VALUES(
                NEW.id,NEW.student_id,NEW.report_id,
                OLD.submission_status,NEW.submission_status,
                NEW.submitted_at,NEW.submitted_by_user_id,NEW.submitted_by_name,
                NEW.updated_at,NEW.updated_at
              );
            END;
            INSERT INTO school_plan_confirmed_schools(
              id,confirmation_id,report_id,student_id,school_name_snapshot,updated_at
            ) VALUES(41,7,3,'S001','School A','before');
            INSERT INTO school_plan_reports VALUES(3,'S001','public',1);
            """
        )

    def tearDown(self):
        self.connection.close()

    def _handler(self, form):
        import app as app_module

        handler = object.__new__(app_module.CRMHandler)
        handler.user = {"id": 9, "display_name": "Consultant A", "role_name": "Consultant"}
        handler.form = form
        handler.client_address = ("127.0.0.1", 12345)
        handler.headers = {"User-Agent": "unit-test"}
        handler.json_response = lambda payload, status=200: (status, payload)
        return handler

    def test_route_updates_owned_school_and_returns_json_state(self):
        import app as app_module

        old_db = app_module.db
        old_access = app_module.can_access_module
        try:
            app_module.db = self.DbAdapter(self.connection)
            app_module.can_access_module = lambda user, module: module == "school_plan_reports"
            status, payload = self._handler({"submitted": "1"}).school_plan_school_submission(
                "POST", "S001", "41"
            )
        finally:
            app_module.db = old_db
            app_module.can_access_module = old_access

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["submission_status"], "submitted")

    def test_route_rejects_school_owned_by_another_student(self):
        import app as app_module

        old_db = app_module.db
        old_access = app_module.can_access_module
        try:
            app_module.db = self.DbAdapter(self.connection)
            app_module.can_access_module = lambda user, module: module == "school_plan_reports"
            status, payload = self._handler({"submitted": "1"}).school_plan_school_submission(
                "POST", "S999", "41"
            )
        finally:
            app_module.db = old_db
            app_module.can_access_module = old_access

        self.assertEqual(status, 404)
        self.assertFalse(payload["ok"])
        self.assertEqual(
            self.connection.execute(
                "SELECT submission_status FROM school_plan_confirmed_schools WHERE id=41"
            ).fetchone()[0],
            "not_submitted",
        )

    def test_private_submission_route_returns_business_conflict_instead_of_500(self):
        import app as app_module
        from application_cycle import CycleConflictError
        from unittest.mock import patch

        old_db = app_module.db
        old_access = app_module.can_access_module
        try:
            app_module.db = self.DbAdapter(self.connection)
            app_module.can_access_module = lambda user, module: module == "school_plan_reports"
            with patch(
                "app.set_school_submission",
                side_effect=CycleConflictError("school plan ownership conflict"),
            ):
                status, payload = self._handler({"submitted": "1"}).school_plan_school_submission(
                    "POST", "S001", "41"
                )
        finally:
            app_module.db = old_db
            app_module.can_access_module = old_access

        self.assertEqual(400, status)
        self.assertFalse(payload["ok"])
        self.assertIn("ownership conflict", payload["message"])

    def test_public_submission_route_returns_business_conflict_instead_of_500(self):
        import app as app_module
        from application_cycle import CycleConflictError
        from unittest.mock import patch

        old_db = app_module.db
        old_access = app_module.can_access_module
        try:
            app_module.db = self.DbAdapter(self.connection)
            app_module.can_access_module = lambda user, module: module == "school_plan_reports"
            with patch(
                "app.mark_public_group_submitted",
                side_effect=CycleConflictError("school plan ownership conflict"),
            ):
                status, payload = self._handler({}).school_plan_public_group_submission(
                    "POST", "S001", "3"
                )
        finally:
            app_module.db = old_db
            app_module.can_access_module = old_access

        self.assertEqual(400, status)
        self.assertFalse(payload["ok"])
        self.assertIn("ownership conflict", payload["message"])


class SchoolPlanPrivateConfirmationPdfTests(unittest.TestCase):
    """Catches private selections disappearing from the signed confirmation PDF."""

    def test_private_selected_school_is_printed_without_legacy_ranking_rows(self):
        import app as app_module
        from database import Database
        from PIL import Image
        from pypdf import PdfReader

        old_db = app_module.db
        old_dir = app_module.SCHOOL_PLAN_CONFIRMATION_DIR
        try:
            with tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                database = Database(root / "aaie_crm.db")
                database.initialize()
                app_module.db = database
                app_module.SCHOOL_PLAN_CONFIRMATION_DIR = root / "confirmations"
                stamp = "2026-08-06T17:00:00+10:00"
                database.execute(
                    """INSERT INTO students(
                         student_id,passport_last_name,passport_first_name,current_status,
                         created_date,updated_at,form_data_json
                       ) VALUES(?,?,?,?,?,?,?)""",
                    ("PRIVATEPDF", "Student", "Private", "Active", stamp, stamp, "{}"),
                )
                report_id = database.execute(
                    """INSERT INTO school_plan_reports(
                         student_id,application_id,report_version,status,selected_school_ids,
                         plan_sector,is_current,scope_review_status,created_at,updated_at,snapshot_json
                       ) VALUES(?,?,?,?,?,'private',1,'resolved',?,?,?)""",
                    ("PRIVATEPDF", "PRIVATEPDF", 1, "confirmed", "[]", stamp, stamp,
                     json.dumps({"student": {"passport_first_name": "Private", "passport_last_name": "Student"}})),
                )
                link_id = database.execute(
                    """INSERT INTO school_plan_report_links(
                         report_id,student_id,application_id,token_hash,token_last_4,
                         share_url,status,expires_at,created_at
                       ) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (report_id, "PRIVATEPDF", "PRIVATEPDF", "private-hash", "hash",
                     "https://example.test/private", "confirmed", stamp, stamp),
                )
                signature = root / "signature.png"
                Image.new("RGB", (360, 120), "white").save(signature)
                confirmation_id = database.execute(
                    """INSERT INTO school_plan_report_confirmations(
                         report_id,student_id,application_id,link_id,confirmed_at,
                         wechat_openid,wechat_nickname,signature_image_path,
                         confirmation_statement,status,created_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,'confirmed',?,?)""",
                    (report_id, "PRIVATEPDF", "PRIVATEPDF", link_id, stamp, "openid",
                     "Parent", str(signature), "selected", stamp, stamp),
                )
                database.execute(
                    """INSERT INTO school_plan_confirmed_schools(
                         confirmation_id,report_id,student_id,school_name_snapshot,
                         plan_sector,decision_mode,is_selected,created_at,updated_at
                       ) VALUES(?,?,?,'Private Selected School','private','selected',1,?,?)""",
                    (confirmation_id, report_id, "PRIVATEPDF", stamp, stamp),
                )

                pdf_path = object.__new__(app_module.CRMHandler).school_plan_generate_confirmation_pdf(
                    confirmation_id
                )

                pdf_text = "\n".join(page.extract_text() or "" for page in PdfReader(str(pdf_path)).pages)
                self.assertIn("Private Selected School", pdf_text)
        finally:
            app_module.db = old_db
            app_module.SCHOOL_PLAN_CONFIRMATION_DIR = old_dir


if __name__ == "__main__":
    unittest.main()
