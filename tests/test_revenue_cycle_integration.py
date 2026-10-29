from __future__ import annotations

import unittest
from unittest.mock import patch

import application_cycle
from revenue.cycle_integration import ensure_revenue_case_for_cycle
from tests.revenue_test_support import RevenueDatabaseFixture
from tests.test_workflow_application_cycles_migration import (
    NOW,
    apply_workflow_migration,
    build_legacy_fixture,
)


class RevenueCycleIntegrationTests(RevenueDatabaseFixture, unittest.TestCase):
    def test_active_module_opens_exactly_one_case_for_cycle(self):
        with self.connect() as connection:
            connection.execute(
                "UPDATE revenue_module_state SET status='active',cutover_at='2026-08-29T00:00:00Z' WHERE id=1"
            )

            first = ensure_revenue_case_for_cycle(
                connection, 100, 1, occurred_at="2026-08-30T00:00:00Z"
            )
            replay = ensure_revenue_case_for_cycle(
                connection, 100, 1, occurred_at="2026-08-30T00:00:00Z"
            )

            self.assertEqual(first["uid"], replay["uid"])
            self.assertEqual(1, connection.execute(
                "SELECT COUNT(*) FROM revenue_commercial_cases WHERE application_cycle_id=100"
            ).fetchone()[0])
            self.assertEqual(1, connection.execute(
                "SELECT COUNT(*) FROM revenue_idempotency_receipts WHERE idempotency_key='revenue-cycle-open:100'"
            ).fetchone()[0])

    def test_disabled_or_missing_authority_is_a_safe_noop(self):
        with self.connect() as connection:
            self.assertIsNone(ensure_revenue_case_for_cycle(
                connection, 100, 1, occurred_at="2026-08-30T00:00:00Z"
            ))
            connection.execute("UPDATE revenue_module_state SET status='active' WHERE id=1")
            self.assertIsNone(ensure_revenue_case_for_cycle(
                connection, 999, 1, occurred_at="2026-08-30T00:00:00Z"
            ))
            self.assertIsNone(ensure_revenue_case_for_cycle(
                connection, 100, None, occurred_at="2026-08-30T00:00:00Z"
            ))
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM revenue_commercial_cases"
            ).fetchone()[0])

    def test_pre_cutover_event_never_materializes_a_historical_cycle(self):
        with self.connect() as connection:
            connection.execute(
                "UPDATE revenue_module_state SET status='active',cutover_at='2026-08-29T00:00:00Z' WHERE id=1"
            )

            result = ensure_revenue_case_for_cycle(
                connection, 100, 1, occurred_at="2026-08-28T23:59:59Z"
            )

            self.assertIsNone(result)
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM revenue_commercial_cases"
            ).fetchone()[0])

            connection.execute(
                """INSERT INTO application_cycles(
                       id,cycle_uid,student_id,cycle_type,status,education_stream,created_at
                   ) VALUES(101,'cycle-before-cutover','S-REVENUE','initial','closed',
                            'school_education','2026-08-28T00:00:00Z')"""
            )
            self.assertIsNone(ensure_revenue_case_for_cycle(
                connection, 101, 1, occurred_at="2026-08-30T00:00:00Z"
            ))
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM revenue_commercial_cases WHERE application_cycle_id=101"
            ).fetchone()[0])


class CanonicalCycleHookTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = self.enterContext(__import__("tempfile").TemporaryDirectory())
        self.connection = build_legacy_fixture(
            __import__("pathlib").Path(self.temp_dir) / "cycles.sqlite3"
        )
        self.addCleanup(self.connection.close)
        apply_workflow_migration(self.connection)

    def test_create_student_cycle_invokes_revenue_hook_after_authoritative_insert(self):
        with patch("application_cycle.ensure_revenue_case_for_cycle") as ensure:
            cycle = application_cycle.create_student_cycle(
                self.connection,
                "S1",
                "initial",
                NOW,
                1,
                cycle_uid="cycle-revenue-hook",
            )
        ensure.assert_called_once_with(
            self.connection, cycle.id, 1,
            occurred_at=NOW, integration_event="created",
        )

    def test_attach_student_invokes_revenue_hook_after_identity_is_authoritative(self):
        cycle = application_cycle.create_parent_form_cycle(
            self.connection, "parent-token-1", NOW, 1, cycle_uid="parent-revenue-hook"
        )
        with patch("application_cycle.ensure_revenue_case_for_cycle") as ensure:
            attached = application_cycle.attach_student(
                self.connection, cycle.id, "S1", NOW, 1
            )
        ensure.assert_called_once_with(
            self.connection, attached.id, 1,
            occurred_at=NOW, integration_event="attached",
        )

    def test_idempotent_attach_replay_does_not_reopen_revenue_integration(self):
        cycle = application_cycle.create_parent_form_cycle(
            self.connection, "parent-token-1", NOW, 1, cycle_uid="parent-revenue-replay"
        )
        attached = application_cycle.attach_student(
            self.connection, cycle.id, "S1", NOW, 1
        )
        with patch("application_cycle.ensure_revenue_case_for_cycle") as ensure:
            replay = application_cycle.attach_student(
                self.connection, cycle.id, "S1", NOW, 1
            )

        self.assertEqual(attached, replay)
        ensure.assert_not_called()


if __name__ == "__main__":
    unittest.main()
