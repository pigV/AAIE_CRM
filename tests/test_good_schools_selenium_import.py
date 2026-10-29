import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.good_schools_selenium_import import GoodSchoolsSeleniumImporter


class FakeElement:
    def __init__(self, text="", href="", driver=None, action=None):
        self.text = text
        self.href = href
        self.driver = driver
        self.action = action
        self.typed = ""

    def get_attribute(self, name):
        return self.href if name == "href" else ""

    def clear(self):
        self.typed = ""

    def send_keys(self, value):
        self.typed += str(value)

    def click(self):
        if self.action:
            self.action()
            return
        if self.driver and self.href:
            self.driver.get(self.href)


class FakeDriver:
    def __init__(self, has_result=True, results=None):
        self.has_result = has_result
        self.current_url = ""
        self.page_text = ""
        self.search_box = FakeElement(driver=self)
        self.results = results or [
            FakeElement("Brighton Grammar School Brighton 3186", "https://www.goodschools.com.au/compare-schools/in-brighton-3186/brighton-grammar-school", self)
        ]
        self.result = self.results[0]
        self.activity_items = {
            "Activities": ["Chess Club", "Debating"],
            "Music": ["Choir", "Concert Band"],
            "Sports": ["Basketball", "Tennis"],
        }

    def get(self, url):
        self.current_url = url
        if "brighton-grammar-school" in url:
            self.page_text = (
                "Brighton Grammar School is an independent boys school. "
                "The school offers academic extension, sport, rowing, music and orchestra programs. "
                "Activities & Support Staff Activities (2) Music (2) Sports (2). "
                "NAPLAN reading Year 9 615 numeracy Year 9 640 writing Year 9 590."
            )
        else:
            self.page_text = "Good Schools search"

    def find_elements(self, by=None, value=None):
        if value and "input" in value:
            return [self.search_box]
        if value == "a" or (value and "a" in value):
            return self.results if self.has_result else []
        if value and any(part in value for part in ("button", "[role='button']", "[role=button]")):
            elements = []
            for category, items in self.activity_items.items():
                def make_action(label=category, values=items):
                    def action():
                        self.page_text = f"{label} " + " ".join(values)
                    return action
                elements.append(FakeElement(f"{category} ({len(items)})", driver=self, action=make_action()))
            return elements
        return []

    def find_element(self, by=None, value=None):
        if value == "body":
            return FakeElement(self.page_text)
        matches = self.find_elements(by, value)
        if matches:
            return matches[0]
        raise RuntimeError("not found")

    def quit(self):
        return None


