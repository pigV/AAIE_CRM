import inspect
import unittest
from contextlib import contextmanager
import sqlite3


class OfferPanelUiTests(unittest.TestCase):
    class DbStub:
        @contextmanager
        def read_snapshot(self):
            connection=sqlite3.connect(':memory:')
            connection.executescript('CREATE TABLE application_cycles(id,student_id); CREATE TABLE audit_logs(id,action_type,module_name,student_id,record_id,new_value);')
            try:yield connection
            finally:connection.close()

        def one(self, sql, params=()):
            if "student_offer_acceptances" in sql:
                return {"offer_id": 1, "school_name": "Accepted School"}
            return None

        def all(self, sql, params=()):
            if "FROM schools" in sql:
                return [
                    {"id": 11, "school_name": "Accepted School", "city_name": "Melbourne", "sector_key": "public"},
                    {"id": 12, "school_name": "Alternative School", "city_name": "Sydney", "sector_key": "private"},
                ]
            return []

    def test_offer_cards_show_accepted_muted_and_classification_contracts(self):
        import app as app_module

        old_db = app_module.db
        try:
            app_module.db = self.DbStub()
            handler = object.__new__(app_module.CRMHandler)
            handler.business_changes_enabled=lambda:False
            handler.user={'id':1,'username':'viewer','role_name':'View Only','permissions_json':'[]'}
            handler.csrf = "csrf-token"
            offers = [
                {
                    "id": 1, "school_name": "Accepted School", "offer_status": "accepted",
                    "deposit_currency": "AUD", "document_type": "Offer", "offer_file_path": "",
                    "signed_offer_file_path": "", "original_filename": "accepted.pdf",
                    "uploaded_at": "2026-08-06", "created_at": "2026-08-06",
                    "deposit_amount": None, "offer_date": "2026-08-06", "offer_expiry_date": "2026-08-20",
                    "school_id": 11, "city_id": 1, "city_name_snapshot": "Melbourne",
                    "sector_key": "public", "classification_status": "resolved",
                },
                {
                    "id": 2, "school_name": "Alternative School", "offer_status": "received",
                    "deposit_currency": "AUD", "document_type": "Offer", "offer_file_path": "",
                    "signed_offer_file_path": "", "original_filename": "alternative.pdf",
                    "uploaded_at": "2026-08-06", "created_at": "2026-08-06",
                    "deposit_amount": None, "offer_date": "2026-08-06", "offer_expiry_date": "2026-08-20",
                    "school_id": 12, "city_id": 2, "city_name_snapshot": "Sydney",
                    "sector_key": "private", "classification_status": "resolved",
                },
                {
                    "id": 3, "school_name": "Unknown", "offer_status": "received",
                    "deposit_currency": "AUD", "document_type": "Offer", "offer_file_path": "",
                    "signed_offer_file_path": "", "original_filename": "unknown.pdf",
                    "uploaded_at": "2026-08-06", "created_at": "2026-08-06",
                    "deposit_amount": None, "offer_date": "", "offer_expiry_date": "",
                    "school_id": None, "city_id": None, "city_name_snapshot": None,
                    "sector_key": None, "classification_status": "unresolved",
                },
            ]

            html = handler.offer_panel("S001", offers)
        finally:
            app_module.db = old_db

        self.assertIn('data-offer-current-accepted="true"', html)
        self.assertIn("当前已接受", html)
        self.assertIn("offer-record-card--muted", html)
        self.assertIn('/students/S001/offer/2/accept', html)
        self.assertIn("设为已接受", html)
        self.assertIn("待完善学校资料", html)
        self.assertIn('/students/S001/offer/3/classify', html)
        self.assertIn("已有 COE", html)
        self.assertNotIn('<option value="accepted"', html)


class StudentTabOrderTests(unittest.TestCase):
    def test_declared_student_tab_order_matches_confirmed_workflow(self):
        import app as app_module

        # The shared snapshot wrapper delegates layout to its retained owner.
        source = inspect.getsource(app_module.CRMLegacyHandler.student_detail)
        expected = [
            '("applications", "申请材料"',
            '("contracts", "合同"',
            '("school_plans", "学校方案"',
            '("offer", "录取通知"',
            '("payments", "付款"',
            '("coe", "入学确认与学生保险"',
            '("visa", "签证"',
            '("homestay", "住宿与接机"',
            '("history", "历史记录"',
            '("reminders", "留言与提醒"',
        ]
        positions = [source.index(marker) for marker in expected]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn("student_detail_school_plan_section(student_id)", source)


if __name__ == "__main__":
    unittest.main()
