from contextlib import closing
from http import HTTPStatus
import json
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse
import urllib.request

from student_case_feature_flags import StudentCaseFeatureFlags
import student_case_service
from tests import test_student_case_phase2_transfer as phase2_tests
from tests import test_transfer_completion_lifecycle as lifecycle_tests
import transfer_case_service
import transfer_case_views
from transfer_completion_readiness import (
    READINESS_CODES,
    TransferCompletionReadinessDTO,
    TransferReadinessItemDTO,
)


@unittest.skip(
    "Legacy duplicate Transfer workbench renderer is no longer a Production route; "
    "Lifecycle/Case summary and service/route contracts cover the supported UI."
)
class TransferCompletionViewTests(
    lifecycle_tests.TransferCompletionLifecycleTests
):
    """T8E rendering contract against an isolated 001-007 database."""

    SENSITIVE_HISTORY_FIELDS = (
        "idempotency_key",
        "payload_sha256",
        "readiness_snapshot",
        "school_state_before_json",
        "school_state_after_json",
        '"case_before"',
        '"case_after"',
        '"payload"',
    )

    @staticmethod
    def user(*permissions):
        return {"id": 1, "permissions": list(permissions)}

    def flags(self, *, write=True, completion=True):
        return StudentCaseFeatureFlags(
            read_enabled=True,
            transfer_write_enabled=write,
            completion_enabled=completion,
        )

    def readiness(self, *, ready=True, warning=False):
        items = []
        for index, code in enumerate(READINESS_CODES):
            status = "satisfied"
            severity = "blocking"
            reason = None
            warning_message = None
            if not ready and index == 0:
                status = "missing"
                reason = "case_not_active"
            elif warning and code == "visa_data_quality":
                status = "warning"
                severity = "warning"
                warning_message = "visa_expiry_missing"
            items.append(
                TransferReadinessItemDTO(
                    code=code,
                    label=code.replace("_", " ").title(),
                    status=status,
                    severity=severity,
                    source_table="fixture",
                    source_id=index + 1,
                    blocking_reason=reason,
                    warning_message=warning_message,
                    target_anchor="#transfer-arrangements",
                    evaluated_at="2026-07-30T12:00:00+10:00",
                    metadata={"internal_secret": "must-not-render"},
                )
            )
        return TransferCompletionReadinessDTO(
            case_id=self.case_id,
            student_id="student-1",
            ready=ready,
            evaluated_at="2026-07-30T12:00:00+10:00",
            items=tuple(items),
            blocking_count=0 if ready else 1,
            warning_count=1 if warning else 0,
            satisfied_count=len(items) - (0 if ready else 1) - (
                1 if warning else 0
            ),
            not_applicable_count=0,
            relationship_status="ready",
            final_application_id=1,
            current_enrolment_id=7001,
            upcoming_enrolment_id=7002,
        )

    def render(
        self,
        *permissions,
        readiness=None,
        write=True,
        completion=True,
    ):
        result = readiness or self.readiness()
        with patch.object(
            transfer_case_service.TransferCaseService,
            "evaluate_completion_readiness",
            return_value=result,
        ):
            return transfer_case_views.render_transfer_case_detail(
                self.database_path,
                "student-1",
                self.case_id,
                self.user("student_cases.view", *permissions),
                self.flags(write=write, completion=completion),
                csrf="csrf-token",
                staff_users=(),
            )

    def test_readiness_renders_stable_items_counts_anchors_and_no_metadata(self):
        html = self.render(
            "student_cases.complete",
            readiness=self.readiness(ready=False, warning=True),
        )
        section = html.split(
            'id="transfer-completion-readiness"', 1
        )[1].split('id="transfer-completion-summary"', 1)[0]
        self.assertIn("Blocked", section)
        self.assertIn("Blocking", section)
        self.assertIn("Warnings", section)
        self.assertIn("Visa Data Quality", section)
        self.assertIn("visa_expiry_missing", section)
        self.assertIn('href="#transfer-arrangements"', section)
        self.assertNotIn("Complete Case", section)
        self.assertNotIn("internal_secret", section)

    def test_complete_requires_exact_permission_all_flags_and_ready_state(self):
        allowed = self.render("student_cases.complete")
        self.assertIn('data-dialog-open="transfer-complete-case"', allowed)
        self.assertIn(
            f'action="/students/student-1/transfer-cases/'
            f'{self.case_id}/complete"',
            allowed,
        )
        self.assertIn('name="effective_date"', allowed)
        self.assertIn('name="completion_note"', allowed)
        self.assertIn('name="confirmed"', allowed)
        self.assertIn('name="idempotency_key"', allowed)
        self.assertNotIn('name="readiness_snapshot"', allowed)

        for permissions, write, completion, readiness in (
            (("*",), True, True, self.readiness()),
            (("student_cases.edit",), True, True, self.readiness()),
            (("student_cases.complete",), False, True, self.readiness()),
            (("student_cases.complete",), True, False, self.readiness()),
            (
                ("student_cases.complete",),
                True,
                True,
                self.readiness(ready=False),
            ),
        ):
            with self.subTest(
                permissions=permissions,
                write=write,
                completion=completion,
            ):
                denied = self.render(
                    *permissions,
                    write=write,
                    completion=completion,
                    readiness=readiness,
                )
                self.assertNotIn(
                    'data-dialog-open="transfer-complete-case"', denied
                )

    def test_status_controls_are_explicit_and_assignment_roles_do_not_grant(self):
        editable = self.render("student_cases.edit")
        self.assertIn('data-lifecycle-action="pause"', editable)
        self.assertIn('data-lifecycle-action="cancel"', editable)
        self.assertIn('data-lifecycle-action="fail"', editable)
        self.assertNotIn('data-lifecycle-action="archive"', editable)
        for permissions in (
            ("*",),
            ("student_cases.view",),
            ("student_cases.complete",),
        ):
            denied = self.render(*permissions)
            self.assertNotIn("data-lifecycle-action", denied)

        student_case_service.add_collaborator(
            self.database_path,
            case_id=self.case_id,
            user_id=1,
            actor_user_id=1,
        )
        student_case_service.add_watcher(
            self.database_path,
            case_id=self.case_id,
            user_id=1,
            actor_user_id=1,
        )
        assigned = self.render()
        self.assertNotIn("data-lifecycle-action", assigned)
        self.assertNotIn('data-dialog-open="transfer-complete-case"', assigned)

    def test_completed_summary_hides_t3_t7_writes_and_shows_exact_actions(self):
        self._complete()
        completed_readiness = self.readiness(ready=False)
        html = self.render(
            "student_cases.edit",
            "student_cases.undo_complete",
            "student_cases.archive",
            readiness=completed_readiness,
        )
        self.assertIn('id="transfer-completion-summary"', html)
        self.assertIn("Transfer verified", html)
        self.assertIn("2026-08-10", html)
        self.assertIn("Confirmation #", html)
        self.assertIn('data-dialog-open="transfer-undo-completion"', html)
        self.assertIn('data-lifecycle-action="archive"', html)
        self.assertNotIn('data-lifecycle-action="pause"', html)
        self.assertNotIn('data-lifecycle-action="cancel"', html)
        self.assertNotIn('data-lifecycle-action="fail"', html)
        self.assertNotIn('data-arrangement-write', html)
        self.assertNotIn("/materials/initialize", html)
        self.assertNotIn("选择或更换Offer", html)
        self.assertNotIn("创建付款要求", html)
        self.assertNotIn(">编辑基本信息<", html)
        self.assertNotIn(">推进到", html)
        self.assertNotIn("通用Reopen", html)

    def test_completion_history_summarizes_event_without_raw_json(self):
        self._complete()
        html = self.render(
            "student_cases.undo_complete",
            readiness=self.readiness(ready=False),
        )
        completion_row = html.split(
            "<strong>transfer_completion_completed</strong>", 1
        )[1].split("</article>", 1)[0]
        self.assertIn("User #1", completion_row)
        self.assertIn("Transfer verified", completion_row)
        self.assertIn("2026-08-10", completion_row)
        self.assertIn("Confirmation #", completion_row)
        self.assertIn("in_progress", completion_row)
        self.assertIn("completed", completion_row)
        for field in self.SENSITIVE_HISTORY_FIELDS:
            self.assertNotIn(field, completion_row)
        self.assertNotIn('{"', completion_row)

        self.service.undo_transfer_completion(
            student_id="student-1",
            case_id=self.case_id,
            actor_user_id=1,
            idempotency_key="undo-for-history-summary",
            reason="Completion date corrected",
        )
        undone_html = self.render(
            "student_cases.complete",
            readiness=self.readiness(ready=False),
        )
        undo_row = undone_html.split(
            "<strong>transfer_completion_undone</strong>", 1
        )[1].split("</article>", 1)[0]
        self.assertIn("User #1", undo_row)
        self.assertIn("Completion date corrected", undo_row)
        self.assertIn("Confirmation #", undo_row)
        self.assertIn("completed", undo_row)
        self.assertIn("in_progress", undo_row)
        for field in self.SENSITIVE_HISTORY_FIELDS:
            self.assertNotIn(field, undo_row)
        self.assertNotIn('{"', undo_row)

    def test_archived_completed_only_exposes_unarchive(self):
        self._complete()
        self.service.archive_transfer_case(
            self.case_id,
            actor_user_id=1,
            idempotency_key="archive-for-view",
        )
        html = self.render(
            "student_cases.edit",
            "student_cases.undo_complete",
            "student_cases.archive",
            readiness=self.readiness(ready=False),
        )
        self.assertIn('data-lifecycle-action="unarchive"', html)
        self.assertNotIn('data-dialog-open="transfer-undo-completion"', html)
        self.assertNotIn('data-lifecycle-action="archive"', html)
        self.assertNotIn('data-arrangement-write', html)

    def test_status_lifecycle_history_is_human_readable_and_redacted(self):
        self.service.pause_transfer_case(
            self.case_id,
            actor_user_id=1,
            reason="Waiting for student",
            idempotency_key="history-pause",
        )
        self.service.resume_transfer_case(
            self.case_id,
            actor_user_id=1,
            idempotency_key="history-resume",
        )
        self.service.cancel_transfer_case(
            self.case_id,
            actor_user_id=1,
            reason="Student withdrew",
            idempotency_key="history-cancel",
        )
        self.service.archive_transfer_case(
            self.case_id,
            actor_user_id=1,
            reason="Filed after cancellation",
            idempotency_key="history-archive",
        )
        self.service.unarchive_transfer_case(
            self.case_id,
            actor_user_id=1,
            reason="Return to terminal list",
            idempotency_key="history-unarchive",
        )
        html = self.render("student_cases.archive")
        for event_type, label in (
            ("transfer_case_paused", "Paused"),
            ("transfer_case_resumed", "Resumed"),
            ("transfer_case_cancelled", "Cancelled"),
            ("transfer_case_archived", "Archived"),
            ("transfer_case_unarchived", "Unarchived"),
        ):
            with self.subTest(event_type=event_type):
                row = html.split(
                    f"<strong>{event_type}</strong>", 1
                )[1].split("</article>", 1)[0]
                self.assertIn(label, row)
                self.assertIn("User #1", row)
                self.assertIn("2026-", row)
                for field in self.SENSITIVE_HISTORY_FIELDS:
                    self.assertNotIn(field, row)
                self.assertNotIn('{"', row)
        self.assertIn("Waiting for student", html)
        self.assertIn("Student withdrew", html)
        self.assertIn("Filed after cancellation", html)
        self.assertIn("Return to terminal list", html)
        self.assertIn("in_progress", html)
        self.assertIn("paused", html)
        self.assertIn("cancelled", html)
        self.assertIn("archived", html)

    def test_failed_and_unknown_history_never_fall_back_to_raw_json(self):
        self.service.fail_transfer_case(
            self.case_id,
            actor_user_id=1,
            reason="Evidence could not be obtained",
            idempotency_key="history-fail",
        )
        student_case_service.append_manual_event(
            self.database_path,
            case_id=self.case_id,
            event_type="transfer_case_future_event",
            actor_user_id=1,
            old_value={"case_before": {"status": "failed"}},
            new_value={"case_after": {"status": "failed"}},
            metadata={
                "message": "Future lifecycle note",
                "payload": {"internal": "must-not-render"},
                "payload_sha256": "must-not-render",
                "idempotency_key": "must-not-render",
            },
        )
        student_case_service.append_manual_event(
            self.database_path,
            case_id=self.case_id,
            event_type="transfer_case_malformed_event",
            actor_user_id=1,
            metadata=["not", "an", "object"],
        )
        html = self.render()
        failed_row = html.split(
            "<strong>transfer_case_failed</strong>", 1
        )[1].split("</article>", 1)[0]
        self.assertIn("Failed", failed_row)
        self.assertIn("Evidence could not be obtained", failed_row)
        self.assertIn("User #1", failed_row)
        self.assertIn("in_progress", failed_row)
        self.assertIn("failed", failed_row)
        future_row = html.split(
            "<strong>transfer_case_future_event</strong>", 1
        )[1].split("</article>", 1)[0]
        self.assertIn("Future lifecycle note", future_row)
        self.assertIn("User #1", future_row)
        malformed_row = html.split(
            "<strong>transfer_case_malformed_event</strong>", 1
        )[1].split("</article>", 1)[0]
        self.assertIn("Details unavailable", malformed_row)
        for row in (failed_row, future_row, malformed_row):
            for field in self.SENSITIVE_HISTORY_FIELDS:
                self.assertNotIn(field, row)
            self.assertNotIn("must-not-render", row)
            self.assertNotIn('{"', row)

    def test_transfer_case_created_only_renders_string_initial_note(self):
        student_case_service.append_manual_event(
            self.database_path,
            case_id=self.case_id,
            event_type="transfer_case_created",
            actor_user_id=1,
            metadata={
                "initial_note": "  <script>alert(1)</script> Safe note  ",
                "payload": {"internal": "must-not-render"},
                "idempotency_key": "must-not-render",
            },
        )
        student_case_service.append_manual_event(
            self.database_path,
            case_id=self.case_id,
            event_type="transfer_case_created",
            actor_user_id=1,
            metadata={"initial_note": {"nested": "must-not-render"}},
        )
        student_case_service.append_manual_event(
            self.database_path,
            case_id=self.case_id,
            event_type="transfer_case_created",
            actor_user_id=1,
            metadata={"initial_note": "   "},
        )
        html = self.render()
        rows = html.split(
            "<strong>transfer_case_created</strong>"
        )[1:]
        self.assertEqual(len(rows), 3)
        combined = "".join(rows)
        self.assertIn(
            "&lt;script&gt;alert(1)&lt;/script&gt; Safe note",
            combined,
        )
        self.assertNotIn("<script>alert(1)</script>", combined)
        self.assertEqual(
            sum("Details unavailable" in row for row in rows),
            2,
        )
        self.assertNotIn("must-not-render", combined)
        for field in self.SENSITIVE_HISTORY_FIELDS:
            self.assertNotIn(field, combined)
        self.assertNotIn('{"', combined)


