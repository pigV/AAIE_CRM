from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
import unittest

from student_lifecycle.transfer_v2_models import (
    TransferV2CreateInput,
    TransferV2TargetInput,
)
from student_lifecycle.transfer_v2_service import (
    TransferV2ActiveConflict,
    TransferV2Service,
    TransferV2ValidationError,
)
from student_lifecycle.transfer_source_authority import save_authority
from tests.transfer_v2_test_support import (
    create_transfer_v2_database,
    query_all,
    query_one,
)


NOW = "2026-08-27T09:30:00+10:00"


class TransferV2CreationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.database_path = Path(self.temp_dir.name) / "transfer-v2.sqlite3"
        create_transfer_v2_database(self.database_path)
        self.service = TransferV2Service(
            self.database_path,
            clock=lambda: NOW,
            uid_factory=lambda prefix: f"{prefix}-fixed",
        )
        self.request = TransferV2CreateInput(
            student_id="S1",
            source_contract_id=101,
            source_label="aaie_existing",
            current_school_id=10,
            current_school_name="Current College",
            current_year_level="Year 8",
            application_year_level="Year 9",
            transfer_year=2027,
            transfer_term="Term 1",
            targets=(
                TransferV2TargetInput(
                    school_id=20,
                    school_name="Target Grammar",
                    state="VIC",
                    region="Melbourne",
                    school_type="Independent",
                ),
                TransferV2TargetInput(
                    school_id=30,
                    school_name="Target High",
                    state="NSW",
                    region="Sydney",
                    school_type="Government",
                ),
                TransferV2TargetInput(
                    school_id=None,
                    school_name="Manual Future School",
                    state="QLD",
                    region="Brisbane",
                    school_type="Independent",
                ),
            ),
        )

    def test_contract_eligibility_uses_fully_executed_unbound_transfer_contract(self):
        decision = self.service.creation_eligibility("S1")
        self.assertTrue(decision.allowed)
        self.assertEqual(101, decision.contract_id)
        self.assertEqual("转学合同已完成 · 待建立转学记录", decision.label)

        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "UPDATE contracts SET final_signed_file_path=NULL WHERE id=101"
            )
            connection.commit()
        blocked = self.service.creation_eligibility("S1")
        self.assertFalse(blocked.allowed)
        self.assertEqual("等待转学合同签署", blocked.label)

        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "UPDATE contracts SET final_signed_file_path=? WHERE id=101",
                (str(Path(self.temp_dir.name) / "missing-final-contract.pdf"),),
            )
            connection.commit()
        missing_artifact = self.service.creation_eligibility("S1")
        self.assertFalse(missing_artifact.allowed)
        self.assertEqual("等待转学合同签署", missing_artifact.label)

    def test_creation_reads_actor_name_from_production_display_name_schema(self):
        with sqlite3.connect(self.database_path) as connection:
            connection.execute("ALTER TABLE users RENAME COLUMN name TO display_name")
            connection.commit()

        overview = self.service.create_cycle(
            self.request, actor_user_id=1, idempotency_key="display-name-schema"
        )

        self.assertEqual("S1", overview.student_id)

    def test_create_cycle_is_atomic_and_creates_three_stable_branches(self):
        overview = self.service.create_cycle(
            self.request, actor_user_id=1, idempotency_key="create-S1-1"
        )
        self.assertEqual("S1", overview.student_id)
        self.assertEqual("materials_preparation", overview.lifecycle_stage)
        self.assertEqual(3, len(overview.branches))
        self.assertEqual(
            ["Target Grammar", "Target High", "Manual Future School"],
            [branch.school_name for branch in overview.branches],
        )
        transfer = query_one(
            self.database_path,
            "SELECT * FROM transfer_cases WHERE case_id=?",
            (overview.case_id,),
        )
        self.assertEqual(2, transfer["v2_version"])
        self.assertEqual("aaie_existing", transfer["transfer_source_label"])
        self.assertEqual("Year 9", transfer["application_year_level"])
        self.assertEqual(2027, transfer["transfer_year"])
        self.assertEqual(101, transfer["source_contract_id"])
        branch_rows = query_all(
            self.database_path,
            "SELECT * FROM transfer_school_applications WHERE application_cycle_id=? ORDER BY preference_order",
            (overview.cycle_id,),
        )
        self.assertEqual([20, 30, None], [row["school_id"] for row in branch_rows])
        self.assertTrue(all(row["branch_uid"] for row in branch_rows))
        self.assertEqual(
            3,
            query_one(
                self.database_path,
                "SELECT COUNT(*) AS count FROM transfer_target_state_events WHERE application_cycle_id=?",
                (overview.cycle_id,),
            )["count"],
        )
        replay = self.service.create_cycle(
            self.request, actor_user_id=1, idempotency_key="create-S1-1"
        )
        self.assertEqual(overview.cycle_id, replay.cycle_id)

    def test_add_target_school_appends_one_branch_to_same_cycle_idempotently(self):
        overview = self.service.create_cycle(
            self.request, actor_user_id=1, idempotency_key="create-for-add-target"
        )
        cycle_count_before = query_one(
            self.database_path,
            "SELECT COUNT(*) AS count FROM application_cycles WHERE student_id='S1'",
        )["count"]

        added = self.service.add_target_branch(
            overview.cycle_id,
            target=TransferV2TargetInput(
                school_id=None,
                school_name="Another Manual School",
                state="SA",
                region="Adelaide",
                school_type="Independent",
            ),
            actor_user_id=1,
            idempotency_key="add-target-1",
        )
        replay = self.service.add_target_branch(
            overview.cycle_id,
            target=TransferV2TargetInput(
                school_id=None,
                school_name="Another Manual School",
                state="SA",
                region="Adelaide",
                school_type="Independent",
            ),
            actor_user_id=1,
            idempotency_key="add-target-1",
        )

        self.assertEqual(overview.cycle_id, added.cycle_id)
        self.assertEqual(overview.case_id, added.case_id)
        self.assertEqual(4, len(added.branches))
        self.assertEqual(
            [branch.branch_id for branch in added.branches],
            [branch.branch_id for branch in replay.branches],
        )
        self.assertEqual(
            cycle_count_before,
            query_one(
                self.database_path,
                "SELECT COUNT(*) AS count FROM application_cycles WHERE student_id='S1'",
            )["count"],
        )
        branch = added.branches[-1]
        self.assertEqual("Another Manual School", branch.school_name)
        self.assertEqual("materials_preparation", branch.current_state)
        self.assertEqual(4, branch.preference_order)

    def test_add_target_school_rejects_duplicate_and_closed_cycle_without_write(self):
        overview = self.service.create_cycle(
            self.request, actor_user_id=1, idempotency_key="create-for-add-guard"
        )
        with self.assertRaisesRegex(TransferV2ValidationError, "unique"):
            self.service.add_target_branch(
                overview.cycle_id,
                target=TransferV2TargetInput(
                    school_id=20,
                    school_name="Target Grammar",
                    state="VIC",
                    region="Melbourne",
                    school_type="Independent",
                ),
                actor_user_id=1,
                idempotency_key="duplicate-target-20",
            )
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "UPDATE application_cycles SET status='completed' WHERE id=?",
                (overview.cycle_id,),
            )
            connection.commit()
        with self.assertRaisesRegex(TransferV2ValidationError, "active"):
            self.service.add_target_branch(
                overview.cycle_id,
                target=TransferV2TargetInput(
                    school_id=None,
                    school_name="Closed Cycle School",
                    state=None,
                    region=None,
                    school_type=None,
                ),
                actor_user_id=1,
                idempotency_key="closed-cycle-target",
            )
        self.assertEqual(
            3,
            query_one(
                self.database_path,
                "SELECT COUNT(*) AS count FROM transfer_school_applications WHERE application_cycle_id=?",
                (overview.cycle_id,),
            )["count"],
        )
    def test_grade_and_term_values_are_restricted_to_transfer_form_contract(self):
        invalid_cases = (
            replace(self.request, current_year_level="Grade 8"),
            replace(self.request, application_year_level="Foundation"),
            replace(self.request, transfer_term="Semester 1"),
        )
        for index, request in enumerate(invalid_cases, start=1):
            with tempfile.TemporaryDirectory() as temp_dir, self.subTest(index=index):
                database_path = Path(temp_dir) / "invalid-choice.sqlite3"
                create_transfer_v2_database(database_path)
                service = TransferV2Service(database_path, clock=lambda: NOW)
                with self.assertRaises(TransferV2ValidationError):
                    service.create_cycle(
                        request,
                        actor_user_id=1,
                        idempotency_key=f"invalid-choice-{index}",
                    )

    def test_school_identifier_requires_matching_canonical_name(self):
        invalid_cases = (
            replace(self.request, current_school_name="Edited Current School"),
            replace(
                self.request,
                targets=(
                    replace(self.request.targets[0], school_name="Edited Target School"),
                ),
            ),
        )
        for index, request in enumerate(invalid_cases, start=1):
            with tempfile.TemporaryDirectory() as temp_dir, self.subTest(index=index):
                database_path = Path(temp_dir) / "school-mismatch.sqlite3"
                create_transfer_v2_database(database_path)
                service = TransferV2Service(database_path, clock=lambda: NOW)
                with self.assertRaisesRegex(
                    TransferV2ValidationError,
                    "school name and identifier do not match",
                ):
                    service.create_cycle(
                        request,
                        actor_user_id=1,
                        idempotency_key=f"school-mismatch-{index}",
                    )

    def test_selected_school_snapshot_uses_canonical_city_when_region_is_empty(self):
        with sqlite3.connect(self.database_path) as connection:
            connection.execute("ALTER TABLE schools ADD COLUMN city_id INTEGER")
            connection.execute(
                "CREATE TABLE school_cities(id INTEGER PRIMARY KEY, city_name TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO school_cities(id,city_name) VALUES(1,'Melbourne')"
            )
            connection.execute(
                "UPDATE schools SET region=NULL,city_id=1 WHERE id IN (10,20)"
            )
            connection.commit()

        overview = self.service.create_cycle(
            self.request,
            actor_user_id=1,
            idempotency_key="canonical-city-region",
        )
        transfer = query_one(
            self.database_path,
            "SELECT current_school_region_snapshot FROM transfer_cases WHERE case_id=?",
            (overview.case_id,),
        )
        branch = query_one(
            self.database_path,
            """SELECT school_region_snapshot
                 FROM transfer_school_applications
                WHERE application_cycle_id=? AND preference_order=1""",
            (overview.cycle_id,),
        )
        self.assertEqual("Melbourne", transfer["current_school_region_snapshot"])
        self.assertEqual("Melbourne", branch["school_region_snapshot"])

    def test_second_active_transfer_is_rejected_including_legacy_active(self):
        self.service.create_cycle(
            self.request, actor_user_id=1, idempotency_key="create-S1-1"
        )
        with self.assertRaisesRegex(
            TransferV2ActiveConflict,
            "已有进行中的转学记录，不能重复建立。",
        ):
            self.service.create_cycle(
                self.request, actor_user_id=1, idempotency_key="create-S1-2"
            )
        legacy_request = replace(
            self.request,
            student_id="S3",
            source_contract_id=101,
        )
        with self.assertRaises(TransferV2ActiveConflict):
            self.service.create_cycle(
                legacy_request, actor_user_id=1, idempotency_key="create-S3"
            )

    def test_completed_and_cancelled_transfer_history_each_allow_a_new_cycle(self):
        for terminal_status in ("completed", "cancelled"):
            with tempfile.TemporaryDirectory() as temp_dir, self.subTest(
                status=terminal_status
            ):
                database_path = Path(temp_dir) / "history.sqlite3"
                create_transfer_v2_database(database_path)
                service = TransferV2Service(
                    database_path,
                    clock=lambda: NOW,
                    uid_factory=lambda prefix: f"{prefix}-{terminal_status}",
                )
                overview = service.create_cycle(
                    self.request,
                    actor_user_id=1,
                    idempotency_key=f"history-{terminal_status}-first",
                )
                next_signed_contract = Path(temp_dir) / "signed-contracts" / "103.pdf"
                next_signed_contract.write_bytes(
                    b"%PDF-1.4\n% transfer history test artifact\n"
                )
                with sqlite3.connect(database_path) as connection:
                    connection.execute(
                        "UPDATE application_cycles SET status=?,closed_at=? WHERE id=?",
                        (terminal_status, NOW, overview.cycle_id),
                    )
                    connection.execute(
                        "UPDATE student_cases SET status=?,active_slot=0 WHERE id=?",
                        (terminal_status, overview.case_id),
                    )
                    connection.execute(
                        "INSERT INTO contracts VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            103,
                            "S1",
                            "TransferApplicationContract",
                            "FullyExecuted",
                            str(next_signed_contract),
                            NOW,
                            NOW,
                            NOW,
                            None,
                            None,
                            None,
                        ),
                    )
                    saved = connection.execute(
                        "SELECT current_enrolment_id, ordinary_relationship_cycle_id, ordinary_relationship_contract_id "
                        "FROM transfer_source_authorities WHERE transfer_contract_id=101"
                    ).fetchone()
                    save_authority(
                        connection,
                        "S1",
                        103,
                        current_enrolment_id=saved[0],
                        ordinary_relationship_cycle_id=saved[1],
                        ordinary_relationship_contract_id=saved[2],
                        actor_user_id=1,
                        occurred_at=NOW,
                    )
                    connection.commit()
                second = service.create_cycle(
                    replace(self.request, source_contract_id=103),
                    actor_user_id=1,
                    idempotency_key=f"history-{terminal_status}-second",
                )
                self.assertNotEqual(overview.cycle_id, second.cycle_id)

    def test_active_non_transfer_school_cycle_with_offer_is_blocked_before_insert(self):
        with sqlite3.connect(self.database_path) as connection:
            cycle_id = connection.execute(
                """INSERT INTO application_cycles(
                     cycle_uid,student_id,cycle_type,sequence_no,status,opened_at,
                     created_by_user_id,created_by_name,created_at,updated_by_user_id,
                     updated_by_name,updated_at,education_stream)
                   VALUES('active-initial-S2','S2','initial',1,'active',?,1,'Alice',?,1,'Alice',?,'school_education')""",
                (NOW, NOW, NOW),
            ).lastrowid
            connection.execute(
                """INSERT INTO offer_received_events(
                     application_cycle_id,student_id,source_kind,school_name_snapshot,
                     external_offer_key,idempotency_key,received_at,created_at)
                   VALUES(?,'S2','private','Real Applied School','active-offer',
                          'active-offer',?,?)""",
                (cycle_id, NOW, NOW),
            )
            connection.commit()
        request = replace(self.request, student_id="S2", source_contract_id=102)
        decision = self.service.creation_eligibility("S2")
        self.assertFalse(decision.allowed)
        self.assertEqual("active_school_cycle_has_business_facts", decision.status)
        with self.assertRaisesRegex(
            TransferV2ActiveConflict,
            "已有进行中的申请周期，不能直接建立转学。",
        ):
            self.service.create_cycle(
                request, actor_user_id=1, idempotency_key="active-initial-S2"
            )
        self.assertEqual(
            0,
            query_one(
                self.database_path,
                "SELECT COUNT(*) AS count FROM transfer_cases WHERE v2_version=2",
            )["count"],
        )

    def test_default_uid_factory_replays_same_create_key_as_one_cycle(self):
        service = TransferV2Service(self.database_path, clock=lambda: NOW)
        first = service.create_cycle(
            self.request, actor_user_id=1, idempotency_key="browser-double-click"
        )
        second = service.create_cycle(
            self.request, actor_user_id=1, idempotency_key="browser-double-click"
        )
        self.assertEqual(first.case_id, second.case_id)
        self.assertEqual(first.cycle_id, second.cycle_id)
        self.assertEqual(
            1,
            query_one(
                self.database_path,
                "SELECT COUNT(*) AS count FROM application_cycles WHERE cycle_type='transfer' AND student_id='S1'",
            )["count"],
        )

    def test_same_create_key_with_changed_payload_has_business_error(self):
        service = TransferV2Service(self.database_path, clock=lambda: NOW)
        service.create_cycle(
            self.request, actor_user_id=1, idempotency_key="changed-payload"
        )
        with self.assertRaisesRegex(
            TransferV2ValidationError,
            "转学记录已经建立，请勿重复提交。",
        ):
            service.create_cycle(
                replace(self.request, transfer_term="Term 2"),
                actor_user_id=1,
                idempotency_key="changed-payload",
            )

    def test_invalid_target_rolls_back_case_cycle_contract_and_events(self):
        invalid = replace(
            self.request,
            targets=self.request.targets + (
                TransferV2TargetInput(
                    school_id=999,
                    school_name="Wrong School",
                    state="VIC",
                    region="Melbourne",
                    school_type="Independent",
                ),
            ),
        )
        with self.assertRaises(TransferV2ValidationError):
            self.service.create_cycle(
                invalid, actor_user_id=1, idempotency_key="invalid-target"
            )
        self.assertEqual(
            0,
            query_one(
                self.database_path,
                "SELECT COUNT(*) AS count FROM transfer_cases WHERE v2_version=2",
            )["count"],
        )
        self.assertIsNone(query_one(
            self.database_path,
            "SELECT application_cycle_id FROM contracts WHERE id=101",
        )["application_cycle_id"])

    def test_duplicate_target_identity_is_rejected_before_any_write(self):
        duplicate = replace(
            self.request,
            targets=(self.request.targets[0], self.request.targets[0]),
        )
        with self.assertRaisesRegex(TransferV2ValidationError, "unique"):
            self.service.create_cycle(
                duplicate, actor_user_id=1, idempotency_key="duplicate-target"
            )
        self.assertEqual(
            0,
            query_one(
                self.database_path,
                "SELECT COUNT(*) AS count FROM transfer_cases WHERE v2_version=2",
            )["count"],
        )

    def test_consumed_source_cannot_change_and_legacy_is_not_v2(self):
        overview = self.service.create_cycle(
            self.request, actor_user_id=1, idempotency_key="create-S1-1"
        )
        with self.assertRaisesRegex(TransferV2ValidationError, "来源"):
            self.service.change_origin(
                overview.cycle_id,
                new_source_label="new_transfer",
                actor_user_id=2,
                note="Attempt to overwrite established source",
                idempotency_key="origin-change-1",
            )
        self.assertEqual(
            "aaie_existing",
            self.service.load_overview(overview.cycle_id).source_label,
        )
        events = query_all(
            self.database_path,
            "SELECT previous_source_label,new_source_label,actor_user_id FROM transfer_v2_origin_events WHERE application_cycle_id=? ORDER BY id",
            (overview.cycle_id,),
        )
        self.assertEqual(
            [(None, "aaie_existing", 1)],
            [tuple(row) for row in events],
        )
        legacy = self.service.load_overview(190)
        self.assertTrue(legacy.is_legacy)
        self.assertEqual("Legacy Transfer · Lifecycle unconfirmed", legacy.lifecycle_label)

    def test_manual_school_can_submit_project_local_library_request(self):
        overview = self.service.create_cycle(
            self.request, actor_user_id=1, idempotency_key="create-manual-request"
        )
        manual = next(branch for branch in overview.branches if branch.school_id is None)
        request_id = self.service.request_school_library(
            overview.cycle_id,
            branch_id=manual.branch_id,
            proposed_name=manual.school_name,
            proposed_state="QLD",
            proposed_region="Brisbane",
            proposed_school_type="Independent",
            actor_user_id=1,
            idempotency_key="library-request-1",
        )
        replay = self.service.request_school_library(
            overview.cycle_id,
            branch_id=manual.branch_id,
            proposed_name=manual.school_name,
            proposed_state="QLD",
            proposed_region="Brisbane",
            proposed_school_type="Independent",
            actor_user_id=1,
            idempotency_key="library-request-1",
        )
        self.assertEqual(request_id, replay)
        row = query_one(
            self.database_path,
            "SELECT * FROM transfer_school_library_requests WHERE id=?",
            (request_id,),
        )
        self.assertEqual("pending", row["status"])
        self.assertEqual(manual.branch_id, row["branch_id"])


if __name__ == "__main__":
    unittest.main()
