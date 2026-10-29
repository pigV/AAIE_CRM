import sqlite3
import shutil
import tempfile
import unittest
from pathlib import Path

from tests.test_higher_education_module import apply_migration, base_connection


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_V2 = ROOT / "migrations" / "20260813_019_higher_education_module_v2.sql"
ROLLBACK_V2 = ROOT / "migrations" / "rollback" / "20260813_019_higher_education_module_v2_rollback.sql"


def apply_v2(conn):
    conn.executescript(MIGRATION_V2.read_text(encoding="utf-8"))


class HigherEducationV2MigrationTests(unittest.TestCase):
    def setUp(self):
        self.conn = base_connection()
        apply_migration(self.conn)
        self.conn.execute("INSERT INTO users(id) VALUES(1)")
        self.conn.execute("INSERT INTO students(student_id,application_stream,higher_education_course_type) VALUES('S13','higher_education','Package')")
        self.conn.execute(
            "INSERT INTO application_cycles(id,cycle_uid,student_id,cycle_type,sequence_no,status,opened_at,created_at,updated_at,education_stream) VALUES(13,'he-13','S13','initial',1,'active','t','t','t','higher_education')"
        )
        self.university_id = self.conn.execute(
            "SELECT id FROM higher_education_providers WHERE normalized_name='monash university'"
        ).fetchone()[0]

    def tearDown(self):
        self.conn.close()

    def test_additive_schema_preserves_existing_cycle_and_adds_v2_authorities(self):
        self.assertTrue(MIGRATION_V2.is_file())
        apply_v2(self.conn)

        self.assertEqual(
            tuple(self.conn.execute("SELECT id,cycle_uid,student_id FROM application_cycles WHERE id=13").fetchone()),
            (13, "he-13", "S13"),
        )
        student_columns = {row[1] for row in self.conn.execute("PRAGMA table_info(students)")}
        self.assertTrue({"higher_education_package_combination", "parent_requested_pathway"} <= student_columns)
        plan_columns = {row[1] for row in self.conn.execute("PRAGMA table_info(higher_education_plans)")}
        self.assertIn("default_pathway_code", plan_columns)
        item_columns = {row[1] for row in self.conn.execute("PRAGMA table_info(higher_education_plan_items)")}
        self.assertTrue({"pathway_combination_id", "course_id"} <= item_columns)
        segment_columns = {row[1] for row in self.conn.execute("PRAGMA table_info(higher_education_package_segments)")}
        self.assertTrue({"target_university_id", "course_id", "is_final_degree"} <= segment_columns)

    def test_university_pathways_have_ordered_provider_segments(self):
        apply_v2(self.conn)
        combination = self.conn.execute(
            """INSERT INTO higher_education_pathway_combinations(
                   university_id,pathway_code,display_name,final_course_level,active_status,created_at,updated_at
                 ) VALUES(?,?,?,?,?,?,?)""",
            (self.university_id, "test-foundation-bachelor", "Test Foundation + Bachelor", "Bachelor", "Active", "t", "t"),
        ).lastrowid
        college = self.conn.execute(
            "SELECT id FROM higher_education_providers WHERE normalized_name='monash college'"
        ).fetchone()[0]
        self.conn.execute(
            "INSERT INTO higher_education_pathway_segments(pathway_combination_id,segment_order,provider_id,course_level,target_university_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (combination, 1, college, "Foundation", self.university_id, "t", "t"),
        )
        self.conn.execute(
            "INSERT INTO higher_education_pathway_segments(pathway_combination_id,segment_order,provider_id,course_level,target_university_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (combination, 2, self.university_id, "Bachelor", self.university_id, "t", "t"),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO higher_education_pathway_segments(pathway_combination_id,segment_order,provider_id,course_level,target_university_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (combination, 2, self.university_id, "Bachelor", self.university_id, "t", "t"),
            )

    def test_course_library_duplicate_key_is_university_name_and_level(self):
        apply_v2(self.conn)
        values = (self.university_id, "Bachelor of Commerce", "bachelor of commerce", "Bachelor", "Active", 1, "t", 1, "t")
        self.conn.execute(
            """INSERT INTO higher_education_courses(
                 university_id,course_name,normalized_name,course_level,active_status,
                 created_by_user_id,created_at,updated_by_user_id,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            values,
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """INSERT INTO higher_education_courses(
                     university_id,course_name,normalized_name,course_level,active_status,
                     created_by_user_id,created_at,updated_by_user_id,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (self.university_id, " bachelor  OF commerce ", "bachelor of commerce", "Bachelor", "Active", 1, "t", 1, "t"),
            )
        self.conn.execute(
            """INSERT INTO higher_education_courses(
                 university_id,course_name,normalized_name,course_level,active_status,
                 created_by_user_id,created_at,updated_by_user_id,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (self.university_id, "Master of Commerce", "master of commerce", "Master", "Active", 1, "t", 1, "t"),
        )

    def test_official_initial_pathway_architecture_is_maintainable_data(self):
        apply_v2(self.conn)
        names = {
            row[0]
            for row in self.conn.execute(
                "SELECT display_name FROM higher_education_pathway_combinations WHERE active_status='Active'"
            )
        }
        self.assertTrue({"Foundation + Bachelor", "Diploma + Bachelor", "Direct Bachelor", "Direct Master"} <= names)
        seeded_universities = self.conn.execute(
            "SELECT count(DISTINCT university_id) FROM higher_education_pathway_combinations WHERE source_url LIKE 'https://%'"
        ).fetchone()[0]
        self.assertGreaterEqual(seeded_universities, 4)
        self.assertEqual(
            self.conn.execute(
                """SELECT count(*) FROM higher_education_pathway_segments s
                   LEFT JOIN higher_education_providers p ON p.id=s.provider_id
                   WHERE p.id IS NULL"""
            ).fetchone()[0],
            0,
        )

    def test_migration_runner_is_idempotent_and_checksum_verified(self):
        import migration_runner

        with tempfile.TemporaryDirectory(dir=ROOT) as temp_name:
            temp_root = Path(temp_name)
            database_path = temp_root / "candidate.sqlite3"
            seed = base_connection()
            apply_migration(seed)
            target = sqlite3.connect(database_path)
            seed.backup(target)
            target.close()
            seed.close()
            package = temp_root / "migrations"
            checks = package / "checks"
            checks.mkdir(parents=True)
            shutil.copy2(MIGRATION_V2, package / MIGRATION_V2.name)
            check_path = ROOT / "migrations" / "checks" / "20260813_019_higher_education_module_v2_forward_check.sql"
            shutil.copy2(check_path, checks / check_path.name)

            first = migration_runner.apply_migrations(
                database_path, package, applied_by="he-v2-test", source_commit="candidate", formal_database_path=None
            )
            second = migration_runner.apply_migrations(
                database_path, package, applied_by="he-v2-test", source_commit="candidate", formal_database_path=None
            )
            verified = migration_runner.verify_migrations(database_path, package)

            self.assertEqual(first["applied"], ["20260813_019"])
            self.assertEqual(second["applied"], [])
            self.assertEqual(verified["verified"], ["20260813_019"])
            self.assertEqual(verified["foreign_key_violations"], [])

    def test_additive_migration_preserves_thirteen_existing_he_cycles(self):
        conn = base_connection()
        apply_migration(conn)
        for number in range(1, 14):
            student_id = f"HE{number:02d}"
            conn.execute(
                "INSERT INTO students(student_id,application_stream,higher_education_course_type) VALUES(?,?,?)",
                (student_id, "higher_education", "Bachelor"),
            )
            conn.execute(
                """INSERT INTO application_cycles(
                     id,cycle_uid,student_id,cycle_type,sequence_no,status,opened_at,created_at,updated_at,education_stream
                   ) VALUES(?,?,?,?,1,'active','t','t','t','higher_education')""",
                (100 + number, f"he-existing-{number}", student_id, "initial"),
            )
        before = [
            tuple(row)
            for row in conn.execute(
                "SELECT id,cycle_uid,student_id,status FROM application_cycles WHERE education_stream='higher_education' ORDER BY id"
            )
        ]

        apply_v2(conn)

        after = [
            tuple(row)
            for row in conn.execute(
                "SELECT id,cycle_uid,student_id,status FROM application_cycles WHERE education_stream='higher_education' ORDER BY id"
            )
        ]
        self.assertEqual(len(after), 13)
        self.assertEqual(after, before)
        conn.close()

    def test_rollback_package_preserves_preexisting_cycles(self):
        conn = base_connection()
        apply_migration(conn)
        conn.execute(
            "INSERT INTO students(student_id,application_stream,higher_education_course_type) VALUES('ROLLBACK-HE','higher_education','Bachelor')"
        )
        conn.execute(
            """INSERT INTO application_cycles(
                 id,cycle_uid,student_id,cycle_type,sequence_no,status,opened_at,created_at,updated_at,education_stream
               ) VALUES(90,'rollback-he','ROLLBACK-HE','initial',1,'active','t','t','t','higher_education')"""
        )
        apply_v2(conn)

        conn.executescript(ROLLBACK_V2.read_text(encoding="utf-8"))

        self.assertEqual(
            tuple(conn.execute("SELECT id,cycle_uid,student_id,status FROM application_cycles WHERE id=90").fetchone()),
            (90, "rollback-he", "ROLLBACK-HE", "active"),
        )
        self.assertIsNone(
            conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='higher_education_courses'").fetchone()
        )
        conn.close()


class HigherEducationV2ParentPathwayTests(unittest.TestCase):
    def test_parent_package_requires_combination_and_uses_plus_display(self):
        from app import missing_student_fields
        from fields import FIELD_MAP
        from higher_education_repository import derive_parent_requested_pathway
        from ui import parent_form

        data = {name: "filled" for name in FIELD_MAP}
        data.update(
            application_stream="higher_education",
            higher_education_course_type="Package",
            higher_education_package_combination="",
            applying_year_level="",
        )
        self.assertIn("higher_education_package_combination", missing_student_fields(data))
        data["higher_education_package_combination"] = "Foundation + Diploma + Bachelor"
        self.assertNotIn("higher_education_package_combination", missing_student_fields(data))
        self.assertEqual(
            derive_parent_requested_pathway("Package", data["higher_education_package_combination"]),
            "Foundation + Diploma + Bachelor",
        )
        self.assertEqual(derive_parent_requested_pathway("Bachelor", ""), "Direct Bachelor")
        self.assertEqual(derive_parent_requested_pathway("Postgraduate", ""), "Direct Master")

        html = parent_form("token", "2026-08-20", values={})
        self.assertIn('name="higher_education_package_combination"', html)
        self.assertIn('data-visible-for-course-type="Package"', html)
        self.assertIn("Foundation + Bachelor", html)
        self.assertNotIn("Foundation → Bachelor", html)

    def test_new_plan_snapshots_parent_requested_pathway_without_changing_cycle(self):
        from higher_education_repository import HigherEducationRepository

        conn = base_connection()
        apply_migration(conn)
        apply_v2(conn)
        conn.execute("INSERT INTO users(id) VALUES(1)")
        conn.execute(
            "INSERT INTO students(student_id,application_stream,higher_education_course_type,parent_requested_pathway) VALUES('S1','higher_education','Package','Foundation + Bachelor')"
        )
        conn.execute(
            "INSERT INTO application_cycles(id,cycle_uid,student_id,cycle_type,sequence_no,status,opened_at,created_at,updated_at,education_stream) VALUES(44,'he','S1','initial',1,'active','t','t','t','higher_education')"
        )
        plan_id = HigherEducationRepository(conn).create_plan(44, 1, "t")
        self.assertEqual(
            conn.execute("SELECT default_pathway_code FROM higher_education_plans WHERE id=?", (plan_id,)).fetchone()[0],
            "Foundation + Bachelor",
        )
        self.assertEqual(conn.execute("SELECT id FROM application_cycles WHERE cycle_uid='he'").fetchone()[0], 44)
        conn.close()


class HigherEducationV2RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.conn = base_connection()
        apply_migration(self.conn)
        apply_v2(self.conn)
        self.conn.execute("INSERT INTO users(id) VALUES(1)")
        self.conn.execute(
            "INSERT INTO students(student_id,application_stream,higher_education_course_type,parent_requested_pathway) VALUES('S1','higher_education','Package','Foundation + Bachelor')"
        )
        self.conn.execute(
            "INSERT INTO application_cycles(id,cycle_uid,student_id,cycle_type,sequence_no,status,opened_at,created_at,updated_at,education_stream) VALUES(1,'he','S1','initial',1,'active','t','t','t','higher_education')"
        )
        self.repo = __import__("higher_education_repository").HigherEducationRepository(self.conn)
        self.university = self.conn.execute(
            "SELECT id FROM higher_education_providers WHERE normalized_name='monash university'"
        ).fetchone()[0]
        self.college = self.conn.execute(
            "SELECT id FROM higher_education_providers WHERE normalized_name='monash college'"
        ).fetchone()[0]

    def tearDown(self):
        self.conn.close()

    def test_course_library_reuses_normalized_duplicate_and_supports_keyword_search(self):
        first, created = self.repo.create_or_get_course(
            self.university, "Bachelor of Commerce", "Bachelor", 1, "t"
        )
        duplicate, duplicate_created = self.repo.create_or_get_course(
            self.university, "  BACHELOR   of commerce ", "Bachelor", 1, "t2"
        )
        self.repo.create_or_get_course(
            self.university, "Master of Business Analytics", "Master", 1, "t"
        )
        self.assertTrue(created)
        self.assertFalse(duplicate_created)
        self.assertEqual(first, duplicate)
        self.assertEqual(
            [row["course_name"] for row in self.repo.search_courses(self.university, "commerce")],
            ["Bachelor of Commerce"],
        )
        self.assertEqual(
            [row["course_name"] for row in self.repo.search_courses(self.university, "BUSINESS")],
            ["Master of Business Analytics"],
        )

    def test_each_university_item_owns_actual_pathway_and_package_materializes_ordered_segments(self):
        pathway = self.repo.create_pathway_combination(
            self.university,
            "test-package-foundation-bachelor",
            "Test Foundation + Bachelor",
            "Bachelor",
            1,
            "t",
            source_url="https://example.test/pathway",
        )
        self.repo.add_pathway_segment(pathway, 1, self.college, "Foundation", 1, "t")
        self.repo.add_pathway_segment(pathway, 2, self.university, "Bachelor", 1, "t")
        course, _ = self.repo.create_or_get_course(
            self.university, "Bachelor of Commerce", "Bachelor", 1, "t"
        )
        plan = self.repo.create_plan(1, 1, "t")
        item = self.repo.add_plan_item(
            plan,
            self.university,
            "Package",
            "Bachelor of Commerce",
            "February 2027",
            1,
            "t",
            pathway_combination_id=pathway,
            course_id=course,
        )
        self.repo.confirm_plan(plan, 1, "t")
        application = self.repo.submit_plan_item(item, "Direct", "February 2027", 1, "t")
        actual = self.conn.execute(
            "SELECT pathway_combination_id,course_id FROM higher_education_applications WHERE id=?",
            (application,),
        ).fetchone()
        self.assertEqual(tuple(actual), (pathway, course))
        segments = self.conn.execute(
            """SELECT segment_order,teaching_provider_id,course_type,target_university_id,is_final_degree
               FROM higher_education_package_segments WHERE application_id=? ORDER BY segment_order""",
            (application,),
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in segments],
            [
                (1, self.college, "Foundation", self.university, 0),
                (2, self.university, "Bachelor", self.university, 1),
            ],
        )

    def test_pathway_from_another_university_cannot_be_used_for_item(self):
        other = self.repo.create_provider("Other University", "University", 1, "t", review_status="Approved")
        pathway = self.repo.create_pathway_combination(
            other, "direct-bachelor", "Direct Bachelor", "Bachelor", 1, "t"
        )
        self.repo.add_pathway_segment(pathway, 1, other, "Bachelor", 1, "t")
        plan = self.repo.create_plan(1, 1, "t")
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.add_plan_item(
                plan, self.university, "Bachelor", "Bachelor of Commerce", "February 2027",
                1, "t", pathway_combination_id=pathway,
            )

    def test_draft_plan_item_can_be_removed_without_touching_cycle(self):
        pathway = self.repo.create_pathway_combination(
            self.university, "test-direct-bachelor", "Test Direct Bachelor", "Bachelor", 1, "t"
        )
        self.repo.add_pathway_segment(pathway, 1, self.university, "Bachelor", 1, "t")
        plan = self.repo.create_plan(1, 1, "t")
        item = self.repo.add_plan_item(
            plan, self.university, "Bachelor", "Bachelor of Commerce", "February 2027",
            1, "t", pathway_combination_id=pathway,
        )

        self.repo.remove_draft_plan_item(item)

        self.assertIsNone(self.conn.execute("SELECT id FROM higher_education_plan_items WHERE id=?", (item,)).fetchone())
        self.assertEqual(self.conn.execute("SELECT status FROM application_cycles WHERE id=1").fetchone()[0], "active")

    def test_admin_can_deactivate_and_merge_duplicate_course(self):
        canonical, _ = self.repo.create_or_get_course(
            self.university, "Bachelor of Commerce", "Bachelor", 1, "t"
        )
        duplicate, _ = self.repo.create_or_get_course(
            self.university, "Bachelor of Commerce (BCom)", "Bachelor", 1, "t"
        )

        self.repo.merge_course(duplicate, canonical, 1, "t2")

        row = self.conn.execute(
            "SELECT active_status,merged_into_course_id FROM higher_education_courses WHERE id=?",
            (duplicate,),
        ).fetchone()
        self.assertEqual(tuple(row), ("Inactive", canonical))

    def test_confirmed_plan_cannot_accept_more_universities(self):
        pathway = self.repo.create_pathway_combination(
            self.university, "locked-direct-bachelor", "Locked Direct Bachelor", "Bachelor", 1, "t"
        )
        self.repo.add_pathway_segment(pathway, 1, self.university, "Bachelor", 1, "t")
        plan = self.repo.create_plan(1, 1, "t")
        self.repo.confirm_plan(plan, 1, "t")
        with self.assertRaisesRegex(Exception, "Draft"):
            self.repo.add_plan_item(
                plan, self.university, "Bachelor", "Bachelor of Commerce", "February 2027",
                1, "t", pathway_combination_id=pathway,
            )

    def test_pathway_architecture_requires_related_provider_and_matching_terminal_level(self):
        unrelated = self.repo.create_provider(
            "Unrelated College", "Pathway Provider", 1, "t", review_status="Approved"
        )
        pathway = self.repo.create_pathway_combination(
            self.university, "bad-terminal", "Foundation + Bachelor", "Bachelor", 1, "t"
        )
        with self.assertRaisesRegex(Exception, "relationship"):
            self.repo.add_pathway_segment(pathway, 1, unrelated, "Foundation", 1, "t")
        self.repo.add_pathway_segment(pathway, 1, self.college, "Foundation", 1, "t")
        self.repo.add_pathway_segment(pathway, 2, self.university, "Diploma", 1, "t")
        plan = self.repo.create_plan(1, 1, "t")
        self.repo.add_plan_item(
            plan, self.university, "Package", "Bachelor of Commerce", "February 2027",
            1, "t", pathway_combination_id=pathway,
        )
        with self.assertRaisesRegex(Exception, "final course level"):
            self.repo.confirm_plan(plan, 1, "t")

    def test_package_identity_uses_ordered_architecture_not_display_text(self):
        from higher_education_repository import course_type_for_pathway

        self.assertEqual(course_type_for_pathway("foundation-bachelor", 2, "Bachelor"), "Package")
        self.assertEqual(course_type_for_pathway("direct-bachelor", 1, "Bachelor"), "Bachelor")
        self.assertEqual(course_type_for_pathway("direct-master", 1, "Master"), "Postgraduate")

    def test_manual_package_segment_mutation_is_disabled(self):
        with self.assertRaisesRegex(Exception, "architecture"):
            self.repo.add_package_segment(1, 1, self.college, "Foundation", "Foundation", "February 2027", 1, "t")

    def test_update_scope_triggers_reject_cross_university_references(self):
        other = self.repo.create_provider("Other University", "University", 1, "t", review_status="Approved")
        pathway = self.repo.create_pathway_combination(
            self.university, "update-direct", "Direct Bachelor", "Bachelor", 1, "t"
        )
        self.repo.add_pathway_segment(pathway, 1, self.university, "Bachelor", 1, "t")
        plan = self.repo.create_plan(1, 1, "t")
        item = self.repo.add_plan_item(
            plan, self.university, "Bachelor", "Bachelor of Commerce", "February 2027",
            1, "t", pathway_combination_id=pathway,
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE higher_education_plan_items SET provider_id=? WHERE id=?", (other, item))


class HigherEducationV2ViewsTests(unittest.TestCase):
    def test_school_management_university_views_are_admin_scoped_and_maintainable(self):
        from higher_education_admin_views import (
            render_australian_universities_card,
            render_australian_universities_list,
            render_university_detail,
            render_university_edit,
        )

        card = render_australian_universities_card(42)
        self.assertIn("Universities", card)
        self.assertNotIn("Australian Universities", card)
        self.assertIn('/schools/universities', card)
        listing = render_australian_universities_list(
            [{"id": 1, "provider_name": "Monash University", "short_name": "Monash", "state": "VIC", "city": "Melbourne", "active_status": "Active", "pathway_count": 4}],
            can_manage=False,
        )
        self.assertIn("Monash University", listing)
        self.assertIn("Available Pathways", listing)
        self.assertNotIn("新增 University", listing)
        university = {"id": 1, "provider_name": "Monash University", "short_name": "Monash", "state": "VIC", "city": "Melbourne", "active_status": "Active"}
        detail = render_university_detail(
            university=university,
            pathways=[{"id": 3, "display_name": "Foundation + Bachelor", "final_course_level": "Bachelor", "active_status": "Active", "source_url": "https://example.test"}],
            segments=[{"pathway_combination_id": 3, "segment_order": 1, "provider_name": "Monash College", "course_level": "Foundation"}],
            courses=[{"id": 8, "course_name": "Bachelor of Commerce", "course_level": "Bachelor", "active_status": "Active"}],
            providers=[{"id": 2, "provider_name": "Monash College"}],
            relationships=[], csrf="x", can_manage=True,
        )
        self.assertIn("Manage Pathways", detail)
        self.assertIn("Courses / Programs", detail)
        self.assertIn("Foundation + Bachelor", detail)
        self.assertIn("Monash College", detail)
        self.assertIn('action="/schools/universities/1/pathways"', detail)
        self.assertIn('action="/schools/universities/1/courses"', detail)
        self.assertNotIn('action="/schools/universities/1/update"', detail)
        self.assertIn('href="/schools/universities/1/edit?', detail)
        edit = render_university_edit(university=university, csrf="x")
        self.assertIn('action="/schools/universities/1/update"', edit)
        self.assertIn('action="/schools/universities/1/courses/8/status"', detail)

    def test_staff_workspace_removes_registry_creation_and_uses_multi_university_cards(self):
        from higher_education_views import render_higher_education_workspace

        html = render_higher_education_workspace(
            providers=[{"id": 1, "provider_name": "Monash University", "provider_type": "University", "review_status": "Approved", "active_status": "Active"}],
            relationships=[], plans=[{"id": 3, "application_cycle_id": 1, "status": "Draft", "default_pathway_code": "Foundation + Bachelor", "student_id": "S1"}],
            applications=[], csrf="x", can_manage=True, cycles=[], plan_items=[{
                "id": 11, "plan_id": 3, "provider_id": 1, "provider_name": "Monash University",
                "pathway_name": "Foundation + Bachelor", "course_name": "Bachelor of Commerce",
                "intake": "February 2027", "status": "Draft",
            }],
            pathways=[{"id": 4, "university_id": 1, "display_name": "Foundation + Bachelor", "final_course_level": "Bachelor"}],
            courses=[{"id": 5, "university_id": 1, "course_name": "Bachelor of Commerce", "course_level": "Bachelor"}],
        )
        self.assertIn("默认升学路径", html)
        self.assertIn("Foundation + Bachelor", html)
        self.assertIn("添加大学", html)
        self.assertIn("搜索课程 / 专业", html)
        self.assertIn("可添加新课程 / 专业", html)
        self.assertIn("data-he-pathway-source", html)
        self.assertIn("data-he-course-source", html)
        self.assertIn('"final_level": "Bachelor"', html)
        self.assertIn("data-he-course-level", html)
        self.assertIn('action="/higher-education/plan-items/', html)
        self.assertNotIn("新增待审核机构", html)
        self.assertNotIn("建立大学—Pathway 关系", html)
        self.assertNotIn("内部高等教育方案", html)
        self.assertNotIn("package-segments", html)
        self.assertNotIn("→", html)

    def test_standalone_contract_conversion_blocks_higher_education_intake_before_student_creation(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        method = source[source.index("    def contract_center_convert_to_student"):source.index("    def contract_generate", source.index("    def contract_center_convert_to_student"))]
        self.assertIn("Higher Education 学生不适用留学服务合同", method)
        self.assertLess(method.index("Higher Education 学生不适用留学服务合同"), method.index("create_student_from_form"))

    def test_higher_education_assets_are_loaded_only_for_workspace(self):
        from ui import page

        html = page("HE", '<section data-higher-education-workspace></section>')
        plain = page("Plain", "<section></section>")
        self.assertIn("/assets/higher-education.css", html)
        self.assertIn("/assets/higher-education.js", html)
        self.assertNotIn("/assets/higher-education.js", plain)


class HigherEducationV2StudentExperienceTests(unittest.TestCase):
    def test_he_contract_tab_explains_disabled_state_without_hover(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        disabled_tab = source[source.index('class="he-contract-disabled"'):source.index("</button>", source.index('class="he-contract-disabled"'))]
        self.assertIn("无需合同", disabled_tab.split(">", 1)[1])

    def test_contract_and_k12_report_are_disabled_only_for_he_only_student(self):
        from higher_education_workflow_adapter import student_higher_education_experience

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE application_cycles(
              id INTEGER PRIMARY KEY, student_id TEXT, education_stream TEXT, status TEXT
            );
            INSERT INTO application_cycles VALUES(1,'HE','higher_education','active');
            INSERT INTO application_cycles VALUES(2,'DUAL','higher_education','active');
            INSERT INTO application_cycles VALUES(3,'DUAL','school_education','active');
            INSERT INTO application_cycles VALUES(4,'K12','school_education','active');
            """
        )
        self.assertEqual(
            student_higher_education_experience(conn, "HE"),
            {"higher_education_active": True, "school_education_active": False, "contract_applicable": False, "k12_report_applicable": False},
        )
        self.assertTrue(student_higher_education_experience(conn, "DUAL")["contract_applicable"])
        self.assertTrue(student_higher_education_experience(conn, "K12")["k12_report_applicable"])
        conn.close()

        legacy = sqlite3.connect(":memory:")
        self.assertTrue(student_higher_education_experience(legacy, "LEGACY")["contract_applicable"])
        legacy.close()


if __name__ == "__main__":
    unittest.main()
