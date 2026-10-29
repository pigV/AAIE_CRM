from pathlib import Path
import sqlite3
import tempfile
import unittest

from student_lifecycle.transfer_v2_documents import (
    TransferV2DocumentService,
    TransferV2DocumentValidationError,
)
from student_lifecycle.transfer_v2_models import (
    TransferV2CreateInput,
    TransferV2TargetInput,
)
from student_lifecycle.transfer_v2_service import TransferV2Service
from tests.transfer_v2_test_support import (
    create_transfer_v2_database,
    query_all,
    query_one,
)


NOW = "2026-08-27T10:00:00+10:00"


class TransferV2BranchesDocumentsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.database_path = Path(self.temp_dir.name) / "transfer-v2-documents.sqlite3"
        create_transfer_v2_database(self.database_path)
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """INSERT INTO application_cycles(
                     id,cycle_uid,student_id,cycle_type,sequence_no,status,outcome,
                     opened_at,closed_at,created_at,updated_at,education_stream)
                   VALUES(180,'S1-initial','S1','initial',1,'closed','completed',
                          '2025-01-01','2025-12-01','2025-01-01','2025-12-01',
                          'school_education')"""
            )
            connection.executemany(
                """INSERT INTO student_documents(
                     id,student_id,document_type,file_path,uploaded_at,document_date)
                   VALUES(?,?,?,?,?,?)""",
                (
                    (401, "S1", "Passport", "/nas/S1/passport-2025.pdf", "2025-02-01", "2025-01-15"),
                    (402, "S1", "Transcript", "/nas/S1/transcript-2025.pdf", "2025-12-01", "2025-11-30"),
                    (403, "S1", "Transcript", "/nas/S1/transcript-2026.pdf", "2026-08-27", "2026-08-20"),
                    (404, "S1", "Passport", "/nas/S1/passport-2026.pdf", "2026-08-27", "2026-08-21"),
                ),
            )
            connection.commit()
        creator = TransferV2Service(
            self.database_path,
            clock=lambda: NOW,
            uid_factory=lambda prefix: f"{prefix}-fixed",
        )
        self.overview = creator.create_cycle(
            TransferV2CreateInput(
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
                    TransferV2TargetInput(20, "Target Grammar"),
                    TransferV2TargetInput(30, "Target High"),
                    TransferV2TargetInput(None, "Manual School", "QLD", "Brisbane", "Independent"),
                ),
            ),
            actor_user_id=1,
            idempotency_key="create-doc-test",
        )
        self.branch = self.overview.branches[0]
        self.other_branch = self.overview.branches[1]
        self.documents = TransferV2DocumentService(
            self.database_path,
            clock=lambda: NOW,
            uid_factory=lambda prefix: f"{prefix}-fixed",
        )

    def test_branch_checklists_are_independent_and_missing_override_is_audited(self):
        passport = self.documents.add_requirement(
            cycle_id=self.overview.cycle_id,
            branch_id=self.branch.branch_id,
            document_type="Passport",
            label="Passport",
            requires_current_cycle=False,
            actor_user_id=1,
            idempotency_key="req-passport",
        )
        transcript = self.documents.add_requirement(
            cycle_id=self.overview.cycle_id,
            branch_id=self.branch.branch_id,
            document_type="Transcript",
            label="Latest transcript",
            requires_current_cycle=True,
            actor_user_id=1,
            idempotency_key="req-transcript",
        )
        self.documents.add_requirement(
            cycle_id=self.overview.cycle_id,
            branch_id=self.other_branch.branch_id,
            document_type="Birth Certificate",
            label="Birth Certificate",
            requires_current_cycle=False,
            actor_user_id=1,
            idempotency_key="req-birth",
        )
        with self.assertRaises(TransferV2DocumentValidationError):
            self.documents.transition_branch(
                cycle_id=self.overview.cycle_id,
                branch_id=self.branch.branch_id,
                new_state="application_in_progress",
                actor_user_id=1,
                allow_missing=False,
                note=None,
                idempotency_key="submit-without-override",
            )
        transition = self.documents.transition_branch(
            cycle_id=self.overview.cycle_id,
            branch_id=self.branch.branch_id,
            new_state="application_in_progress",
            actor_user_id=1,
            allow_missing=True,
            note="School accepted pending documents",
            idempotency_key="submit-with-override",
        )
        self.assertEqual("application_in_progress", transition.new_state)
        self.assertEqual(
            [passport.requirement_uid, transcript.requirement_uid],
            list(transition.missing_requirement_uids),
        )
        stored = query_one(
            self.database_path,
            """SELECT missing_checklist_json, note, actor_user_id,
                      occurred_at, created_at
                 FROM transfer_target_state_events WHERE event_uid=?""",
            (transition.event_uid,),
        )
        self.assertIn(passport.requirement_uid, stored["missing_checklist_json"])
        self.assertIn(transcript.requirement_uid, stored["missing_checklist_json"])
        self.assertEqual("School accepted pending documents", stored["note"])
        self.assertEqual(1, stored["actor_user_id"])
        self.assertTrue(stored["occurred_at"])
        self.assertTrue(stored["created_at"])
        other = self.documents.load_checklist(
            self.overview.cycle_id, self.other_branch.branch_id
        )
        self.assertEqual(["Birth Certificate"], [item.label for item in other])

    def test_existing_passport_can_satisfy_but_historical_transcript_cannot(self):
        passport = self.documents.add_requirement(
            cycle_id=self.overview.cycle_id,
            branch_id=self.branch.branch_id,
            document_type="Passport",
            label="Passport",
            requires_current_cycle=False,
            actor_user_id=1,
            idempotency_key="req-passport",
        )
        transcript = self.documents.add_requirement(
            cycle_id=self.overview.cycle_id,
            branch_id=self.branch.branch_id,
            document_type="Transcript",
            label="Latest transcript",
            requires_current_cycle=True,
            actor_user_id=1,
            idempotency_key="req-transcript",
        )
        passport_binding = self.documents.bind_document(
            cycle_id=self.overview.cycle_id,
            branch_id=self.branch.branch_id,
            document_id=401,
            source_cycle_id=180,
            document_type="Passport",
            document_date="2025-01-15",
            actor_user_id=1,
            idempotency_key="bind-passport-old",
        )
        old_transcript = self.documents.bind_document(
            cycle_id=self.overview.cycle_id,
            branch_id=self.branch.branch_id,
            document_id=402,
            source_cycle_id=180,
            document_type="Transcript",
            document_date="2025-11-30",
            actor_user_id=1,
            idempotency_key="bind-transcript-old",
        )
        self.documents.adopt_document(
            requirement_id=passport.requirement_id,
            binding_id=passport_binding.binding_id,
            actor_user_id=1,
            idempotency_key="adopt-passport-old",
        )
        self.documents.adopt_document(
            requirement_id=transcript.requirement_id,
            binding_id=old_transcript.binding_id,
            actor_user_id=1,
            idempotency_key="adopt-transcript-old",
        )
        checklist = self.documents.load_checklist(
            self.overview.cycle_id, self.branch.branch_id
        )
        self.assertEqual(
            {"Passport": True, "Latest transcript": False},
            {item.label: item.satisfied for item in checklist},
        )
        current_transcript = self.documents.bind_document(
            cycle_id=self.overview.cycle_id,
            branch_id=self.branch.branch_id,
            document_id=403,
            source_cycle_id=self.overview.cycle_id,
            document_type="Transcript",
            document_date="2026-08-20",
            actor_user_id=1,
            idempotency_key="bind-transcript-current",
        )
        self.documents.adopt_document(
            requirement_id=transcript.requirement_id,
            binding_id=current_transcript.binding_id,
            actor_user_id=1,
            idempotency_key="adopt-transcript-current",
        )
        self.assertTrue(self.documents.load_checklist(
            self.overview.cycle_id, self.branch.branch_id
        )[1].satisfied)

    def test_document_sections_reference_paths_without_copy_and_show_source_cycle(self):
        old_binding = self.documents.bind_document(
            cycle_id=self.overview.cycle_id,
            branch_id=self.branch.branch_id,
            document_id=401,
            source_cycle_id=180,
            document_type="Passport",
            document_date="2025-01-15",
            actor_user_id=1,
            idempotency_key="bind-existing",
        )
        current_binding = self.documents.bind_document(
            cycle_id=self.overview.cycle_id,
            branch_id=self.branch.branch_id,
            document_id=404,
            source_cycle_id=self.overview.cycle_id,
            document_type="Passport",
            document_date="2026-08-21",
            actor_user_id=1,
            idempotency_key="bind-current",
        )
        sections = self.documents.list_document_sections(
            self.overview.cycle_id, self.branch.branch_id
        )
        self.assertEqual([current_binding.binding_id], [item.binding_id for item in sections.current])
        self.assertEqual([old_binding.binding_id], [item.binding_id for item in sections.existing])
        self.assertEqual("Initial Cycle", sections.existing[0].source_cycle_label)
        self.assertEqual("Current Transfer #3", sections.current[0].source_cycle_label)
        self.assertEqual(
            ["/nas/S1/passport-2025.pdf", "/nas/S1/passport-2026.pdf"],
            [row["file_path"] for row in query_all(
                self.database_path,
                "SELECT file_path FROM student_documents WHERE id IN (401,404) ORDER BY id",
            )],
        )
        self.assertFalse(Path("/nas/S1/passport-2025.pdf").exists())

    def test_invalid_current_adoption_falls_back_to_previous_valid_binding(self):
        requirement = self.documents.add_requirement(
            cycle_id=self.overview.cycle_id,
            branch_id=self.branch.branch_id,
            document_type="Passport",
            label="Passport",
            requires_current_cycle=False,
            actor_user_id=1,
            idempotency_key="req-passport",
        )
        older = self.documents.bind_document(
            cycle_id=self.overview.cycle_id,
            branch_id=self.branch.branch_id,
            document_id=401,
            source_cycle_id=180,
            document_type="Passport",
            document_date="2025-01-15",
            actor_user_id=1,
            idempotency_key="bind-old",
        )
        newer = self.documents.bind_document(
            cycle_id=self.overview.cycle_id,
            branch_id=self.branch.branch_id,
            document_id=404,
            source_cycle_id=self.overview.cycle_id,
            document_type="Passport",
            document_date="2026-08-21",
            actor_user_id=1,
            idempotency_key="bind-new",
        )
        self.documents.adopt_document(
            requirement_id=requirement.requirement_id,
            binding_id=older.binding_id,
            actor_user_id=1,
            idempotency_key="adopt-old",
        )
        self.documents.adopt_document(
            requirement_id=requirement.requirement_id,
            binding_id=newer.binding_id,
            actor_user_id=1,
            idempotency_key="adopt-new",
        )
        fallback = self.documents.invalidate_binding(
            binding_id=newer.binding_id,
            actor_user_id=2,
            reason="Replaced passport is unreadable",
            idempotency_key="invalidate-new",
        )
        self.assertEqual(older.binding_id, fallback.binding_id)
        current = self.documents.current_adopted(requirement.requirement_id)
        self.assertEqual(older.binding_id, current.binding_id)
        adoption_actions = query_all(
            self.database_path,
            "SELECT action,binding_id FROM transfer_document_adoption_events WHERE requirement_id=? ORDER BY id",
            (requirement.requirement_id,),
        )
        self.assertEqual(
            [("adopted", older.binding_id), ("adopted", newer.binding_id), ("auto_fallback", older.binding_id)],
            [tuple(row) for row in adoption_actions],
        )


if __name__ == "__main__":
    unittest.main()
