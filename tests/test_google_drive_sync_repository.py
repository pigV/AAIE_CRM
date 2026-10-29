import shutil
import tempfile
import unittest
from pathlib import Path

from database import Database
from google_drive_sync_repository import (
    ConfirmedDriveContent,
    GoogleDriveSyncRepository,
    normalized_content_fingerprint,
)


class GoogleDriveSyncRepositoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.class_temp = tempfile.TemporaryDirectory(prefix="google-drive-repository-template-")
        cls.template_path = Path(cls.class_temp.name) / "template.db"
        Database(cls.template_path).initialize()

    @classmethod
    def tearDownClass(cls):
        cls.class_temp.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="google-drive-repository-case-")
        self.db_path = Path(self.temp.name) / "case.db"
        shutil.copy2(self.template_path, self.db_path)
        self.db = Database(self.db_path)
        self.repo = GoogleDriveSyncRepository(
            self.db,
            now_iso=lambda: "2026-08-10T04:00:00+10:00",
        )

    def tearDown(self):
        self.temp.cleanup()

    def seed_script(
        self,
        *,
        title="Existing title",
        body="Existing body",
        legacy_system_name=None,
        legacy_record_id=None,
        drive_file_id=None,
        version_fingerprint="version-1",
    ):
        with self.db.transaction() as conn:
            topic_id = conn.execute(
                """INSERT INTO content_topics(topic_name,status,created_at,updated_at)
                   VALUES(?,?,?,?)""",
                (title, "Active", "2026-08-01", "2026-08-01"),
            ).lastrowid
            script_id = conn.execute(
                """INSERT INTO content_scripts(
                       title,body,topic_id,status,version_number,
                       legacy_system_name,legacy_table_name,legacy_record_id,
                       created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    title,
                    body,
                    topic_id,
                    "Draft",
                    1,
                    legacy_system_name,
                    "drive_files" if legacy_system_name else None,
                    legacy_record_id,
                    "2026-08-01",
                    "2026-08-01",
                ),
            ).lastrowid
            material_id = None
            if drive_file_id:
                material_id = conn.execute(
                    """INSERT INTO content_materials(
                           script_id,material_type,title,body,source_path,source_url,file_hash,
                           created_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        script_id,
                        "Script Source",
                        title,
                        body,
                        f"gdrive://{drive_file_id}",
                        f"https://drive.invalid/{drive_file_id}",
                        version_fingerprint,
                        "2026-08-01",
                        "2026-08-01",
                    ),
                ).lastrowid
        return {"topic_id": topic_id, "script_id": script_id, "material_id": material_id}

    @staticmethod
    def confirmed(
        drive_file_id="drive-new",
        *,
        title="New title",
        body="New body",
        version_fingerprint="version-new",
    ):
        return ConfirmedDriveContent(
            drive_file_id=drive_file_id,
            name=f"{title}.txt",
            mime_type="text/plain",
            modified_time="2026-08-10T03:59:00Z",
            created_time="2026-05-01T01:02:03Z",
            version_fingerprint=version_fingerprint,
            content_fingerprint=normalized_content_fingerprint(title, body),
            title=title,
            body=body,
            source_url=f"https://drive.invalid/{drive_file_id}",
            content_date="2026-08-10",
        )

    def counts(self):
        return {
            table: self.db.one(f"SELECT COUNT(*) AS count FROM {table}")["count"]
            for table in (
                "content_topics",
                "content_scripts",
                "content_script_versions",
                "content_materials",
            )
        }

    def test_gdrive_material_same_version_is_current(self):
        seeded = self.seed_script(drive_file_id="drive-1", version_fingerprint="version-1")

        result = self.repo.resolve_identity(
            "drive-1",
            normalized_content_fingerprint("Existing title", "Existing body"),
            "version-1",
        )

        self.assertEqual(result.state, "current")
        self.assertEqual(result.script_id, seeded["script_id"])
        self.assertEqual(result.material_id, seeded["material_id"])

    def test_legacy_drive_id_only_resolves_existing_script_as_changed(self):
        seeded = self.seed_script(
            legacy_system_name="Google Drive",
            legacy_record_id="drive-legacy",
        )

        result = self.repo.resolve_identity(
            "drive-legacy",
            normalized_content_fingerprint("Existing title", "Existing body"),
            "version-2",
        )

        self.assertEqual(result.state, "changed")
        self.assertEqual(result.script_id, seeded["script_id"])
        self.assertEqual(result.reason_code, "legacy_identity_only")

    def test_legacy_and_material_same_script_is_unambiguous(self):
        seeded = self.seed_script(
            legacy_system_name="Google Drive",
            legacy_record_id="drive-shared",
            drive_file_id="drive-shared",
            version_fingerprint="version-1",
        )

        result = self.repo.resolve_identity(
            "drive-shared",
            normalized_content_fingerprint("Existing title", "Existing body"),
            "version-2",
        )

        self.assertEqual(result.state, "changed")
        self.assertEqual(result.matching_script_ids, (seeded["script_id"],))

    def test_legacy_and_material_different_scripts_is_conflict(self):
        legacy = self.seed_script(
            title="Legacy copy",
            legacy_system_name="Google Drive",
            legacy_record_id="drive-conflict",
        )
        material = self.seed_script(title="Material copy", drive_file_id="drive-conflict")

        result = self.repo.resolve_identity(
            "drive-conflict",
            normalized_content_fingerprint("Incoming", "Incoming body"),
            "incoming-version",
        )

        self.assertEqual(result.state, "conflict")
        self.assertEqual(
            result.matching_script_ids,
            tuple(sorted((legacy["script_id"], material["script_id"]))),
        )
        self.assertEqual(result.reason_code, "drive_id_maps_to_multiple_scripts")

    def test_same_content_on_different_drive_id_is_content_duplicate(self):
        seeded = self.seed_script(
            title="Same title",
            body="Same body",
            drive_file_id="drive-original",
        )

        result = self.repo.resolve_identity(
            "drive-copy",
            normalized_content_fingerprint("Same title", "Same body"),
            "copy-version",
        )

        self.assertEqual(result.state, "content_duplicate")
        self.assertEqual(result.matching_script_ids, (seeded["script_id"],))

    def test_null_legacy_record_ids_are_not_grouped_as_one_identity(self):
        self.seed_script(legacy_system_name="Google Drive", legacy_record_id=None)
        self.seed_script(legacy_system_name="Google Drive", legacy_record_id=None)

        result = self.repo.resolve_identity(
            "drive-unseen",
            normalized_content_fingerprint("Unseen", "Different"),
            "new-version",
        )

        self.assertEqual(result.state, "new")
        self.assertEqual(result.matching_script_ids, ())

    def test_new_write_creates_one_topic_script_version_and_material(self):
        before = self.counts()

        outcome = self.repo.write_confirmed(self.confirmed(), actor_user_id=7)

        after = self.counts()
        self.assertEqual(outcome.status, "created")
        for table in after:
            self.assertEqual(after[table], before[table] + 1, table)
        material = self.db.one(
            "SELECT source_path,file_hash,script_id FROM content_materials WHERE script_id=?",
            (outcome.script_id,),
        )
        self.assertEqual(material["source_path"], "gdrive://drive-new")
        self.assertEqual(material["file_hash"], "version-new")

    def test_new_write_persists_canonical_drive_modified_time(self):
        columns = {row["name"] for row in self.db.all("PRAGMA table_info(content_materials)")}
        self.assertIn("source_modified_at", columns)

        outcome = self.repo.write_confirmed(self.confirmed(), actor_user_id=7)

        material = self.db.one(
            "SELECT source_modified_at,updated_at FROM content_materials WHERE script_id=?",
            (outcome.script_id,),
        )
        self.assertEqual(material["source_modified_at"], "2026-08-10T03:59:00.000+00:00")
        self.assertEqual(material["updated_at"], "2026-08-10T04:00:00+10:00")

    def test_late_first_sync_uses_drive_created_time_not_ingestion_time(self):
        outcome = self.repo.write_confirmed(self.confirmed(), actor_user_id=7)

        script = self.db.one(
            "SELECT created_at,updated_at FROM content_scripts WHERE id=?",
            (outcome.script_id,),
        )
        material = self.db.one(
            "SELECT created_at,updated_at FROM content_materials WHERE script_id=?",
            (outcome.script_id,),
        )

        self.assertEqual(script["created_at"], "2026-05-01T01:02:03.000+00:00")
        self.assertEqual(material["created_at"], "2026-05-01T01:02:03.000+00:00")
        self.assertEqual(script["updated_at"], "2026-08-10T04:00:00+10:00")
        self.assertEqual(material["updated_at"], "2026-08-10T04:00:00+10:00")

    def test_changed_write_updates_source_modified_time_but_same_version_does_not(self):
        seeded = self.seed_script(drive_file_id="drive-current", version_fingerprint="version-1")
        self.db.execute(
            "UPDATE content_materials SET source_modified_at=? WHERE id=?",
            ("2026-08-01T00:00:00.000+00:00", seeded["material_id"]),
        )

        updated = self.repo.write_confirmed(
            self.confirmed("drive-current", version_fingerprint="version-2"),
            actor_user_id=7,
        )
        after_update = self.db.one(
            "SELECT source_modified_at FROM content_materials WHERE id=?",
            (seeded["material_id"],),
        )["source_modified_at"]
        skipped = self.repo.write_confirmed(
            self.confirmed("drive-current", version_fingerprint="version-2"),
            actor_user_id=7,
        )
        after_skip = self.db.one(
            "SELECT source_modified_at FROM content_materials WHERE id=?",
            (seeded["material_id"],),
        )["source_modified_at"]

        self.assertEqual(updated.status, "updated")
        self.assertEqual(after_update, "2026-08-10T03:59:00.000+00:00")
        self.assertEqual(skipped.status, "skipped_duplicate")
        self.assertEqual(after_skip, after_update)

    def test_legacy_only_write_updates_existing_script_without_duplicate(self):
        seeded = self.seed_script(
            legacy_system_name="Google Drive",
            legacy_record_id="drive-legacy",
        )
        before = self.counts()

        outcome = self.repo.write_confirmed(
            self.confirmed("drive-legacy", title="Updated", body="Updated body"),
            actor_user_id=7,
        )

        after = self.counts()
        self.assertEqual(outcome.status, "updated")
        self.assertEqual(outcome.script_id, seeded["script_id"])
        self.assertEqual(after["content_scripts"], before["content_scripts"])
        self.assertEqual(after["content_topics"], before["content_topics"])
        self.assertEqual(after["content_script_versions"], before["content_script_versions"] + 1)
        self.assertEqual(after["content_materials"], before["content_materials"] + 1)

    def test_same_version_write_is_skipped_without_changes(self):
        self.seed_script(drive_file_id="drive-current", version_fingerprint="version-current")
        before = self.counts()

        outcome = self.repo.write_confirmed(
            self.confirmed("drive-current", version_fingerprint="version-current"),
            actor_user_id=7,
        )

        self.assertEqual(outcome.status, "skipped_duplicate")
        self.assertEqual(self.counts(), before)

    def test_conflict_write_requires_manual_review_without_changes(self):
        self.seed_script(
            title="Legacy copy",
            legacy_system_name="Google Drive",
            legacy_record_id="drive-conflict",
        )
        self.seed_script(title="Material copy", drive_file_id="drive-conflict")
        before = self.counts()

        outcome = self.repo.write_confirmed(
            self.confirmed("drive-conflict"),
            actor_user_id=7,
        )

        self.assertEqual(outcome.status, "manual_review")
        self.assertEqual(self.counts(), before)

    def test_material_insert_failure_rolls_back_entire_new_item(self):
        self.db.execute(
            """CREATE TRIGGER fail_google_drive_material
               BEFORE INSERT ON content_materials
               BEGIN SELECT RAISE(FAIL,'synthetic material failure'); END"""
        )
        before = self.counts()

        with self.assertRaisesRegex(Exception, "synthetic material failure"):
            self.repo.write_confirmed(self.confirmed(), actor_user_id=7)

        self.assertEqual(self.counts(), before)


if __name__ == "__main__":
    unittest.main()
