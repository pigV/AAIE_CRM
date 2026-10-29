import tempfile
import unittest
from pathlib import Path

import app as app_module
from database import Database


class PrivateInternationalFeesMVPTests(unittest.TestCase):
    def test_schema_and_data_source_seed_exist(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "aaie_crm.db")
            db.initialize()

            tables = {
                row["name"]
                for row in db.all("SELECT name FROM sqlite_master WHERE type='table'")
            }
            self.assertTrue({
                "school_international_fee_records",
                "school_international_fee_items",
                "school_international_fee_fetch_sources",
            }.issubset(tables))

            record_columns = {
                row["name"]
                for row in db.all("PRAGMA table_info(school_international_fee_records)")
            }
            self.assertTrue({
                "data_update_job_id",
                "school_id",
                "school_name",
                "state",
                "fee_year",
                "source_url",
                "file_url",
                "source_type",
                "file_hash",
                "raw_text",
                "confidence_score",
                "verification_status",
                "is_current",
            }.issubset(record_columns))

            item_columns = {
                row["name"]
                for row in db.all("PRAGMA table_info(school_international_fee_items)")
            }
            self.assertTrue({
                "fee_record_id",
                "fee_category",
                "year_level",
                "amount",
                "amount_period",
                "currency",
                "is_compulsory",
                "raw_text",
                "confidence_score",
            }.issubset(item_columns))

            source = db.one(
                """SELECT * FROM data_sources
                   WHERE source_type='private_international_fees' AND state='ALL'"""
            )
            self.assertIsNotNone(source)
            self.assertEqual(source["source_name"], "Private School International Student Fees")
            self.assertEqual(source["requires_admin_confirmation"], 1)

    def test_vic_mvp_selects_ten_private_schools_with_websites(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "aaie_crm.db")
            db.initialize()
            stamp = "2026-07-01T12:00:00+10:00"
            for index in range(12):
                db.execute(
                    """INSERT INTO schools(
                       school_name,state,sector_key,school_type,website_url,
                       international_student_available,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        f"VIC Private Fee School {index:02d}",
                        "VIC",
                        "private",
                        "Pri/Sec",
                        f"https://fees-school-{index}.example.edu.au",
                        1 if index < 3 else 0,
                        stamp,
                        stamp,
                    ),
                )
            db.execute(
                """INSERT INTO schools(school_name,state,sector_key,school_type,website_url,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?)""",
                ("NSW Private Fee School", "NSW", "private", "Private", "https://nsw.example.edu.au", stamp, stamp),
            )

            rows = app_module.private_fee_mvp_school_rows(db, state="VIC", limit=10)

            self.assertEqual(len(rows), 10)
            self.assertTrue(all(row["state"] == "VIC" for row in rows))
            self.assertTrue(all(row["website_url"] for row in rows))
            self.assertEqual([row["international_student_available"] for row in rows[:3]], [1, 1, 1])

    def test_all_private_fee_school_rows_selects_all_states_without_mvp_limit(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "aaie_crm.db")
            db.initialize()
            stamp = "2026-07-01T12:00:00+10:00"
            for state in ("VIC", "NSW", "QLD"):
                for index in range(5):
                    db.execute(
                        """INSERT INTO schools(
                           school_name,state,sector_key,school_type,website_url,
                           international_student_available,created_at,updated_at
                        ) VALUES(?,?,?,?,?,?,?,?)""",
                        (
                            f"{state} Private Fee School {index:02d}",
                            state,
                            "private",
                            "Pri/Sec",
                            f"https://{state.lower()}-fees-school-{index}.example.edu.au",
                            1 if index == 0 else 0,
                            stamp,
                            stamp,
                        ),
                    )
            db.execute(
                """INSERT INTO schools(school_name,state,sector_key,school_type,website_url,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?)""",
                ("VIC Government School", "VIC", "public", "Government", "https://public.example.edu.au", stamp, stamp),
            )

            rows = app_module.private_fee_school_rows(db)

            self.assertEqual(len(rows), 15)
            self.assertEqual({row["state"] for row in rows}, {"VIC", "NSW", "QLD"})
            self.assertNotIn("VIC Government School", [row["school_name"] for row in rows])

    def test_private_fee_data_update_source_prefers_all_scope_over_legacy_vic(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "aaie_crm.db")
            db.initialize()
            stamp = "2026-07-01T12:00:00+10:00"
            db.execute("DELETE FROM data_sources WHERE source_name='Private School International Student Fees'")
            for state, frequency in (("VIC", "annual MVP pilot"), ("ALL", "annual full private school batch")):
                db.execute(
                    """INSERT INTO data_sources(
                       source_name,source_type,state,source_url,update_frequency,auto_check_enabled,
                       requires_admin_confirmation,status,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "Private School International Student Fees",
                        "private_international_fees",
                        state,
                        "",
                        frequency,
                        0,
                        1,
                        "Manual Only",
                        stamp,
                        stamp,
                    ),
                )

            source = app_module.data_update_source_for_action_key(db, "private_international_fees")

            self.assertEqual(source["state"], "ALL")

    def test_vic_mvp_excludes_non_k12_private_providers(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "aaie_crm.db")
            db.initialize()
            stamp = "2026-07-01T12:00:00+10:00"
            db.execute(
                """INSERT INTO schools(
                   school_name,state,sector_key,school_type,year_levels,website_url,
                   international_student_available,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    "Acknowledge Education",
                    "VIC",
                    "private",
                    "Private",
                    "Year 11-Year 12",
                    "https://www.acknowledgeeducation.edu.au/",
                    1,
                    stamp,
                    stamp,
                ),
            )
            db.execute(
                """INSERT INTO schools(
                   school_name,state,sector_key,school_type,year_levels,website_url,
                   international_student_available,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    "ELTHAM College",
                    "VIC",
                    "private",
                    "Private",
                    "Year 1-Year 12",
                    "https://www.elthamcollege.vic.edu.au",
                    1,
                    stamp,
                    stamp,
                ),
            )

            rows = app_module.private_fee_mvp_school_rows(db, state="VIC", limit=10)
            names = [row["school_name"] for row in rows]

            self.assertNotIn("Acknowledge Education", names)
            self.assertIn("ELTHAM College", names)

    def test_mvp_stage_writes_review_records_not_production_school_fees(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "aaie_crm.db")
            db.initialize()
            stamp = "2026-07-01T12:00:00+10:00"
            school_id = db.execute(
                """INSERT INTO schools(
                   school_name,state,sector_key,school_type,website_url,
                   international_student_available,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    "Codex VIC International Fee College",
                    "VIC",
                    "private",
                    "Private",
                    "https://codex-fees.example.edu.au",
                    1,
                    stamp,
                    stamp,
                ),
            )
            job_id = app_module.data_update_create_job(db, "private_international_fees", user_id=1)

            report = app_module.stage_private_international_fee_mvp(
                db,
                job_id,
                school_rows=[db.one("SELECT * FROM schools WHERE id=?", (school_id,))],
                source_payloads={
                    school_id: """
                    2026 International Student Fees
                    Years 7-10 Tuition Fee AUD 38,500 per year
                    Application fee AUD 450 payable with application.
                    """
                },
                user_id=1,
            )

            self.assertEqual(report["records_found"], 1)
            self.assertEqual(report["affected_school_count"], 1)
            self.assertEqual(report["records_written_to_crm"], 0)
            self.assertEqual(db.one("SELECT COUNT(*) count FROM school_fees")["count"], 0)

            record = db.one("SELECT * FROM school_international_fee_records WHERE data_update_job_id=?", (job_id,))
            self.assertEqual(record["school_id"], school_id)
            self.assertIn(record["verification_status"], {"needs_review", "verified"})
            self.assertGreater(record["confidence_score"], 0)

            item_count = db.one(
                "SELECT COUNT(*) count FROM school_international_fee_items WHERE fee_record_id=?",
                (record["id"],),
            )["count"]
            self.assertGreaterEqual(item_count, 1)

    def test_confirm_publishes_reviewed_fee_items_to_school_fees(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "aaie_crm.db")
            db.initialize()
            stamp = "2026-07-01T12:00:00+10:00"
            school_id = db.execute(
                """INSERT INTO schools(
                   school_name,state,sector_key,school_type,website_url,
                   international_student_available,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    "Codex Confirmed Fee College",
                    "VIC",
                    "private",
                    "Private",
                    "https://confirmed-fees.example.edu.au",
                    1,
                    stamp,
                    stamp,
                ),
            )
            job_id = app_module.data_update_create_job(db, "private_international_fees", user_id=1)
            report = app_module.stage_private_international_fee_mvp(
                db,
                job_id,
                school_rows=[db.one("SELECT * FROM schools WHERE id=?", (school_id,))],
                source_payloads={
                    school_id: """
                    2026 International Student Fees
                    Years 7-10 Tuition Fee AUD 38,500 per year
                    Application fee AUD 450 payable with application.
                    """
                },
                user_id=1,
            )
            app_module.data_update_write_preview(db, job_id, report)
            app_module.data_update_finish_job(db, job_id, "Needs Confirmation", report)

            changed = app_module.confirm_data_update_job(db, job_id, user_id=1)

            self.assertGreaterEqual(changed, 1)
            published = db.all("SELECT * FROM school_fees WHERE school_id=? ORDER BY id", (school_id,))
            self.assertGreaterEqual(len(published), 1)
            self.assertTrue(all(row["review_status"] == "Confirmed" for row in published))
            school = db.one("SELECT * FROM schools WHERE id=?", (school_id,))
            self.assertEqual(school["international_tuition_fee_currency"], "AUD")
            self.assertTrue(school["fee_source_url"])

    def test_confirm_replaces_existing_private_fee_rows_without_duplicates(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "aaie_crm.db")
            db.initialize()
            stamp = "2026-07-01T12:00:00+10:00"
            school_id = db.execute(
                """INSERT INTO schools(
                   school_name,state,sector_key,school_type,website_url,
                   international_student_available,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    "Codex Existing Fee College",
                    "VIC",
                    "private",
                    "Pri/Sec",
                    "https://existing-fees.example.edu.au",
                    1,
                    stamp,
                    stamp,
                ),
            )
            db.execute(
                """INSERT INTO school_fees(
                   school_id,source_name,source_url,fetched_at,review_status,fee_type,year_level,
                   amount,currency,fee_year,raw_text,notes,created_at,updated_at,updated_by
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    school_id,
                    app_module.PRIVATE_FEE_SOURCE_NAME,
                    "https://old.example.edu.au",
                    stamp,
                    "Confirmed",
                    "Tuition",
                    "Year 7",
                    "1,111",
                    "AUD",
                    "2025",
                    "old row",
                    "old row",
                    stamp,
                    stamp,
                    1,
                ),
            )
            job_id = app_module.data_update_create_job(db, "private_international_fees", user_id=1)
            report = app_module.stage_private_international_fee_mvp(
                db,
                job_id,
                school_rows=[db.one("SELECT * FROM schools WHERE id=?", (school_id,))],
                source_payloads={
                    school_id: """
                    Scale of Fees for Overseas Students for 2026.
                    Year Level Year 7 Year 8
                    Annual Tuition Fee $44,338 $44,338
                    """
                },
                user_id=1,
            )
            app_module.data_update_write_preview(db, job_id, report)
            app_module.data_update_finish_job(db, job_id, "Needs Confirmation", report)

            changed = app_module.confirm_data_update_job(db, job_id, user_id=1)

            self.assertEqual(changed, 2)
            published = db.all("SELECT * FROM school_fees WHERE school_id=? AND source_name=? ORDER BY year_level", (school_id, app_module.PRIVATE_FEE_SOURCE_NAME))
            self.assertEqual(len(published), 2)
            self.assertEqual([row["amount"] for row in published], ["44,338", "44,338"])

    def test_mvp_does_not_fabricate_fee_items_when_source_has_no_amounts(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "aaie_crm.db")
            db.initialize()
            stamp = "2026-07-01T12:00:00+10:00"
            school_id = db.execute(
                """INSERT INTO schools(
                   school_name,state,sector_key,school_type,website_url,
                   international_student_available,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    "Codex No Fee Amount College",
                    "VIC",
                    "private",
                    "Private",
                    "https://no-fee-amount.example.edu.au",
                    1,
                    stamp,
                    stamp,
                ),
            )
            job_id = app_module.data_update_create_job(db, "private_international_fees", user_id=1)

            report = app_module.stage_private_international_fee_mvp(
                db,
                job_id,
                school_rows=[db.one("SELECT * FROM schools WHERE id=?", (school_id,))],
                source_payloads={school_id: "International admissions page. Contact registrar for current fees."},
                user_id=1,
            )

            self.assertEqual(report["records_found"], 1)
            record = db.one("SELECT * FROM school_international_fee_records WHERE data_update_job_id=?", (job_id,))
            self.assertEqual(record["verification_status"], "not_found")
            self.assertEqual(
                db.one("SELECT COUNT(*) count FROM school_international_fee_items WHERE fee_record_id=?", (record["id"],))["count"],
                0,
            )

    def test_mvp_does_not_verify_application_only_international_pages(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "aaie_crm.db")
            db.initialize()
            stamp = "2026-07-01T12:00:00+10:00"
            school_id = db.execute(
                """INSERT INTO schools(
                   school_name,state,sector_key,school_type,website_url,
                   international_student_available,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    "Codex Application Only College",
                    "VIC",
                    "private",
                    "Pri/Sec",
                    "https://application-only.example.edu.au",
                    1,
                    stamp,
                    stamp,
                ),
            )
            job_id = app_module.data_update_create_job(db, "private_international_fees", user_id=1)

            app_module.stage_private_international_fee_mvp(
                db,
                job_id,
                school_rows=[db.one("SELECT * FROM schools WHERE id=?", (school_id,))],
                source_payloads={
                    school_id: """
                    2026 International students scholarships and enrolment process.
                    Application fee AUD 120 payable before assessment.
                    CRICOS provider information.
                    """
                },
                user_id=1,
            )

            record = db.one("SELECT * FROM school_international_fee_records WHERE data_update_job_id=?", (job_id,))
            self.assertNotEqual(record["verification_status"], "verified")
            tuition_count = db.one(
                """SELECT COUNT(*) count
                   FROM school_international_fee_items
                   WHERE fee_record_id=? AND fee_category='tuition'""",
                (record["id"],),
            )["count"]
            self.assertEqual(tuition_count, 0)

    def test_confirm_does_not_publish_needs_review_records(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "aaie_crm.db")
            db.initialize()
            stamp = "2026-07-01T12:00:00+10:00"
            school_id = db.execute(
                """INSERT INTO schools(
                   school_name,state,sector_key,school_type,website_url,
                   international_student_available,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    "Codex Needs Review Fee College",
                    "VIC",
                    "private",
                    "Pri/Sec",
                    "https://needs-review.example.edu.au",
                    1,
                    stamp,
                    stamp,
                ),
            )
            job_id = app_module.data_update_create_job(db, "private_international_fees", user_id=1)
            report = app_module.stage_private_international_fee_mvp(
                db,
                job_id,
                school_rows=[db.one("SELECT * FROM schools WHERE id=?", (school_id,))],
                source_payloads={
                    school_id: """
                    2026 International student enrolment page.
                    Application fee AUD 150.
                    Enrolment fee AUD 1,500.
                    """
                },
                user_id=1,
            )
            app_module.data_update_write_preview(db, job_id, report)
            app_module.data_update_finish_job(db, job_id, "Needs Confirmation", report)

            changed = app_module.confirm_private_international_fee_job(db, db.one("SELECT * FROM data_update_jobs WHERE id=?", (job_id,)), user_id=1)

            self.assertEqual(changed, 0)
            self.assertEqual(db.one("SELECT COUNT(*) count FROM school_fees")["count"], 0)

    def test_mvp_extracts_overseas_student_annual_tuition(self):
        raw_text = """
        Scale of Fees for Overseas Students for 2026.
        Year Level Year 7 Year 8 Year 9 Year 10 Year 11 Year 12 Boarding Year 9 Outdoor Education Experience
        Annual Tuition Fee $44,338 $44,338 $47,539 $47,539 $48,280 $48,280 $33,400
        Additional Fee and Payroll Tax Levy $10,562 $10,562 $10,562 $11,372 $10,562 $10,562
        """

        rows = app_module.private_fee_extract_rows_from_text(raw_text)

        self.assertIn(
            {
                "fee_type": "Tuition",
                "year_level": "Year 7",
                "amount": "44,338",
                "fee_year": "2026",
                "raw_text": "Overseas Students 2026 Annual Tuition Fee Year 7 $44,338",
            },
            rows,
        )
        self.assertNotIn("33,400", [row["amount"] for row in rows])

    def test_mvp_overseas_tuition_ignores_local_fee_table_before_overseas_heading(self):
        raw_text = """
        Enrol Fees Scholarships Overseas Students Fact Sheets and Booklets.
        Scale of Fees for 2026.
        Year Level Preparatory Year 1 Year 2
        Annual Tuition Fee $23,580 $24,132 $29,541
        Scale of Fees for Overseas Students for 2026.
        Year Level Preparatory Year 1 Year 2
        Annual Tuition Fee $30,428 $35,866 $35,866
        """

        rows = app_module.private_fee_extract_rows_from_text(raw_text)
        amounts = [row["amount"] for row in rows if row["fee_type"] == "Tuition"]
        all_amounts = [row["amount"] for row in rows]

        self.assertIn("30,428", amounts)
        self.assertNotIn("23,580", all_amounts)

    def test_routes_and_labels_are_wired(self):
        source = Path("app.py").read_text(encoding="utf-8")
        self.assertIn("private_international_fees", source)
        self.assertIn("私校国际生学费更新", source)
        self.assertIn("/data-updates/private-international-fees/review", source)
        self.assertIn("school_international_fee_panel", source)
        self.assertIn("International Student Fees 国际生学费", source)
        self.assertIn("latest_private_international_fee_job_id", source)
        self.assertIn("record['data_update_job_id'] == latest_job_id", source)
        self.assertIn("WHERE r.data_update_job_id=?", source)
        self.assertIn("全库私校", source)

    def test_official_fetcher_has_robots_txt_guard(self):
        source = Path("app.py").read_text(encoding="utf-8")
        self.assertIn("urllib.robotparser", source)
        self.assertIn("private_fee_can_fetch", source)
        self.assertIn(".can_fetch(", source)


if __name__ == "__main__":
    unittest.main()
