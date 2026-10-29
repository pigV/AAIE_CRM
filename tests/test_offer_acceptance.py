import sqlite3
import unittest
from unittest.mock import patch


class OfferAcceptanceFixture(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            CREATE TABLE school_cities(
              id INTEGER PRIMARY KEY,city_name TEXT NOT NULL,state TEXT,
              is_active INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE schools(
              id INTEGER PRIMARY KEY,school_name TEXT NOT NULL,city_id INTEGER,
              sector_key TEXT,state TEXT
            );
            CREATE TABLE offers(
              id INTEGER PRIMARY KEY,student_id TEXT NOT NULL,school_name TEXT NOT NULL,
              offer_status TEXT NOT NULL,accepted_date TEXT,accepted_by INTEGER,
              school_id INTEGER,city_id INTEGER,city_name_snapshot TEXT,sector_key TEXT,
              classification_status TEXT NOT NULL DEFAULT 'unresolved'
            );
            CREATE TABLE coe_oshc(
              id INTEGER PRIMARY KEY,student_id TEXT NOT NULL,document_kind TEXT NOT NULL
            );
            CREATE TABLE student_offer_acceptances(
              student_id TEXT PRIMARY KEY,offer_id INTEGER NOT NULL UNIQUE,
              status_before_acceptance TEXT NOT NULL,accepted_at TEXT NOT NULL,
              accepted_by_user_id INTEGER,accepted_by_name TEXT,updated_at TEXT NOT NULL
            );
            CREATE TABLE offer_acceptance_history(
              id INTEGER PRIMARY KEY,student_id TEXT NOT NULL,previous_offer_id INTEGER,
              new_offer_id INTEGER NOT NULL,previous_offer_restored_status TEXT,
              new_offer_previous_status TEXT NOT NULL,accepted_by_user_id INTEGER,
              accepted_by_name TEXT,accepted_at TEXT NOT NULL,coe_present INTEGER NOT NULL,
              coe_warning_acknowledged INTEGER NOT NULL,created_at TEXT NOT NULL
            );
            INSERT INTO school_cities VALUES
              (1,'Melbourne','VIC',1),(2,'Sydney','NSW',1);
            INSERT INTO schools VALUES
              (11,'Melbourne Public School',1,'public','VIC'),
              (12,'  CASE   School  ',1,'private','VIC'),
              (13,'Duplicate School',1,'public','VIC'),
              (14,'duplicate   school',2,'private','NSW'),
              (15,'Missing City',NULL,'public',''),
              (16,'Invalid Sector',1,'government','VIC'),
              (17,'Serpell Primary School',NULL,'public','VIC');
            INSERT INTO offers VALUES
              (101,'S001','Melbourne Public School','received',NULL,NULL,NULL,NULL,NULL,NULL,'unresolved'),
              (102,'S001','CASE School','signed',NULL,NULL,12,1,'Melbourne','private','resolved'),
              (103,'S001','Unknown School','sent_to_parent',NULL,NULL,NULL,NULL,NULL,NULL,'unresolved'),
              (201,'S002','Melbourne Public School','received',NULL,NULL,11,1,'Melbourne','public','resolved');
            """
        )
        self.actor = {"id": 9, "display_name": "Consultant A", "can_manage_offer": True}

    def tearDown(self):
        self.connection.close()


class OfferClassificationTests(OfferAcceptanceFixture):
    def test_exact_and_normalized_unique_names_resolve(self):
        from offer_acceptance import classify_offer_school

        exact = classify_offer_school(self.connection, "Melbourne Public School")
        normalized = classify_offer_school(self.connection, " case   SCHOOL ")

        self.assertEqual((exact.status, exact.school_id, exact.city_name, exact.sector_key),
                         ("resolved", 11, "Melbourne", "public"))
        self.assertEqual((normalized.status, normalized.school_id, normalized.sector_key),
                         ("resolved", 12, "private"))

    def test_unknown_duplicate_missing_city_and_invalid_sector_do_not_guess(self):
        from offer_acceptance import classify_offer_school

        unknown = classify_offer_school(self.connection, "No Such School")
        duplicate = classify_offer_school(self.connection, "DUPLICATE SCHOOL")
        missing_city = classify_offer_school(self.connection, "Missing City")
        invalid_sector = classify_offer_school(self.connection, "Invalid Sector")

        self.assertEqual(unknown.status, "unresolved")
        self.assertEqual(duplicate.status, "ambiguous")
        self.assertEqual({candidate.school_id for candidate in duplicate.candidates}, {13, 14})
        self.assertEqual(missing_city.status, "unresolved")
        self.assertEqual(invalid_sector.status, "unresolved")

    def test_manual_assignment_validates_and_persists_canonical_school_scope(self):
        from offer_acceptance import OfferClassificationError, assign_offer_school

        result = assign_offer_school(self.connection, 103, 11, self.actor)
        self.assertEqual(result.status, "resolved")
        row = self.connection.execute(
            """SELECT school_id,school_name,city_id,city_name_snapshot,sector_key,classification_status
                 FROM offers WHERE id=103"""
        ).fetchone()
        self.assertEqual(tuple(row), (11, "Melbourne Public School", 1, "Melbourne", "public", "resolved"))
        with self.assertRaises(OfferClassificationError):
            assign_offer_school(self.connection, 103, 15, self.actor)

    def test_resolved_manual_match_wires_offer_to_workflow_lifecycle_gateway(self):
        from offer_acceptance import assign_offer_school

        self.connection.execute("ALTER TABLE offers ADD COLUMN application_cycle_id INTEGER")
        self.connection.execute("CREATE TABLE application_cycles(id INTEGER PRIMARY KEY)")
        with patch("application_cycle.get_current_cycle", return_value=object()), patch(
            "application_cycle.attach_offer"
        ) as attach_offer:
            assign_offer_school(self.connection, 103, 11, self.actor)

        attach_offer.assert_called_once()
        self.assertEqual((self.connection, 103), attach_offer.call_args.args)
        self.assertTrue(attach_offer.call_args.kwargs["occurred_at"])

    def test_offer_classification_uses_the_same_unique_state_city_scope(self):
        """Catches School Plan and Offer disagreeing about one legacy school."""
        from offer_acceptance import classify_offer_school

        result = classify_offer_school(self.connection, "Serpell Primary School")

        self.assertEqual(
            (
                result.status,
                result.school_id,
                result.city_id,
                result.city_name,
                result.sector_key,
            ),
            ("resolved", 17, 1, "Melbourne", "public"),
        )
        self.assertIsNone(
            self.connection.execute(
                "SELECT city_id FROM schools WHERE id=17"
            ).fetchone()[0]
        )


class OfferSelectionTests(OfferAcceptanceFixture):
    def _resolve_first(self):
        from offer_acceptance import assign_offer_school

        assign_offer_school(self.connection, 101, 11, self.actor)

    def test_first_acceptance_and_same_offer_request_are_audited_once(self):
        from offer_acceptance import get_current_accepted_offer, set_accepted_offer

        self._resolve_first()
        first = set_accepted_offer(
            self.connection, "S001", 101, self.actor, occurred_at="2026-08-06T18:00:00+10:00"
        )
        repeated = set_accepted_offer(
            self.connection, "S001", 101, self.actor, occurred_at="2026-08-06T18:05:00+10:00"
        )

        self.assertFalse(first.unchanged)
        self.assertTrue(repeated.unchanged)
        current = get_current_accepted_offer(self.connection, "S001")
        self.assertEqual(current.offer_id, 101)
        self.assertEqual(current.status_before_acceptance, "received")
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM offer_acceptance_history").fetchone()[0], 1
        )

    def test_switch_restores_exact_previous_status_without_coe(self):
        from offer_acceptance import set_accepted_offer

        self._resolve_first()
        set_accepted_offer(self.connection, "S001", 101, self.actor)
        changed = set_accepted_offer(self.connection, "S001", 102, self.actor)

        statuses = dict(self.connection.execute("SELECT id,offer_status FROM offers WHERE id IN (101,102)"))
        self.assertEqual(statuses, {101: "received", 102: "accepted"})
        self.assertEqual(changed.previous_offer_id, 101)
        pointer = self.connection.execute(
            "SELECT offer_id,status_before_acceptance FROM student_offer_acceptances WHERE student_id='S001'"
        ).fetchone()
        self.assertEqual(tuple(pointer), (102, "signed"))

    def test_explicit_withdraw_restores_offer_and_clears_only_current_projection(self):
        from offer_acceptance import set_accepted_offer, withdraw_accepted_offer

        self._resolve_first()
        set_accepted_offer(self.connection, "S001", 101, self.actor)

        result = withdraw_accepted_offer(
            self.connection,
            "S001",
            self.actor,
            reason="family changed plans",
            occurred_at="2026-08-07T10:00:00+10:00",
        )

        self.assertEqual("received", result.restored_status)
        self.assertEqual("received", self.connection.execute(
            "SELECT offer_status FROM offers WHERE id=101"
        ).fetchone()[0])
        self.assertEqual(0, self.connection.execute(
            "SELECT count(*) FROM student_offer_acceptances WHERE student_id='S001'"
        ).fetchone()[0])
        self.assertEqual(1, self.connection.execute(
            "SELECT count(*) FROM offer_acceptance_history WHERE student_id='S001'"
        ).fetchone()[0])

    def test_unresolved_mismatch_and_unauthorized_actor_are_blocked(self):
        from offer_acceptance import OfferAcceptanceError, set_accepted_offer

        with self.assertRaisesRegex(OfferAcceptanceError, "学校"):
            set_accepted_offer(self.connection, "S001", 103, self.actor)
        with self.assertRaisesRegex(OfferAcceptanceError, "不属于"):
            set_accepted_offer(self.connection, "S001", 201, self.actor)
        with self.assertRaisesRegex(OfferAcceptanceError, "权限"):
            set_accepted_offer(
                self.connection, "S001", 102,
                {"id": 5, "display_name": "Viewer", "can_manage_offer": False},
            )

    def test_coe_requires_acknowledgement_only_when_switching(self):
        from offer_acceptance import OfferAcceptanceError, acceptance_eligibility, set_accepted_offer

        self._resolve_first()
        self.connection.execute("INSERT INTO coe_oshc VALUES(1,'S001','COE')")
        set_accepted_offer(self.connection, "S001", 101, self.actor)
        eligibility = acceptance_eligibility(self.connection, 102)
        self.assertTrue(eligibility.coe_present)
        self.assertTrue(eligibility.requires_coe_warning)
        with self.assertRaisesRegex(OfferAcceptanceError, "COE") as caught:
            set_accepted_offer(self.connection, "S001", 102, self.actor)
        self.assertEqual(caught.exception.code, "coe_warning_required")

        changed = set_accepted_offer(
            self.connection, "S001", 102, self.actor, warning_acknowledged=True
        )
        self.assertTrue(changed.coe_warning_acknowledged)
        history = self.connection.execute(
            "SELECT coe_present,coe_warning_acknowledged FROM offer_acceptance_history ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(tuple(history), (1, 1))

    def test_history_failure_rolls_back_pointer_and_both_offer_statuses(self):
        from offer_acceptance import set_accepted_offer

        self._resolve_first()
        set_accepted_offer(self.connection, "S001", 101, self.actor)
        self.connection.executescript(
            """
            CREATE TRIGGER fail_offer_history
            BEFORE INSERT ON offer_acceptance_history
            BEGIN SELECT RAISE(ABORT,'synthetic offer history failure'); END;
            """
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "synthetic offer history failure"):
            set_accepted_offer(self.connection, "S001", 102, self.actor)

        self.assertEqual(
            dict(self.connection.execute("SELECT id,offer_status FROM offers WHERE id IN (101,102)")),
            {101: "accepted", 102: "signed"},
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT offer_id FROM student_offer_acceptances WHERE student_id='S001'"
            ).fetchone()[0],
            101,
        )

    def test_legacy_unknown_restore_status_blocks_switch_without_inventing_a_status(self):
        from offer_acceptance import OfferAcceptanceError, assign_offer_school, set_accepted_offer

        assign_offer_school(self.connection, 101, 11, self.actor)
        self.connection.execute("UPDATE offers SET offer_status='accepted' WHERE id=101")
        self.connection.execute(
            """INSERT INTO student_offer_acceptances(
                 student_id,offer_id,status_before_acceptance,accepted_at,updated_at
               ) VALUES('S001',101,'__legacy_status_unknown__','2026-08-01','2026-08-01')"""
        )

        with self.assertRaisesRegex(OfferAcceptanceError, "历史状态") as caught:
            set_accepted_offer(self.connection, "S001", 102, self.actor)

        self.assertEqual(caught.exception.code, "legacy_restore_status_required")
        self.assertEqual(
            dict(self.connection.execute("SELECT id,offer_status FROM offers WHERE id IN (101,102)")),
            {101: "accepted", 102: "signed"},
        )


class OfferRouteTests(OfferAcceptanceFixture):
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

    def _handler(self, form):
        import app as app_module

        handler = object.__new__(app_module.CRMHandler)
        handler.user = {"id": 9, "display_name": "Consultant A", "role_name": "Consultant"}
        handler.form = form
        handler.headers = {"Accept": "application/json", "User-Agent": "unit-test"}
        handler.client_address = ("127.0.0.1", 12345)
        handler.json_response = lambda payload, status=200: (status, payload)
        return handler

    def test_classification_route_checks_ownership_and_returns_refreshed_scope(self):
        import app as app_module

        old_db = app_module.db
        old_permission = app_module.can_workflow_module_save
        try:
            app_module.db = self.DbAdapter(self.connection)
            app_module.can_workflow_module_save = lambda user, module: module == "Offer"
            status, payload = self._handler({"csrf": "token", "school_id": "11"}).offer_classify(
                "POST", "S001", "103"
            )
            missing_status, _ = self._handler({"csrf": "token", "school_id": "11"}).offer_classify(
                "POST", "S999", "103"
            )
        finally:
            app_module.db = old_db
            app_module.can_workflow_module_save = old_permission

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["classification_status"], "resolved")
        self.assertEqual(payload["city_name"], "Melbourne")
        self.assertEqual(missing_status, 404)

    def test_accept_route_returns_coe_warning_then_switches_after_acknowledgement(self):
        import app as app_module
        from offer_acceptance import assign_offer_school, set_accepted_offer

        assign_offer_school(self.connection, 101, 11, self.actor)
        set_accepted_offer(self.connection, "S001", 101, self.actor)
        self.connection.execute("INSERT INTO coe_oshc VALUES(1,'S001','COE')")
        old_db = app_module.db
        old_permission = app_module.can_workflow_module_save
        try:
            app_module.db = self.DbAdapter(self.connection)
            app_module.can_workflow_module_save = lambda user, module: module == "Offer"
            warning_status, warning = self._handler(
                {"csrf": "token", "coe_warning_acknowledged": "0"}
            ).offer_accept("POST", "S001", "102")
            status, payload = self._handler(
                {"csrf": "token", "coe_warning_acknowledged": "1"}
            ).offer_accept("POST", "S001", "102")
        finally:
            app_module.db = old_db
            app_module.can_workflow_module_save = old_permission

        self.assertEqual(warning_status, 409)
        self.assertTrue(warning["requires_coe_warning"])
        self.assertIn("Melbourne Public School", warning["previous_school_name"])
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["accepted_offer_id"], 102)


if __name__ == "__main__":
    unittest.main()