def create_school_db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE schools(id INTEGER PRIMARY KEY, school_name TEXT, state TEXT, suburb TEXT, postcode TEXT, updated_at TEXT);
        CREATE TABLE school_feature_categories(id INTEGER PRIMARY KEY, category_name_cn TEXT, category_name_en TEXT, sort_order INTEGER, is_active INTEGER);
        CREATE TABLE school_feature_tags(id INTEGER PRIMARY KEY, category_id INTEGER, tag_name_cn TEXT, tag_name_en TEXT, keywords TEXT, sort_order INTEGER, is_active INTEGER);
        CREATE TABLE school_feature_tag_assignments(id INTEGER PRIMARY KEY, school_id INTEGER, category_id INTEGER, tag_id INTEGER, assignment_type TEXT, status TEXT, confidence_rate REAL, source_url TEXT, source_title TEXT, source_type TEXT, source_excerpt TEXT, collected_at TEXT, collected_by_system INTEGER, notes TEXT, created_at TEXT, updated_at TEXT);
        CREATE TABLE school_third_party_profiles(id INTEGER PRIMARY KEY, school_id INTEGER, source_name TEXT, source_type TEXT DEFAULT 'third_party', source_url TEXT, profile_url TEXT, fetched_at TEXT, review_status TEXT, school_name TEXT, third_party_description TEXT, third_party_summary TEXT, strengths_json TEXT, raw_extract_json TEXT, fetch_status TEXT, error_message TEXT, notes TEXT, created_at TEXT, updated_at TEXT, updated_by INTEGER);
        CREATE TABLE school_academic_indicators(id INTEGER PRIMARY KEY, school_id INTEGER, source_name TEXT, source_type TEXT DEFAULT 'third_party', source_url TEXT, fetched_at TEXT, review_status TEXT, indicator_name TEXT, year_level TEXT, data_year TEXT, indicator_value TEXT, comparison_notes TEXT, raw_text TEXT, notes TEXT, created_at TEXT, updated_at TEXT, updated_by INTEGER);
        """
    )
    conn.execute("INSERT INTO schools(id,school_name,state,suburb,postcode,updated_at) VALUES(1,'Brighton Grammar School','VIC','Brighton','3186','')")
    conn.execute("INSERT INTO school_feature_categories(id,category_name_cn,category_name_en,sort_order,is_active) VALUES(1,'学术','Academic',1,1),(2,'体育','Sports',2,1),(3,'音乐','Music',3,1),(4,'领导力与课外活动','Leadership & Activities',4,1)")
    conn.executemany(
        "INSERT INTO school_feature_tags(id,category_id,tag_name_cn,tag_name_en,keywords,sort_order,is_active) VALUES(?,?,?,?,?,?,1)",
        [
            (1, 1, "学术", "Academic", '["academic","extension"]', 1),
            (2, 2, "体育", "Sports", '["sport","rowing"]', 1),
            (3, 3, "音乐", "Music", '["music","orchestra"]', 1),
            (4, 2, "篮球", "Basketball", '["basketball"]', 2),
            (5, 2, "网球", "Tennis", '["tennis"]', 3),
            (6, 3, "合唱团", "Choir", '["choir"]', 2),
            (7, 3, "管乐团", "Band", '["band","concert band"]', 3),
            (8, 4, "Debating", "Debating", '["debating"]', 1),
            (9, 4, "社团丰富", "Clubs", '["activities","club","chess"]', 2),
        ],
    )
    conn.commit()
    conn.close()


class GoodSchoolsSeleniumImporterTests(unittest.TestCase):
    def test_importer_searches_extracts_and_writes_structured_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "crm.db"
            create_school_db(db_path)
            importer = GoodSchoolsSeleniumImporter(db_path=db_path, driver_factory=lambda: FakeDriver())

            result = importer.run("Brighton Grammar School")

            self.assertEqual(result["status"], "success")
            self.assertEqual(result["school_id"], 1)
            self.assertTrue(any(row["tag_name_cn"] in {"篮球", "网球", "合唱团", "管乐团", "Debating", "社团丰富"} for row in result["features"]))
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            self.assertEqual(conn.execute("SELECT COUNT(*) c FROM school_third_party_profiles").fetchone()["c"], 1)
            self.assertGreaterEqual(conn.execute("SELECT COUNT(*) c FROM school_feature_tag_assignments").fetchone()["c"], 3)
            self.assertGreaterEqual(conn.execute("SELECT COUNT(*) c FROM school_academic_indicators").fetchone()["c"], 2)
            conn.close()

    def test_goodschools_activities_are_mapped_to_school_feature_tags(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "crm.db"
            create_school_db(db_path)
            importer = GoodSchoolsSeleniumImporter(db_path=db_path, driver_factory=lambda: FakeDriver())

            result = importer.run("Brighton Grammar School")

            self.assertEqual(result["status"], "success")
            self.assertIn("activities", result)
            self.assertGreaterEqual(len(result["activities"]), 3)
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT t.tag_name_cn,a.source_type,a.notes
                   FROM school_feature_tag_assignments a
                   JOIN school_feature_tags t ON t.id=a.tag_id
                   WHERE a.source_type='Good Schools Guide'
                   ORDER BY t.id"""
            ).fetchall()
            tag_names = {row["tag_name_cn"] for row in rows}
            self.assertTrue({"篮球", "网球", "合唱团", "管乐团", "Debating", "社团丰富"}.issubset(tag_names))
            self.assertTrue(any("GoodSchools Activities" in (row["notes"] or "") for row in rows))
            profile = conn.execute("SELECT raw_extract_json FROM school_third_party_profiles LIMIT 1").fetchone()
            self.assertIn("activity_categories", profile["raw_extract_json"])
            conn.close()

    def test_importer_reports_school_not_found_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "crm.db"
            create_school_db(db_path)
            importer = GoodSchoolsSeleniumImporter(db_path=db_path, driver_factory=lambda: FakeDriver(has_result=False))

            result = importer.run("Brighton Grammar School")

            self.assertEqual(result["status"], "not_found")
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            self.assertEqual(conn.execute("SELECT COUNT(*) c FROM school_third_party_profiles").fetchone()["c"], 0)
            conn.close()

    def test_importer_uses_suburb_and_postcode_to_choose_matching_goodschools_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "crm.db"
            create_school_db(db_path)
            wrong = FakeElement(
                "Brighton Grammar School Springfield 4300",
                "https://www.goodschools.com.au/compare-schools/in-springfield-4300/brighton-grammar-school",
            )
            right = FakeElement(
                "Brighton Grammar School Brighton 3186",
                "https://www.goodschools.com.au/compare-schools/in-brighton-3186/brighton-grammar-school",
            )
            driver = FakeDriver(results=[wrong, right])
            for element in driver.results:
                element.driver = driver
            importer = GoodSchoolsSeleniumImporter(db_path=db_path, driver_factory=lambda: driver)

            result = importer.run("Brighton Grammar School")

            self.assertEqual(result["status"], "success")
            self.assertEqual(result["source_url"], "https://www.goodschools.com.au/compare-schools/in-brighton-3186/brighton-grammar-school")

    def test_importer_requires_review_when_goodschools_result_does_not_match_school_location(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "crm.db"
            create_school_db(db_path)
            wrong = FakeElement(
                "Brighton Grammar School Springfield 4300",
                "https://www.goodschools.com.au/compare-schools/in-springfield-4300/brighton-grammar-school",
            )
            driver = FakeDriver(results=[wrong])
            driver.result.driver = driver
            importer = GoodSchoolsSeleniumImporter(db_path=db_path, driver_factory=lambda: driver)

            result = importer.run("Brighton Grammar School")

            self.assertEqual(result["status"], "needs_review")
            self.assertIn("区名或邮编", result["message"])
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            self.assertEqual(conn.execute("SELECT COUNT(*) c FROM school_third_party_profiles").fetchone()["c"], 0)
            conn.close()

    def test_importer_rejects_goodschools_search_page_as_school_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "crm.db"
            create_school_db(db_path)
            search_page = FakeElement(
                "Brighton Grammar School Brighton 3186",
                "https://www.goodschools.com.au/compare-schools/search?keywords=Brighton+Grammar+School+Brighton+3186",
            )
            driver = FakeDriver(results=[search_page])
            driver.result.driver = driver
            importer = GoodSchoolsSeleniumImporter(db_path=db_path, driver_factory=lambda: driver)

            result = importer.run("Brighton Grammar School")

            self.assertIn(result["status"], {"needs_review", "not_found"})
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            self.assertEqual(conn.execute("SELECT COUNT(*) c FROM school_feature_tag_assignments").fetchone()["c"], 0)
            conn.close()


if __name__ == "__main__":
    unittest.main()
