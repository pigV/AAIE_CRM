"""Exercise two HTTP confirmations that both pass the initial duplicate check."""
import base64
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import app
from database import Database
from tests import test_school_plan_lifecycle as lifecycle_tests


class SchoolPlanConfirmationConcurrencyTests(unittest.TestCase):
    def test_racing_http_requests_archive_one_pair_and_keep_one_signature(self):
        fixture = lifecycle_tests.SchoolPlanParentConfirmationWriteTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            database = Database(root / "confirmation.db")
            with sqlite3.connect(database.path) as target:
                fixture.connection.backup(target)
                target.execute("CREATE TABLE archived_documents(confirmation_id INTEGER, kind TEXT)")
            link = {
                **fixture.context, "id": 5, "plan_sector": "public",
                "bound_wechat_openid": "openid", "bound_wechat_unionid": "unionid",
                "bound_wechat_nickname": "Parent",
            }
            ready = threading.Barrier(2)
            saved_signatures = []
            save_signature = app.CRMHandler.school_plan_save_signature_image

            def save_original_signature(handler, report_id, signature_data):
                target = save_signature(handler, report_id, signature_data)
                saved_signatures.append((target, target.read_bytes()))
                return target

            def request_body(handler):
                # Each thread has already observed no confirmation at this boundary.
                ready.wait(timeout=10)
                payload = (b"A" if threading.current_thread().name.endswith("0") else b"B") * 256
                return {
                    "declaration": "yes", "rank_school_1": "1", "rank_school_2": "2",
                    "signature_data": "data:image/png;base64," + base64.b64encode(payload).decode(),
                }, {}

            def generate_pdf(handler, confirmation_id):
                target = root / f"confirmation_{confirmation_id}.pdf"
                target.write_bytes(b"rendered signature page")
                return target

            def archive_locked(handler, confirmation_id, user=None):
                # Rendering/storage boundary; the production file lock stays real.
                with database.transaction() as connection:
                    for kind in ("FinalConfirmedSchoolPlan", "ParentSignaturePage"):
                        connection.execute("INSERT INTO archived_documents VALUES(?,?)", (confirmation_id, kind))
                return {"confirmation_id": confirmation_id}

            def submit():
                handler = object.__new__(app.CRMHandler)
                handler.client_address = ("127.0.0.1", 0)
                handler.headers = {"User-Agent": "concurrent-test"}
                return handler.school_plan_share_confirm("POST", "test-token")

            patches = {
                "school_plan_share_row_by_token": lambda self, token: link,
                "school_plan_share_status_problem": lambda self, row: ("", "", 200),
                "school_plan_share_cookie_valid": lambda self, row: True,
                "school_plan_share_confirm_permission_problem": lambda self, row: ("", "", 200),
                "school_plan_snapshot": lambda self, row: {},
                "school_plan_recommended_schools_for_confirmation": lambda self, snapshot: fixture.schools,
                "school_plan_generate_confirmation_pdf": generate_pdf,
                "school_plan_save_signature_image": save_original_signature,
                "school_plan_archive_confirmation_documents_locked": archive_locked,
                "school_plan_share_record_log": lambda *args, **kwargs: None,
                "school_plan_share_public_page": lambda self, *args: "success",
                "redirect": lambda self, url: "redirect",
            }
            with patch.object(app, "db", database), patch.object(
                app, "SCHOOL_PLAN_CONFIRMATION_DIR", root / "confirmations"
            ), patch.object(app, "parse_request_body", request_body), patch.multiple(app.CRMHandler, **patches):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    results = list(pool.map(lambda _: submit(), range(2)))

            self.assertCountEqual(results, ["success", "redirect"])
            self.assertEqual(database.one("SELECT COUNT(*) n FROM school_plan_report_confirmations")["n"], 1)
            self.assertEqual(database.one("SELECT COUNT(*) n FROM school_plan_confirmed_schools")["n"], 2)
            self.assertEqual([row["kind"] for row in database.all(
                "SELECT kind FROM archived_documents ORDER BY kind"
            )], ["FinalConfirmedSchoolPlan", "ParentSignaturePage"])
            self.assertEqual(len(list((root / "confirmations" / "1").glob("signature_*.png"))), 1)
            self.assertEqual(len(saved_signatures), 1)
            signature_path, original_payload = saved_signatures[0]
            self.assertEqual(signature_path.read_bytes(), original_payload)
            self.assertEqual(database.one(
                "SELECT COUNT(*) n FROM school_plan_report_events WHERE event_type='parent_page_archive_requested'"
            )["n"], 1)


if __name__ == "__main__":
    unittest.main()
