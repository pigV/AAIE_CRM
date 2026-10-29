import re
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import app as app_module
from database import Database


class ContentLibrarySortingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.class_temp = tempfile.TemporaryDirectory(prefix="content-library-sort-template-")
        cls.template_path = Path(cls.class_temp.name) / "template.db"
        db = Database(cls.template_path)
        db.initialize()
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with db.transaction() as conn:
            for index in range(205):
                created_at = (base + timedelta(minutes=index)).isoformat()
                updated_at = (base + timedelta(minutes=1000 - index)).isoformat()
                title = f"{'Needle ' if index % 20 == 0 else ''}Script {index:03d}"
                conn.execute(
                    """INSERT INTO content_scripts(
                           title,body,category,status,created_at,updated_at
                       ) VALUES(?,?,?,?,?,?)""",
                    (
                        title,
                        f"Body {index:03d}",
                        "Even" if index % 2 == 0 else "Odd",
                        "Draft",
                        created_at,
                        updated_at,
                    ),
                )
                script_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                source_modified_at = (base + timedelta(minutes=index)).isoformat()
                conn.execute(
                    """INSERT INTO content_materials(
                           script_id,material_type,title,source_path,source_url,file_hash,
                           source_modified_at,created_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        script_id,
                        "google_drive_document",
                        title,
                        f"gdrive://drive-{script_id}",
                        f"https://drive.google.com/open?id=drive-{script_id}",
                        f"hash-{script_id}",
                        source_modified_at,
                        created_at,
                        updated_at,
                    ),
                )
            conn.execute(
                "UPDATE content_scripts SET title='Zulu',content_year=2020,status='Zeta',imported_at=? WHERE id=2",
                (datetime(2030, 1, 1, tzinfo=timezone.utc).isoformat(),),
            )
            conn.execute(
                "UPDATE content_scripts SET title='Alpha',content_year=2030,status='Alpha' WHERE id=3"
            )
            conn.execute("UPDATE content_scripts SET title='Mike' WHERE id=4")

    @classmethod
    def tearDownClass(cls):
        cls.class_temp.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="content-library-sort-case-")
        self.db_path = Path(self.temp.name) / "case.db"
        shutil.copy2(self.template_path, self.db_path)
        self.db = Database(self.db_path)

    def tearDown(self):
        self.temp.cleanup()

    def render(self, **query):
        handler = object.__new__(app_module.CRMHandler)
        handler.query = {key: [str(value)] for key, value in query.items()}
        handler.csrf = "csrf-content-sort"
        with mock.patch.object(app_module, "db", self.db), mock.patch.object(
            handler, "content_daily_sync_report_panel", return_value=""
        ):
            return handler.content_dashboard_panel()

    @staticmethod
    def script_ids(html):
        return [int(value) for value in re.findall(r'href="/content/scripts/(\d+)"', html)]

    def test_default_sort_is_google_drive_source_modified_at_descending(self):
        html = self.render()

        self.assertEqual(self.script_ids(html)[:3], [205, 204, 203])
        self.assertIn('name="sort_by"', html)
        self.assertRegex(html, r'<option value="source_modified_at" selected>最后修改时间</option>')
        self.assertRegex(html, r'<option value="desc" selected>最新 → 最旧</option>')

    def test_old_drive_document_modified_today_moves_to_top(self):
        self.db.execute(
            "UPDATE content_materials SET source_modified_at=? WHERE script_id=1",
            ("2099-01-01T00:00:00.000+00:00",),
        )

        html = self.render()

        self.assertEqual(self.script_ids(html)[:3], [1, 205, 204])

    def test_editing_old_content_in_crm_does_not_reorder_drive_document(self):
        self.db.execute("UPDATE content_scripts SET updated_at=? WHERE id=1", ("2099-01-01T00:00:00Z",))

        html = self.render()

        self.assertEqual(self.script_ids(html)[:3], [205, 204, 203])

    def test_equal_source_modified_times_use_id_descending_as_stable_secondary_sort(self):
        tied_time = self.db.one(
            "SELECT source_modified_at FROM content_materials WHERE script_id=205"
        )["source_modified_at"]
        self.db.execute(
            "UPDATE content_materials SET source_modified_at=? WHERE script_id=204", (tied_time,)
        )

        html = self.render()

        self.assertEqual(self.script_ids(html)[:2], [205, 204])

    def test_drive_created_and_modified_times_are_displayed_in_melbourne(self):
        html = self.render()

        self.assertIn("建立时间：", html)
        self.assertIn("最后修改：", html)
        self.assertIn("01/01/2026 14:24", html)

    def test_four_required_timestamp_orders(self):
        cases = (
            ({"sort_by": "source_modified_at", "sort_order": "desc"}, [205, 204, 203]),
            ({"sort_by": "source_modified_at", "sort_order": "asc"}, [1, 2, 3]),
            ({"sort_by": "created_at", "sort_order": "desc"}, [205, 204, 203]),
            ({"sort_by": "created_at", "sort_order": "asc"}, [1, 2, 3]),
        )
        for query, expected in cases:
            with self.subTest(query=query):
                self.assertEqual(self.script_ids(self.render(**query))[:3], expected)

    def test_pagination_sorts_the_entire_dataset_and_preserves_query(self):
        page_two = self.render(sort_by="created_at", sort_order="asc", page=2)
        page_three = self.render(sort_by="created_at", sort_order="asc", page=3)

        self.assertEqual(self.script_ids(page_two)[:3], [101, 102, 103])
        self.assertEqual(len(self.script_ids(page_two)), 100)
        self.assertEqual(self.script_ids(page_three), [201, 202, 203, 204, 205])
        self.assertIn("第 2 / 3 页", page_two)
        self.assertIn("sort_by=created_at", page_two)
        self.assertIn("sort_order=asc", page_two)
        self.assertIn("page=3", page_two)

    def test_search_and_sort_are_composed_server_side(self):
        html = self.render(q="Needle", sort_by="created_at", sort_order="desc")

        self.assertEqual(self.script_ids(html), [201, 181, 161, 141, 121, 101, 81, 61, 41, 21, 1])
        self.assertIn('value="Needle"', html)

    def test_filter_and_sort_are_composed_server_side(self):
        html = self.render(category="Even", sort_by="source_modified_at", sort_order="asc", page=1)

        self.assertEqual(self.script_ids(html)[:3], [1, 3, 5])
        self.assertIn('<option value="Even" selected>Even</option>', html)

    def test_untrusted_sort_parameters_fall_back_to_allowlisted_default(self):
        html = self.render(
            sort_by="source_modified_at; DROP TABLE content_scripts",
            sort_order="desc; DROP TABLE content_topics",
        )

        self.assertEqual(self.script_ids(html)[:3], [205, 204, 203])
        self.assertEqual(self.db.one("SELECT COUNT(*) count FROM content_scripts")["count"], 205)
        self.assertNotIn("DROP TABLE", html)

    def test_drive_without_source_modified_time_never_falls_back_to_crm_updated_at(self):
        self.db.execute(
            "UPDATE content_materials SET source_modified_at=NULL WHERE script_id=1"
        )
        self.db.execute(
            "UPDATE content_scripts SET updated_at=? WHERE id=1",
            ("2099-01-01T00:00:00.000+00:00",),
        )

        html = self.render()

        self.assertNotEqual(self.script_ids(html)[0], 1)
        item_html = self.render(q="Body 000")
        self.assertEqual(self.script_ids(item_html), [1])
        self.assertIn("最后修改：</span><strong>未填写</strong>", item_html)

    def test_non_drive_content_falls_back_to_crm_updated_at(self):
        self.db.execute(
            "UPDATE content_materials SET source_path=?,source_url=NULL,source_modified_at=NULL WHERE script_id=1",
            ("manual://content-1",),
        )
        self.db.execute(
            "UPDATE content_scripts SET source_type='manual',updated_at=? WHERE id=1",
            ("2099-01-01T00:00:00.000+00:00",),
        )

        html = self.render()

        self.assertEqual(self.script_ids(html)[0], 1)

    def test_legacy_sort_links_preserve_their_original_semantics(self):
        cases = (
            ({"sort": "latest"}, 2),
            ({"sort": "title"}, 3),
            ({"sort": "year"}, 3),
            ({"sort": "status"}, 3),
        )
        for query, expected_first_id in cases:
            with self.subTest(query=query):
                html = self.render(**query)
                self.assertEqual(self.script_ids(html)[0], expected_first_id)
                self.assertIn(f"sort={query['sort']}", html)

    def test_sort_controls_reset_page_to_one_and_pager_is_accessible(self):
        html = self.render(page=2)

        self.assertIn('name="page" value="1"', html)
        self.assertIn('data-content-sort-control', html)
        self.assertNotIn('onchange=', html)
        self.assertIn('aria-label="文案库分页"', html)
        self.assertIn("上一页", html)
        self.assertIn("下一页", html)
        self.assertIn('/assets/content-library-sort.js?v=20260822', html)
        script = Path("assets/content-library-sort.js").read_text(encoding="utf-8")
        self.assertIn('qa("[data-content-sort-control]")', script)
        self.assertIn('form.elements.page.value = "1"', script)
        self.assertIn("form.requestSubmit()", script)


if __name__ == "__main__":
    unittest.main()
