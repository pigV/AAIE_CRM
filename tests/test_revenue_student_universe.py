"""Read-only acceptance against the explicitly provisioned Staging database."""
import os
from contextlib import closing
import sqlite3
import unittest
from pathlib import Path
from revenue.read_models import RevenueReadModels
from revenue.views import render_revenue_workspace_page

DB = Path(os.environ.get("REVENUE_UNIVERSE_STAGING_DB", "/Volumes/Zhang/AAIE CodeX/CRM/.worktrees/agent-reach-trends-single-platform-20260912/STAGING_DATA/database/staging.db"))

class StudentUniverseTests(unittest.TestCase):
    def setUp(self):
        self.assertIn("STAGING_DATA", DB.parts)
        self.c = sqlite3.connect(DB.resolve().as_uri()+"?mode=ro", uri=True)
        self.c.execute("PRAGMA query_only=ON")
        self.m = RevenueReadModels(self.c)
    def tearDown(self):
        self.c.close()
    def eligible(self):
        return self.c.execute("SELECT s.* FROM students s WHERE "+self.m.visibility.student_predicate("s")+" ORDER BY s.student_id").fetchall()
    def test_all_pages_equal_master_without_duplicates(self):
        first=self.m.workspace("cases", {})
        self.assertIn("pagination", first)
        self.assertEqual("/revenue/cases?filter=all", first["filters"][0]["href"])
        rows=[]
        for page in range(1, first["pagination"]["pages"]+1):
            rows.extend(self.m.workspace("cases", {"page":str(page)})["rows"])
        ids=[r["student_id"] for r in rows]
        self.assertEqual({s["student_id"] for s in self.eligible()},set(ids))
        self.assertEqual(len(ids),len(set(ids)))
    def test_no_case_student_search_number_name_and_detail(self):
        s=next(s for s in self.eligible() if not self.c.execute("SELECT 1 FROM revenue_commercial_cases WHERE student_id=?",(s["student_id"],)).fetchone())
        for q in [s["student_id"], s["passport_first_name"], s["passport_last_name"]]:
            d=self.m.workspace("cases", {"q":[q]})
            self.assertIn(s["student_id"], [r["student_id"] for r in d["rows"]])
        d=self.m.workspace("cases", {"student":s["student_id"]})
        self.assertEqual(s["student_id"], d["student_detail"]["student_id"])
        self.assertEqual([],d["student_detail"]["cases"])
        self.assertIn("未建立",render_revenue_workspace_page(d,"csrf"))
    def test_multi_case_detail_preserves_every_cycle(self):
        s=self.c.execute("SELECT student_id FROM revenue_commercial_cases GROUP BY student_id HAVING COUNT(*)>1 LIMIT 1").fetchone()[0]
        d=self.m.workspace("cases", {"student":s})
        expected={r[0] for r in self.c.execute("SELECT uid FROM revenue_commercial_cases WHERE student_id=?",(s,))}
        self.assertEqual(expected,{r["uid"] for r in d["student_detail"]["cases"]})
    def test_search_filter_pagination_and_invalid_input(self):
        d=self.m.workspace("cases", {"q":"zz-no-student-'%_", "page":"bad", "filter":"bad"})
        self.assertEqual([],d["rows"])
        self.assertEqual(1,d["pagination"]["page"])
        d=self.m.workspace("cases", {"q":"a", "filter":"open"})
        self.assertTrue(all("q=a" in x["href"] for x in d["filters"]))
        html=render_revenue_workspace_page(d,"csrf")
        self.assertIn('name="q"',html)
        self.assertIn('action="/revenue/cases"',html)
        for row in d["rows"]:
            self.assertTrue(self.c.execute("SELECT 1 FROM revenue_commercial_cases WHERE student_id=? AND status='open'",(row["student_id"],)).fetchone())
    def test_unknown_or_deleted_student_never_exposes_detail(self):
        d=self.m.workspace("cases", {"student":"not-a-student"})
        self.assertEqual("not_found",d["student_detail"]["state"])

    def test_mobile_links_pages_and_read_only_queries(self):
        trace=[]
        self.c.set_trace_callback(trace.append)
        model=RevenueReadModels(self.c,case_surface="mobile")
        first=model.workspace("cases",{})
        self.assertTrue(first["pagination"]["next"].startswith("/mobile/revenue/cases?"))
        self.assertEqual(first["pagination"]["pages"],model.workspace("cases",{"page":"99999"})["pagination"]["page"])
        self.assertEqual(1,model.workspace("cases",{"page":"-5"})["pagination"]["page"])
        self.assertFalse(any(sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP")) for sql in trace))
    def test_search_html_escaping(self):
        q='"><script>alert(1)</script>'
        d=self.m.workspace("cases",{"q":q})
        html=render_revenue_workspace_page(d,"csrf")
        self.assertNotIn(q,html)
        self.assertIn("&lt;script&gt;",html)

class StudentUniverseLegacyFixtureTests(unittest.TestCase):
    def test_identifier_only_fixture_supports_search_page_and_detail(self):
        # Existing isolated Revenue fixtures predate the optional display columns.
        with closing(sqlite3.connect(':memory:')) as c:
            c.execute('CREATE TABLE students (student_id TEXT PRIMARY KEY)')
            c.execute('CREATE TABLE revenue_commercial_cases (id INTEGER, uid TEXT, student_id TEXT, application_cycle_id INTEGER, status TEXT)')
            c.execute("INSERT INTO students VALUES ('LEGACY-1')")
            model=RevenueReadModels(c)
            page=model._student_page('all', {'q':'LEGACY', 'student':'LEGACY-1'})
            self.assertEqual(['LEGACY-1'],[r['student_id'] for r in page['rows']])
            self.assertEqual('姓名待关联',page['rows'][0]['student_name'])
            self.assertEqual('姓名待关联',page['student_detail']['student_name'])
            self.assertEqual([],model._student_page('all',{'q':'missing'})['rows'])
