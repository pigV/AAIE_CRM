import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

import app as app_module
from scripts import bettereducation_top_pages_staging_fetch as top_pages_fetch
from scripts import bettereducation_vce_trend_staging_fetch as vce_trend_fetch
from database import Database


class BetterEducationAutoFetchTests(unittest.TestCase):
    def test_auto_fetch_schema_has_review_first_tables(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "aaie_crm.db")
            db.initialize()

            tables = {
                row["name"]
                for row in db.all(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertTrue({
                "bettereducation_fetch_jobs",
                "bettereducation_fetch_queue",
                "bettereducation_fetch_log",
                "bettereducation_school_mapping",
                "academic_results_import_staging",
            }.issubset(tables))

            staging_columns = {
                row["name"]
                for row in db.all("PRAGMA table_info(academic_results_import_staging)")
            }
            self.assertTrue({
                "import_batch_id",
                "state",
                "source_type",
                "source_name",
                "crm_school_id",
                "source_school_name",
                "data_year",
                "bettereducation_rank",
                "median_vce_score",
                "percentage_study_scores_40_plus",
                "match_status",
                "confidence_score",
                "review_status",
            }.issubset(staging_columns))

    def test_auto_fetch_results_enter_review_without_writing_production_rankings(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "aaie_crm.db")
            db.initialize()
            stamp = "2026-07-01T12:00:00+10:00"
            school_id = db.execute(
                """INSERT INTO schools(
                   school_name,state,sector_key,school_type,suburb,postcode,year_levels,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                ("Codex Better Education Auto Fetch School", "VIC", "public", "Public", "South Yarra", "3141", "7-12", stamp, stamp),
            )
            job_id = db.execute(
                """INSERT INTO data_update_jobs(
                   job_type,state,status,started_at,trigger_type,source_period,preview_json,source_type,
                   source_name,source_url,data_year,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "bettereducation_auto_fetch", "VIC", "Running", stamp, "manual", "2016-2025",
                    "{}", "third_party_ranking", "Better Education",
                    "https://bettereducation.com.au/", "2025", stamp, stamp,
                ),
            )

            report = app_module.bettereducation_stage_fetch_results(
                db,
                job_id,
                [
                    {
                        "crm_school_id": school_id,
                        "crm_school_name": "Codex Better Education Auto Fetch School",
                        "source_school_name": "Codex Better Education Auto Fetch School",
                        "state": "VIC",
                        "data_year": "2025",
                        "bettereducation_rank": "1",
                        "bettereducation_percentile": "99",
                        "score": "100",
                        "median_vce_score": "38",
                        "percentage_study_scores_40_plus": "32.5",
                        "result_metric_name": "Better Education VCE Compare/Trend Rank",
                        "result_metric_value": "1",
                        "students": "240",
                        "exams_or_entries": "1100",
                        "source_url": "https://bettereducation.com.au/school/secondary/vic/vic_top_secondary_schools.aspx",
                        "confidence_score": 0.98,
                        "match_status": "Matched",
                        "raw_row_json": {"Year": "2025", "Rank": "1"},
                    }
                ],
                user_id=1,
                artifact_dir=Path(folder) / "bettereducation_auto_fetch",
            )

            self.assertEqual(report["records_found"], 1)
            self.assertEqual(report["records_saved_to_staging"], 1)
            self.assertEqual(report["records_written_to_crm"], 0)
            self.assertEqual(db.one("SELECT COUNT(*) count FROM school_academic_rankings")["count"], 0)

            import_row = db.one("SELECT * FROM school_ranking_imports WHERE id=?", (report["import_id"],))
            self.assertEqual(import_row["source_name"], "Better Education")
            self.assertEqual(import_row["source_type"], "third_party")
            self.assertEqual(import_row["review_status"], "Needs Review")

            review_row = db.one("SELECT * FROM school_ranking_import_rows WHERE import_id=?", (report["import_id"],))
            self.assertEqual(review_row["suggested_school_id"], school_id)
            self.assertEqual(review_row["review_status"], "Needs Review")
            self.assertEqual(review_row["rank"], "1")
            self.assertEqual(review_row["score"], "100")
            self.assertEqual(review_row["median_score"], "38")
            self.assertEqual(review_row["top_scores_percentage"], "32.5")
            self.assertEqual(review_row["result_metric_name"], "Better Education VCE Compare/Trend Rank")
            self.assertEqual(review_row["result_metric_value"], "1")

            staging_row = db.one("SELECT * FROM academic_results_import_staging WHERE import_batch_id=?", (report["import_id"],))
            self.assertEqual(staging_row["source_type"], "third_party")
            self.assertEqual(staging_row["source_name"], "Better Education")
            self.assertEqual(staging_row["crm_school_id"], school_id)
            self.assertEqual(staging_row["review_status"], "Needs Review")

    def test_top_pages_parser_extracts_yearly_rankings(self):
        html = """
        <table id="ctl00_ContentPlaceHolder1_GridView1">
          <tr><th>Row #</th><th>School</th><th>State Overall Score</th><th>Better Education Percentile</th><th>Total Enrolments</th><th>Locality</th><th>Sector</th><th>SES</th></tr>
          <tr><td>22</td><td>Abbotsleigh,Wahroonga,NSW,2076</td><td>99</td><td>3%</td><td>1640</td><td>Wahroonga,NSW,2076</td><td>Non-government</td><td>97</td></tr>
        </table>
        """

        rows = top_pages_fetch.parse_grid_rows(
            html,
            state="NSW",
            year=2025,
            source_url="https://bettereducation.com.au/school/Secondary/nsw/nsw_top_secondary_schools.aspx?yr=2025",
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source_school_name"], "Abbotsleigh")
        self.assertEqual(rows[0]["source_suburb"], "Wahroonga")
        self.assertEqual(rows[0]["bettereducation_rank"], "22")
        self.assertEqual(rows[0]["score"], "99")
        self.assertEqual(rows[0]["bettereducation_percentile"], "3")
        self.assertEqual(rows[0]["ranking_system"], "Better Education Secondary")

    def test_top_pages_matching_uses_school_name_and_suburb(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "aaie_crm.db")
            db.initialize()
            stamp = "2026-07-01T12:00:00+10:00"
            school_id = db.execute(
                """INSERT INTO schools(
                   school_name,state,sector_key,school_type,suburb,postcode,year_levels,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                ("Abbotsleigh", "NSW", "private", "Private", "Wahroonga", "2076", "7-12", stamp, stamp),
            )
            records = [{"source_school_name": "Abbotsleigh", "source_suburb": "Wahroonga", "source_postcode": "2076"}]

            counts = top_pages_fetch.attach_crm_matches(records, db, "NSW")

            self.assertEqual(counts["Matched"], 1)
            self.assertEqual(records[0]["crm_school_id"], school_id)
            self.assertEqual(records[0]["crm_school_name"], "Abbotsleigh")
            self.assertEqual(records[0]["match_status"], "Matched")
            self.assertGreaterEqual(records[0]["confidence_score"], 0.95)

    def test_vce_trend_parser_extracts_bettereducation_rank_history(self):
        html = """
        <table>
          <tr><th>School</th><th>Year</th><th>Better Education Rank</th><th>scores of 40+ (%)</th><th>Median VCE Score</th><th>Median Score Percentile</th><th>Locality</th><th>IB</th><th>Unit 3-4 cohort</th></tr>
          <tr><td>Frankston High School</td><td>2024</td><td>116</td><td>9.6</td><td>31</td><td></td><td>FRANKSTON</td><td></td><td>490</td></tr>
          <tr><td>Frankston High School</td><td>2025</td><td>191</td><td>5.7</td><td>30</td><td></td><td>FRANKSTON</td><td></td><td>446</td></tr>
        </table>
        """

        rows = vce_trend_fetch.parse_vce_trend_rows(
            html,
            crm_school_id=49,
            crm_school_name="Frankston High School",
            state="VIC",
            year_start=2016,
            year_end=2025,
            source_url="https://bettereducation.com.au/CompareSchools/year_12/vic/compare_vce_school_ranking.aspx",
        )

        self.assertEqual([row["data_year"] for row in rows], ["2024", "2025"])
        self.assertEqual(rows[1]["bettereducation_rank"], "191")
        self.assertEqual(rows[1]["median_vce_score"], "30")
        self.assertEqual(rows[1]["percentage_study_scores_40_plus"], "5.7")
        self.assertEqual(rows[1]["students"], "446")
        self.assertEqual(rows[1]["result_metric_name"], "Better Education VCE Compare/Trend Rank")
        self.assertEqual(rows[1]["result_metric_value"], "191")

    def test_year12_trend_parser_extracts_nsw_hsc_rank_history(self):
        html = """
        <table>
          <tr><th>School Name</th><th>Year</th><th>Rank</th><th>%</th><th>DA</th><th>BE Percentile</th><th>Exams Sat</th><th>Year 12 Students</th><th>Category</th><th>Region</th><th>Locality</th></tr>
          <tr><td>St Paul's Grammar School</td><td>2024</td><td>430</td><td>2.3%</td><td>6</td><td></td><td>266</td><td>41</td><td>Non-government</td><td>Metro</td><td>Cranebrook,NSW,2749</td></tr>
          <tr><td>St Paul's Grammar School</td><td>2025</td><td>453</td><td>2.0%</td><td>3</td><td></td><td>149</td><td></td><td>Non-government</td><td>Metro</td><td>Cranebrook,NSW,2749</td></tr>
        </table>
        """

        rows = vce_trend_fetch.parse_year12_trend_rows(
            html,
            crm_school_id=917,
            crm_school_name="St Paul's Grammar School",
            state="NSW",
            year_start=2016,
            year_end=2025,
            source_url="https://bettereducation.com.au/CompareSchools/year_12/nsw/compare_secondary_schools_by_hsc.aspx",
        )

        self.assertEqual([row["data_year"] for row in rows], ["2024", "2025"])
        self.assertEqual(rows[1]["bettereducation_rank"], "453")
        self.assertEqual(rows[1]["score"], "3")
        self.assertEqual(rows[1]["percentage_study_scores_40_plus"], "2.0")
        self.assertEqual(rows[1]["exams_or_entries"], "149")
        self.assertEqual(rows[1]["ranking_system"], "HSC")
        self.assertEqual(rows[1]["result_metric_name"], "Better Education HSC Compare/Trend Rank")
        self.assertEqual(rows[1]["result_metric_value"], "453")

    def test_missing_scope_fetch_job_queues_only_schools_without_bettereducation_data(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "aaie_crm.db")
            db.initialize()
            stamp = "2026-07-01T12:00:00+10:00"
            missing_id = db.execute(
                """INSERT INTO schools(
                   school_name,state,sector_key,school_type,suburb,postcode,year_levels,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                ("Codex Missing BetterEducation School", "VIC", "private", "Private", "Brighton", "3186", "7-12", stamp, stamp),
            )
            covered_id = db.execute(
                """INSERT INTO schools(
                   school_name,state,sector_key,school_type,suburb,postcode,year_levels,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                ("Codex Covered BetterEducation School", "VIC", "private", "Private", "Kew", "3101", "7-12", stamp, stamp),
            )
            db.execute(
                """INSERT INTO school_academic_rankings(
                   school_id,school_name_raw,ranking_system,state,data_year,rank,source_name,source_type,
                   imported_at,review_status
                ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    covered_id, "Codex Covered BetterEducation School", "VCE", "VIC", "2025", "12",
                    "Better Education", "third_party_ranking", stamp, "Confirmed",
                ),
            )

            result = app_module.bettereducation_create_fetch_job(
                db,
                state="VIC",
                year_start=2016,
                year_end=2025,
                school_scope="missing",
                selected_school_ids=[missing_id, covered_id],
                user_id=1,
                test_mode=False,
            )

            self.assertEqual(result["queued_count"], 1)
            queued = db.all("SELECT * FROM bettereducation_fetch_queue WHERE fetch_job_id=?", (result["fetch_job_id"],))
            self.assertEqual([row["school_id"] for row in queued], [missing_id])
            self.assertEqual(db.one("SELECT total_schools FROM bettereducation_fetch_jobs WHERE id=?", (result["fetch_job_id"],))["total_schools"], 1)
            data_job = db.one("SELECT * FROM data_update_jobs WHERE id=?", (result["data_update_job_id"],))
            self.assertEqual(data_job["source_name"], "Better Education")
            self.assertEqual(data_job["status"], "Queued")

    def test_data_update_centre_exposes_bettereducation_auto_fetch_controls(self):
        source = Path("app.py").read_text(encoding="utf-8")
        self.assertIn("/data-updates/academic-results/bettereducation", source)
        self.assertIn("Better Education Auto Fetch", source)
        self.assertIn("Test Fetch 3 Schools", source)
        self.assertIn("Fetch Missing Schools Only", source)
        self.assertIn("Require Review Before CRM Sync", source)

    def test_playwright_command_uses_project_worker_and_local_browser_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            payload_path = Path(folder) / "payload.json"
            output_path = Path(folder) / "output.json"
            artifact_dir = Path(folder) / "artifacts"

            command, env = app_module.bettereducation_playwright_command(payload_path, output_path, artifact_dir)

            self.assertTrue(command[0].endswith("node"))
            self.assertEqual(Path(command[1]).name, "bettereducation_playwright_fetch.js")
            self.assertEqual(command[2], str(payload_path))
            self.assertEqual(command[3], str(output_path))
            self.assertEqual(command[4], str(artifact_dir))
            self.assertIn("NODE_PATH", env)
            self.assertIn("PLAYWRIGHT_BROWSERS_PATH", env)
            self.assertIn("cache/ms-playwright", env["PLAYWRIGHT_BROWSERS_PATH"])

    def test_playwright_command_defaults_to_vic_vce_compare_trend_page(self):
        with tempfile.TemporaryDirectory() as folder:
            command, env = app_module.bettereducation_playwright_command(
                Path(folder) / "payload.json",
                Path(folder) / "output.json",
                Path(folder) / "artifacts",
            )

            self.assertIn("BETTEREDUCATION_COMPARE_URL", env)
            self.assertIn("CompareSchools/year_12/vic/compare_vce_school_ranking.aspx", env["BETTEREDUCATION_COMPARE_URL"])
            self.assertNotEqual(env["BETTEREDUCATION_COMPARE_URL"], "https://bettereducation.com.au/")

    def test_bettereducation_compare_url_uses_school_state(self):
        self.assertIn("CompareSchools/year_12/vic/compare_vce_school_ranking.aspx", app_module.bettereducation_compare_url_for_state("VIC"))
        self.assertIn("CompareSchools/year_12/nsw/compare_secondary_schools_by_hsc.aspx", app_module.bettereducation_compare_url_for_state("NSW"))
        self.assertIn("CompareSchools/year_12/qld/compare_qld_yr12_school_ranking.aspx", app_module.bettereducation_compare_url_for_state("QLD"))
        self.assertIn("CompareSchools/year_12/wa/compare_wace_school_ranking.aspx", app_module.bettereducation_compare_url_for_state("WA"))
        self.assertIn("compare_act_secondary_schools.aspx", app_module.bettereducation_compare_url_for_state("ACT"))

    def test_year12_trend_fetcher_supports_qld_compare_trend_page(self):
        self.assertEqual(
            vce_trend_fetch.YEAR12_COMPARE_URLS["QLD"],
            "https://bettereducation.com.au/CompareSchools/year_12/qld/compare_qld_yr12_school_ranking.aspx",
        )
        self.assertEqual(vce_trend_fetch.ranking_system_for_state("QLD"), "QCE")

    def test_year12_trend_fetcher_supports_wa_compare_trend_page(self):
        self.assertEqual(
            vce_trend_fetch.YEAR12_COMPARE_URLS["WA"],
            "https://bettereducation.com.au/CompareSchools/year_12/wa/compare_wace_school_ranking.aspx",
        )
        self.assertEqual(vce_trend_fetch.ranking_system_for_state("WA"), "WACE")

    def test_queue_payload_uses_bettereducation_vic_vce_compare_trend_page(self):
        source = Path("app.py").read_text(encoding="utf-8")
        self.assertIn("BETTER_EDUCATION_SECONDARY_COMPARE_URLS", source)
        self.assertIn("CompareSchools/year_12/vic/compare_vce_school_ranking.aspx", source)
        self.assertIn("bettereducation_compare_url_for_state", source)
        self.assertNotIn('"source_url": "https://bettereducation.com.au/"', source)

    def test_queue_payload_uses_state_specific_compare_page(self):
        source = Path("app.py").read_text(encoding="utf-8")
        worker_source = Path("scripts/bettereducation_playwright_fetch.js").read_text(encoding="utf-8")
        self.assertIn('"source_url": bettereducation_compare_url_for_state(queue["state"])', source)
        self.assertIn("payload.source_url || process.env.BETTEREDUCATION_COMPARE_URL", worker_source)

    def test_playwright_worker_supports_bettereducation_home_search_controls(self):
        source = Path("scripts/bettereducation_playwright_fetch.js").read_text(encoding="utf-8")
        self.assertIn("TextBoxSchool", source)
        self.assertIn("input[value='Go' i]", source)
        self.assertIn("fillFirstMatchingInput", source)

    def test_playwright_worker_maps_vce_compare_trend_rank_as_metric(self):
        source = Path("scripts/bettereducation_playwright_fetch.js").read_text(encoding="utf-8")
        self.assertIn('const betterEducationRank = rowValue(cells, ["bettereducationrank", "rank"])', source)
        self.assertIn("function resultMetricNameForState", source)
        self.assertIn("Better Education ${rankingSystemForState(state)} Compare/Trend Rank", source)
        self.assertIn("result_metric_value: betterEducationRank", source)
        self.assertIn("unit34cohort", source)

    def test_failed_worker_result_json_is_logged_with_specific_error_type(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "aaie_crm.db")
            db.initialize()
            stamp = "2026-07-01T12:00:00+10:00"
            school_id = db.execute(
                """INSERT INTO schools(
                   school_name,state,sector_key,school_type,suburb,postcode,year_levels,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                ("Codex Browser Launch Test School", "VIC", "public", "Public", "South Yarra", "3141", "7-12", stamp, stamp),
            )
            data_update_job_id = db.execute(
                """INSERT INTO data_update_jobs(
                   job_type,state,status,started_at,trigger_type,source_period,preview_json,source_type,
                   source_name,source_url,data_year,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "bettereducation_auto_fetch", "VIC", "Running", stamp, "manual", "2016-2025",
                    "{}", "third_party_ranking", "Better Education",
                    "https://bettereducation.com.au/", "2025", stamp, stamp,
                ),
            )
            fetch_job_id = db.execute(
                """INSERT INTO bettereducation_fetch_jobs(
                   data_update_job_id,state,year_start,year_end,school_scope,total_schools,status,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (data_update_job_id, "VIC", 2016, 2025, "test_3", 1, "Running", stamp, stamp),
            )
            queue_id = db.execute(
                """INSERT INTO bettereducation_fetch_queue(
                   fetch_job_id,data_update_job_id,school_id,crm_school_name,suburb,state,sector,fetch_status,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (fetch_job_id, data_update_job_id, school_id, "Codex Browser Launch Test School", "South Yarra", "VIC", "public", "pending", stamp, stamp),
            )

            def fake_command(_payload_path, output_path, _artifact_dir):
                Path(output_path).write_text(
                    """{"status":"failed","error_type":"browser_launch_failed","error_message":"MachPortRendezvous permission denied"}""",
                    encoding="utf-8",
                )
                return ["/bin/false"], {}

            with patch.object(app_module, "bettereducation_playwright_command", side_effect=fake_command), \
                    patch.object(app_module.subprocess, "run", return_value=SimpleNamespace(returncode=1, stdout="", stderr="")):
                result = app_module.bettereducation_run_queue_item(db, queue_id)

            self.assertEqual(result["error_type"], "browser_launch_failed")
            queue = db.one("SELECT fetch_status,error_message FROM bettereducation_fetch_queue WHERE id=?", (queue_id,))
            self.assertEqual(queue["fetch_status"], "browser_launch_failed")
            self.assertIn("MachPortRendezvous", queue["error_message"])
            log = db.one("SELECT fetch_status,error_type,error_message FROM bettereducation_fetch_log WHERE queue_id=?", (queue_id,))
            self.assertEqual(log["error_type"], "browser_launch_failed")
            self.assertIn("MachPortRendezvous", log["error_message"])

    def test_playwright_worker_classifies_browser_launch_failures(self):
        source = Path("scripts/bettereducation_playwright_fetch.js").read_text(encoding="utf-8")
        self.assertIn("browser_launch_failed", source)
        self.assertIn("MachPortRendezvous", source)

    def test_pending_runner_retries_browser_launch_failed_queue_items(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "aaie_crm.db")
            db.initialize()
            stamp = "2026-07-01T12:00:00+10:00"
            data_update_job_id = db.execute(
                """INSERT INTO data_update_jobs(
                   job_type,state,status,started_at,trigger_type,source_period,preview_json,source_type,
                   source_name,source_url,data_year,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "bettereducation_auto_fetch", "VIC", "Error", stamp, "manual", "2016-2025",
                    "{}", "third_party_ranking", "Better Education",
                    "https://bettereducation.com.au/", "2025", stamp, stamp,
                ),
            )
            fetch_job_id = db.execute(
                """INSERT INTO bettereducation_fetch_jobs(
                   data_update_job_id,state,year_start,year_end,school_scope,total_schools,status,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (data_update_job_id, "VIC", 2016, 2025, "test_3", 1, "Error", stamp, stamp),
            )
            db.execute(
                """INSERT INTO bettereducation_fetch_queue(
                   fetch_job_id,data_update_job_id,school_id,crm_school_name,suburb,state,sector,fetch_status,error_message,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    fetch_job_id, data_update_job_id, None, "Codex Browser Retry School", "South Yarra",
                    "VIC", "public", "browser_launch_failed", "MachPortRendezvous permission denied", stamp, stamp,
                ),
            )

            calls = []
            with patch.object(app_module, "bettereducation_run_fetch_job", side_effect=lambda job_id, **_kwargs: calls.append(job_id)):
                result = app_module.bettereducation_run_pending_fetch_jobs(db, pause_between_schools=False)

            self.assertEqual(calls, [fetch_job_id])
            self.assertEqual(result["jobs_run"], 1)
            self.assertEqual(result["fetch_job_ids"], [fetch_job_id])

    def test_pending_runner_retries_page_structure_changed_after_worker_update(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "aaie_crm.db")
            db.initialize()
            stamp = "2026-07-01T12:00:00+10:00"
            data_update_job_id = db.execute(
                """INSERT INTO data_update_jobs(
                   job_type,state,status,started_at,trigger_type,source_period,preview_json,source_type,
                   source_name,source_url,data_year,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "bettereducation_auto_fetch", "VIC", "Error", stamp, "manual", "2016-2025",
                    "{}", "third_party_ranking", "Better Education",
                    "https://bettereducation.com.au/CompareSchools/secondary/vic/compare_vic_secondary_schools.aspx", "2025", stamp, stamp,
                ),
            )
            fetch_job_id = db.execute(
                """INSERT INTO bettereducation_fetch_jobs(
                   data_update_job_id,state,year_start,year_end,school_scope,total_schools,status,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (data_update_job_id, "VIC", 2016, 2025, "test_3", 1, "Error", stamp, stamp),
            )
            db.execute(
                """INSERT INTO bettereducation_fetch_queue(
                   fetch_job_id,data_update_job_id,school_id,crm_school_name,suburb,state,sector,fetch_status,error_message,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    fetch_job_id, data_update_job_id, None, "Codex Page Structure Retry School", "South Yarra",
                    "VIC", "public", "page_structure_changed", "School search box not found.", stamp, stamp,
                ),
            )

            calls = []
            with patch.object(app_module, "bettereducation_run_fetch_job", side_effect=lambda job_id, **_kwargs: calls.append(job_id)):
                result = app_module.bettereducation_run_pending_fetch_jobs(db, pause_between_schools=False)

            self.assertEqual(calls, [fetch_job_id])
            self.assertEqual(result["jobs_run"], 1)

    def test_fetch_job_runner_processes_browser_launch_failed_queue_items(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "aaie_crm.db")
            db.initialize()
            old_db = app_module.db
            app_module.db = db
            try:
                stamp = "2026-07-01T12:00:00+10:00"
                data_update_job_id = db.execute(
                    """INSERT INTO data_update_jobs(
                       job_type,state,status,started_at,trigger_type,source_period,preview_json,source_type,
                       source_name,source_url,data_year,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "bettereducation_auto_fetch", "VIC", "Error", stamp, "manual", "2016-2025",
                        "{}", "third_party_ranking", "Better Education",
                        "https://bettereducation.com.au/", "2025", stamp, stamp,
                    ),
                )
                fetch_job_id = db.execute(
                    """INSERT INTO bettereducation_fetch_jobs(
                       data_update_job_id,state,year_start,year_end,school_scope,total_schools,status,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (data_update_job_id, "VIC", 2016, 2025, "test_3", 1, "Error", stamp, stamp),
                )
                queue_id = db.execute(
                    """INSERT INTO bettereducation_fetch_queue(
                       fetch_job_id,data_update_job_id,school_id,crm_school_name,suburb,state,sector,fetch_status,error_message,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        fetch_job_id, data_update_job_id, None, "Codex Browser Retry School", "South Yarra",
                        "VIC", "public", "browser_launch_failed", "MachPortRendezvous permission denied", stamp, stamp,
                    ),
                )

                calls = []
                def fake_queue_item(_db_handle, seen_queue_id, **_kwargs):
                    calls.append(seen_queue_id)
                    return {"status": "success", "records": 1}

                with patch.object(app_module, "bettereducation_run_queue_item", side_effect=fake_queue_item):
                    app_module.bettereducation_run_fetch_job(fetch_job_id, pause_between_schools=False)

                self.assertEqual(calls, [queue_id])
            finally:
                app_module.db = old_db

    def test_fetch_job_retry_recounts_progress_from_final_queue_statuses(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "aaie_crm.db")
            db.initialize()
            old_db = app_module.db
            app_module.db = db
            try:
                stamp = "2026-07-01T12:00:00+10:00"
                data_update_job_id = db.execute(
                    """INSERT INTO data_update_jobs(
                       job_type,state,status,started_at,trigger_type,source_period,preview_json,source_type,
                       source_name,source_url,data_year,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "bettereducation_auto_fetch", "VIC", "Error", stamp, "manual", "2016-2025",
                        "{}", "third_party_ranking", "Better Education",
                        "https://bettereducation.com.au/", "2025", stamp, stamp,
                    ),
                )
                fetch_job_id = db.execute(
                    """INSERT INTO bettereducation_fetch_jobs(
                       data_update_job_id,state,year_start,year_end,school_scope,total_schools,status,
                       processed_schools,success_count,failed_count,review_required_count,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (data_update_job_id, "VIC", 2016, 2025, "test_3", 1, "Error", 1, 0, 1, 0, stamp, stamp),
                )
                queue_id = db.execute(
                    """INSERT INTO bettereducation_fetch_queue(
                       fetch_job_id,data_update_job_id,school_id,crm_school_name,suburb,state,sector,fetch_status,error_message,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        fetch_job_id, data_update_job_id, None, "Codex Browser Retry School", "South Yarra",
                        "VIC", "public", "browser_launch_failed", "MachPortRendezvous permission denied", stamp, stamp,
                    ),
                )

                def fake_queue_item(db_handle, seen_queue_id, **_kwargs):
                    db_handle.execute("UPDATE bettereducation_fetch_queue SET fetch_status='success',error_message='' WHERE id=?", (seen_queue_id,))
                    return {"status": "success", "records": 1}

                with patch.object(app_module, "bettereducation_run_queue_item", side_effect=fake_queue_item):
                    app_module.bettereducation_run_fetch_job(fetch_job_id, pause_between_schools=False)

                job = db.one("SELECT processed_schools,success_count,failed_count,review_required_count FROM bettereducation_fetch_jobs WHERE id=?", (fetch_job_id,))
                self.assertEqual(job["processed_schools"], 1)
                self.assertEqual(job["success_count"], 1)
                self.assertEqual(job["failed_count"], 0)
                self.assertEqual(job["review_required_count"], 1)
            finally:
                app_module.db = old_db

    def test_bettereducation_terminal_runner_command_invokes_pending_runner(self):
        command = Path("运行BetterEducation自动抓取.command").read_text(encoding="utf-8")
        self.assertIn("--bettereducation-run-pending", command)
        self.assertIn("staging", command.lower())
        self.assertIn("does not auto-sync", command)

    def test_school_detail_source_notice_explains_bettereducation_is_third_party(self):
        source = Path("app.py").read_text(encoding="utf-8")
        self.assertIn("Better Education 排名属于第三方参考数据", source)
        self.assertIn("not an official VCAA ATAR ranking", source)