class TransferCompletionRouteTests(unittest.TestCase):
    """T8E real HTTP routes on the existing isolated route harness."""

    def create_legacy_case_fixture(self):
        """Seed the pre-V2 case directly; the public create route is V2-only."""
        self.fixture.ensure_route_activation_authority()
        draft = self.fixture.service.create_transfer_case(
            student_id="1001",
            actor_user_id=1,
            owner_user_id=2,
            title="Route transfer",
            current_school_name_snapshot="Current School",
            current_school_id=10,
            current_city_key="melbourne",
            current_school_sector="public",
            initial_note="Created through isolated legacy fixture",
            source_contract_id=3001,
            idempotency_key="completion-ui-legacy-fixture",
        )
        return phase2_tests.lifecycle_support.activate_transfer(
            self.fixture.service,
            draft,
            actor_user_id=1,
        ).case_id

    def setUp(self):
        self.fixture = phase2_tests.TransferCaseT3RouteTests(
            methodName="runTest"
        )
        self.fixture.before_transfer_cutover = self._seed_before_transfer_cutover
        self.fixture.setUp()
        self.database_path = self.fixture.database_path
        self.handler_class = self.fixture.handler_class
        self.request = self.fixture.request
        self.ready = self._readiness(ready=True)
        self.readiness_patch = patch.object(
            transfer_case_service.TransferCaseService,
            "evaluate_completion_readiness",
            side_effect=lambda case_id: self.ready,
        )
        self.readiness_patch.start()
        self._set_profile(
            "student_cases.view",
            "student_cases.complete",
            "student_cases.undo_complete",
            "student_cases.edit",
            "student_cases.archive",
        )

    def _seed_before_transfer_cutover(self):
        self.database_path = self.fixture.database_path
        self.fixture.service = transfer_case_service.TransferCaseService(
            self.database_path,
            timeout=2.0,
            clock=lambda: "2026-07-30T09:00:00+10:00",
        )
        self.case_id = self.create_legacy_case_fixture()
        self.service = transfer_case_service.TransferCaseService(
            self.database_path,
            timeout=2.0,
            clock=lambda: "2026-07-30T12:00:00+10:00",
        )
        with closing(self.fixture.connect(self.database_path)) as connection:
            connection.execute(
                """INSERT INTO student_school_enrolments(
                       id,student_id,school_id,school_name_snapshot,
                       institution_group_id,city_key,campus_key,school_sector,
                       enrolment_role,source_case_id,status,created_by,
                       created_at,updated_by,updated_at
                   ) VALUES
                   (7002,'1001',20,'Target School',300,'sydney','main',
                    'government','upcoming',?,'active',1,
                    '2026-07-30T09:00:00+10:00',1,
                    '2026-07-30T09:00:00+10:00')""",
                (self.case_id,),
            )
            application_id = connection.execute(
                """INSERT INTO transfer_school_applications(
                       case_id,school_id,school_name_snapshot,
                       institution_group_id,city_key,campus_key,school_sector,
                       expected_start_date,preference_order,
                       application_status,is_final,created_by,created_at,
                       updated_at
                   ) VALUES(
                       ?,20,'Target School',300,'sydney','main','private',
                       '2026-08-10',1,'accepted',1,1,
                       '2026-07-30T09:00:00+10:00',
                       '2026-07-30T09:00:00+10:00'
                   )""",
                (self.case_id,),
            ).lastrowid
            connection.execute(
                """UPDATE transfer_cases
                   SET workflow_state='ready_to_complete',
                       final_application_id=?,
                       current_school_enrolment_id=3001,
                       upcoming_school_enrolment_id=7002,
                       relationship_status='ready'
                   WHERE case_id=?""",
                (application_id, self.case_id),
            )
            cycle_id = connection.execute(
                """SELECT id FROM application_cycles
                     WHERE source_student_case_id=?""",
                (self.case_id,),
            ).fetchone()[0]
            connection.execute(
                """INSERT INTO school_enrolment_transition_intents(
                       id,intent_uid,student_id,source_case_id,
                       application_cycle_id,current_enrolment_id,
                       upcoming_enrolment_id,effective_date,status,reason,
                       created_by,created_at
                   ) VALUES(
                       7100,'transfer-completion-route-transition','1001',?,?,
                       3001,7002,'2026-08-10','pending',
                       'Route fixture explicitly confirmed target school',1,
                       '2026-07-30T09:00:00+10:00'
                   )""",
                (self.case_id, cycle_id),
            )
            connection.execute(
                """UPDATE student_cases
                   SET status='in_progress',active_slot=1,
                       terminal_result=NULL
                   WHERE id=?""",
                (self.case_id,),
            )
            connection.commit()
    def tearDown(self):
        self.readiness_patch.stop()
        self.fixture.tearDown()

    def _readiness(self, *, ready):
        items = tuple(
            TransferReadinessItemDTO(
                code=code,
                label=code,
                status="satisfied" if ready else "missing",
                severity="blocking",
                source_table=None,
                source_id=None,
                blocking_reason=None if ready else code,
                warning_message=None,
                target_anchor="#transfer-completion-readiness",
                evaluated_at="2026-07-30T12:00:00+10:00",
                metadata={},
            )
            for code in READINESS_CODES
        )
        return TransferCompletionReadinessDTO(
            case_id=self.case_id,
            student_id="1001",
            ready=ready,
            evaluated_at="2026-07-30T12:00:00+10:00",
            items=items,
            blocking_count=0 if ready else len(items),
            warning_count=0,
            satisfied_count=len(items) if ready else 0,
            not_applicable_count=0,
            relationship_status="ready",
            final_application_id=1,
            current_enrolment_id=3001,
            upcoming_enrolment_id=7002,
        )

    def _set_profile(self, *permissions):
        values = ["students.view", *permissions]
        self.handler_class.route_user = {
            "id": 1,
            "username": "admin",
            "display_name": "Admin",
            "role_name": "Admin",
            "permissions": values,
            "permissions_json": json.dumps(values),
        }
        self.handler_class.student_case_feature_flags = (
            StudentCaseFeatureFlags(
                read_enabled=True,
                transfer_write_enabled=True,
                completion_enabled=True,
            )
        )

    def _path(self, suffix):
        return (
            f"/students/1001/transfer-cases/{self.case_id}/{suffix}"
        )

    @staticmethod
    def _complete_form(key="route-complete", **overrides):
        values = {
            "effective_date": "2026-08-10",
            "completion_note": "Route completion",
            "confirmed": "1",
            "idempotency_key": key,
        }
        values.update(overrides)
        return values

    def test_complete_and_undo_routes_write_once_and_redirect_to_anchor(self):
        status, headers, _body = self.request(
            self._path("complete"),
            form=self._complete_form(),
        )
        self.assertEqual(status, HTTPStatus.SEE_OTHER)
        self.assertTrue(
            headers["Location"].endswith("#transfer-completion-readiness")
        )
        self.assertEqual(
            self.service.get_transfer_case(self.case_id).status,
            "completed",
        )
        status, headers, _body = self.request(
            self._path("undo-completion"),
            form={
                "reason": "Recorded against the wrong date",
                "confirmed": "1",
                "idempotency_key": "route-undo",
            },
        )
        self.assertEqual(status, HTTPStatus.SEE_OTHER)
        self.assertTrue(
            headers["Location"].endswith("#transfer-completion-readiness")
        )
        self.assertEqual(
            self.service.get_transfer_case(self.case_id).workflow_state,
            "ready_to_complete",
        )
        with closing(self.fixture.connect(self.database_path)) as connection:
            self.assertEqual(
                connection.execute(
                    """SELECT count(*) FROM case_completion_confirmations
                       WHERE case_id=?""",
                    (self.case_id,),
                ).fetchone()[0],
                2,
            )

    def test_complete_route_rejects_wildcard_extra_fields_and_cross_student(self):
        self._set_profile("student_cases.view", "*")
        status, _headers, _body = self.request(
            self._path("complete"),
            form={
                "effective_date": "2026-08-10",
                "confirmed": "1",
                "idempotency_key": "wildcard",
            },
        )
        self.assertEqual(status, HTTPStatus.FORBIDDEN)
        self._set_profile("student_cases.view", "student_cases.complete")
        for path, form in (
            (
                self._path("complete"),
                {
                    "effective_date": "2026-08-10",
                    "confirmed": "1",
                    "idempotency_key": "extra",
                    "workflow_state": "completed",
                },
            ),
            (
                self._path("complete"),
                {
                    "effective_date": "2026-08-10",
                    "confirmed": "1",
                    "idempotency_key": "fixture-profile-extra",
                    "t8e_profile": "undo",
                },
            ),
            (
                f"/students/1002/transfer-cases/{self.case_id}/complete",
                {
                    "effective_date": "2026-08-10",
                    "confirmed": "1",
                    "idempotency_key": "cross-student",
                },
            ),
        ):
            with self.subTest(path=path):
                status, headers, _body = self.request(path, form=form)
                self.assertEqual(status, HTTPStatus.SEE_OTHER)
                self.assertTrue(
                    headers["Location"].endswith(
                        "#transfer-completion-readiness"
                    )
                )
        with closing(self.fixture.connect(self.database_path)) as connection:
            self.assertEqual(
                connection.execute(
                    """SELECT count(*) FROM case_completion_confirmations
                       WHERE case_id=?""",
                    (self.case_id,),
                ).fetchone()[0],
                0,
            )

    def test_complete_route_csrf_permission_flags_and_confirmation_fail_closed(self):
        request = urllib.request.Request(
            self.fixture.base_url + self._path("complete"),
            data=urllib.parse.urlencode(
                {"csrf": "wrong", **self._complete_form("bad-csrf")}
            ).encode(),
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(caught.exception.code, HTTPStatus.FORBIDDEN)

        for permissions, flags, form, expected in (
            (
                ("student_cases.view",),
                StudentCaseFeatureFlags(
                    read_enabled=True,
                    transfer_write_enabled=True,
                    completion_enabled=True,
                ),
                self._complete_form("no-permission"),
                HTTPStatus.FORBIDDEN,
            ),
            (
                ("student_cases.view", "student_cases.complete"),
                StudentCaseFeatureFlags(
                    read_enabled=True,
                    transfer_write_enabled=False,
                    completion_enabled=True,
                ),
                self._complete_form("write-off"),
                HTTPStatus.FORBIDDEN,
            ),
            (
                ("student_cases.view", "student_cases.complete"),
                StudentCaseFeatureFlags(
                    read_enabled=True,
                    transfer_write_enabled=True,
                    completion_enabled=False,
                ),
                self._complete_form("completion-off"),
                HTTPStatus.FORBIDDEN,
            ),
            (
                ("student_cases.view", "student_cases.complete"),
                StudentCaseFeatureFlags(
                    read_enabled=True,
                    transfer_write_enabled=True,
                    completion_enabled=True,
                ),
                self._complete_form("not-confirmed", confirmed=""),
                HTTPStatus.SEE_OTHER,
            ),
        ):
            with self.subTest(
                permissions=permissions,
                flags=flags,
                form=form,
            ):
                self._set_profile(*permissions)
                self.handler_class.student_case_feature_flags = flags
                status, headers, _body = self.request(
                    self._path("complete"), form=form
                )
                self.assertEqual(status, expected)
                if expected == HTTPStatus.SEE_OTHER:
                    self.assertTrue(
                        headers["Location"].endswith(
                            "#transfer-completion-readiness"
                        )
                    )
        with closing(self.fixture.connect(self.database_path)) as connection:
            self.assertEqual(
                connection.execute(
                    """SELECT count(*) FROM case_completion_confirmations
                       WHERE case_id=?""",
                    (self.case_id,),
                ).fetchone()[0],
                0,
            )

    def test_complete_route_blocked_invalid_date_and_wrong_case_write_nothing(self):
        self.ready = self._readiness(ready=False)
        status, headers, _body = self.request(
            self._path("complete"),
            form=self._complete_form("blocked"),
        )
        self.assertEqual(status, HTTPStatus.SEE_OTHER)
        self.assertTrue(
            headers["Location"].endswith("#transfer-completion-readiness")
        )
        status, _headers, body = self.request(headers["Location"])
        self.assertEqual(status, HTTPStatus.OK, body)
        self.assertIn("Case尚未满足全部完成条件", body)
        self.assertIn("历史转学记录 · 办理状态待确认", body)
        self.assertIn("只读查看", body)
        self.assertNotIn('class="transfer-workbench', body)
        self.ready = self._readiness(ready=True)
        for path, form in (
            (
                self._path("complete"),
                self._complete_form(
                    "bad-date", effective_date="10/08/2026"
                ),
            ),
            (
                f"/students/1001/transfer-cases/{self.case_id + 999}/complete",
                self._complete_form("wrong-case"),
            ),
        ):
            with self.subTest(path=path):
                status, headers, _body = self.request(path, form=form)
                self.assertEqual(status, HTTPStatus.SEE_OTHER)
                self.assertTrue(
                    headers["Location"].endswith(
                        "#transfer-completion-readiness"
                    )
                )
        with closing(self.fixture.connect(self.database_path)) as connection:
            self.assertEqual(
                connection.execute(
                    """SELECT count(*) FROM case_completion_confirmations
                       WHERE case_id=?""",
                    (self.case_id,),
                ).fetchone()[0],
                0,
            )

    def test_complete_route_idempotent_retry_conflict_and_repeat_are_bounded(self):
        for form in (
            self._complete_form("same-key"),
            self._complete_form("same-key"),
            self._complete_form(
                "same-key", completion_note="Different payload"
            ),
            self._complete_form("different-key"),
        ):
            status, headers, _body = self.request(
                self._path("complete"), form=form
            )
            self.assertEqual(status, HTTPStatus.SEE_OTHER)
            self.assertTrue(
                headers["Location"].endswith(
                    "#transfer-completion-readiness"
                )
            )
        with closing(self.fixture.connect(self.database_path)) as connection:
            self.assertEqual(
                connection.execute(
                    """SELECT count(*) FROM case_completion_confirmations
                       WHERE case_id=?""",
                    (self.case_id,),
                ).fetchone()[0],
                1,
            )

    def test_undo_route_rejects_archived_extra_cross_student_and_repeat(self):
        self.request(
            self._path("complete"),
            form=self._complete_form("undo-prep"),
        )
        status, _headers, _body = self.request(
            self._path("undo-completion"),
            form={
                "reason": "Extra field",
                "confirmed": "1",
                "idempotency_key": "undo-extra",
                "workflow_state": "ready_to_complete",
            },
        )
        self.assertEqual(status, HTTPStatus.SEE_OTHER)
        status, _headers, _body = self.request(
            f"/students/1002/transfer-cases/{self.case_id}/undo-completion",
            form={
                "reason": "Cross student",
                "confirmed": "1",
                "idempotency_key": "undo-cross",
            },
        )
        self.assertEqual(status, HTTPStatus.SEE_OTHER)
        self.request(
            self._path("archive"),
            form={"reason": "", "idempotency_key": "archive-before-undo"},
        )
        status, _headers, _body = self.request(
            self._path("undo-completion"),
            form={
                "reason": "Archived",
                "confirmed": "1",
                "idempotency_key": "undo-archived",
            },
        )
        self.assertEqual(status, HTTPStatus.SEE_OTHER)
        self.request(
            self._path("unarchive"),
            form={"idempotency_key": "unarchive-before-undo"},
        )
        form = {
            "reason": "Undo exact completion",
            "confirmed": "1",
            "idempotency_key": "undo-once",
        }
        for _attempt in range(2):
            status, headers, _body = self.request(
                self._path("undo-completion"), form=form
            )
            self.assertEqual(status, HTTPStatus.SEE_OTHER)
            self.assertTrue(
                headers["Location"].endswith(
                    "#transfer-completion-readiness"
                )
            )
        with closing(self.fixture.connect(self.database_path)) as connection:
            actions = [
                row[0] for row in connection.execute(
                    """SELECT action FROM case_completion_confirmations
                       WHERE case_id=? ORDER BY id""",
                    (self.case_id,),
                )
            ]
        self.assertEqual(actions, ["complete", "undo_completion"])

    def test_undo_route_permission_and_relationship_drift_are_rejected(self):
        self.request(
            self._path("complete"),
            form=self._complete_form("drift-prep"),
        )
        self._set_profile(
            "student_cases.view", "student_cases.complete"
        )
        status, _headers, _body = self.request(
            self._path("undo-completion"),
            form={
                "reason": "No permission",
                "confirmed": "1",
                "idempotency_key": "undo-no-permission",
            },
        )
        self.assertEqual(status, HTTPStatus.FORBIDDEN)
        self._set_profile(
            "student_cases.view", "student_cases.undo_complete"
        )
        with closing(self.fixture.connect(self.database_path)) as connection:
            connection.execute(
                """UPDATE student_school_enrolments
                   SET school_name_snapshot='Changed after completion'
                   WHERE id=7002"""
            )
            connection.commit()
        status, headers, _body = self.request(
            self._path("undo-completion"),
            form={
                "reason": "Relationship drift",
                "confirmed": "1",
                "idempotency_key": "undo-drift",
            },
        )
        self.assertEqual(status, HTTPStatus.SEE_OTHER)
        self.assertTrue(
            headers["Location"].endswith("#transfer-completion-readiness")
        )
        with closing(self.fixture.connect(self.database_path)) as connection:
            self.assertEqual(
                connection.execute(
                    """SELECT count(*) FROM case_completion_confirmations
                       WHERE case_id=? AND action='undo_completion'""",
                    (self.case_id,),
                ).fetchone()[0],
                0,
            )

    def test_status_routes_enforce_fields_permissions_and_anchor(self):
        actions = (
            ("pause", {"reason": "Waiting", "idempotency_key": "pause"}),
            ("resume", {"idempotency_key": "resume"}),
            (
                "cancel",
                {"reason": "Withdrawn", "idempotency_key": "cancel"},
            ),
            ("archive", {"reason": "", "idempotency_key": "archive"}),
            (
                "unarchive",
                {"idempotency_key": "unarchive"},
            ),
        )
        for action, form in actions:
            with self.subTest(action=action):
                status, headers, _body = self.request(
                    self._path(action), form=form
                )
                self.assertEqual(status, HTTPStatus.SEE_OTHER)
                self.assertTrue(
                    headers["Location"].endswith(
                        "#transfer-completion-readiness"
                    )
                )
        self.assertEqual(
            self.service.get_transfer_case(self.case_id).status,
            "cancelled",
        )

        self._set_profile("student_cases.view", "student_cases.edit")
        status, _headers, _body = self.request(
            self._path("archive"),
            form={"idempotency_key": "forbidden-archive"},
        )
        self.assertEqual(status, HTTPStatus.FORBIDDEN)

    def test_status_routes_reject_missing_reason_extra_fields_and_flag_off(self):
        for action, form in (
            ("pause", {"reason": "", "idempotency_key": "missing-reason"}),
            (
                "pause",
                {
                    "reason": "Injected",
                    "idempotency_key": "extra-field",
                    "status": "paused",
                },
            ),
        ):
            with self.subTest(action=action, form=form):
                status, headers, _body = self.request(
                    self._path(action), form=form
                )
                self.assertEqual(status, HTTPStatus.SEE_OTHER)
                self.assertTrue(
                    headers["Location"].endswith(
                        "#transfer-completion-readiness"
                    )
                )
        self.assertEqual(
            self.service.get_transfer_case(self.case_id).status,
            "in_progress",
        )
        self.handler_class.student_case_feature_flags = (
            StudentCaseFeatureFlags(read_enabled=True)
        )
        status, _headers, _body = self.request(
            self._path("pause"),
            form={"reason": "Flag off", "idempotency_key": "flag-off"},
        )
        self.assertEqual(status, HTTPStatus.FORBIDDEN)

    def test_completed_backend_rejects_forged_t3_t7_writes(self):
        self.request(
            self._path("complete"),
            form=self._complete_form("terminal-lock"),
        )
        attempts = (
            (
                f"/students/1001/transfer-cases/{self.case_id}/edit",
                {
                    "title": "Forged terminal edit",
                    "current_school_name_snapshot": "Changed",
                    "current_school_id": "10",
                    "current_city_key": "melbourne",
                    "current_school_sector": "public",
                },
                HTTPStatus.BAD_REQUEST,
            ),
            (
                f"/students/1001/transfer-cases/{self.case_id}/applications",
                {
                    "school_name_snapshot": "Forged application",
                    "application_status": "preparing",
                },
                HTTPStatus.SEE_OTHER,
            ),
            (
                f"/students/1001/transfer-cases/{self.case_id}"
                "/arrangements/visa/pending",
                {},
                HTTPStatus.SEE_OTHER,
            ),
        )
        for path, form, expected_status in attempts:
            with self.subTest(path=path):
                status, _headers, _body = self.request(path, form=form)
                self.assertEqual(status, expected_status)
        case = self.service.get_transfer_case(self.case_id)
        self.assertNotEqual(case.title, "Forged terminal edit")
        with closing(self.fixture.connect(self.database_path)) as connection:
            self.assertEqual(
                connection.execute(
                    """SELECT count(*) FROM transfer_school_applications
                       WHERE case_id=? AND school_name_snapshot=
                       'Forged application'""",
                    (self.case_id,),
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    """SELECT count(*) FROM case_arrangement_confirmations
                       WHERE case_id=?""",
                    (self.case_id,),
                ).fetchone()[0],
                0,
            )


if __name__ == "__main__":
    unittest.main()
