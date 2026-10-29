import hashlib
import json
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

import contract_template_editor as editor
import migration_runner
from tools import run_migrations


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_025 = ROOT / "migrations" / "20260818_025_contract_template_editor_v1.sql"
MIGRATION_029 = ROOT / "migrations" / "20260824_029_contract_template_superseded_status.sql"
FORWARD_029 = (
    ROOT
    / "migrations"
    / "checks"
    / "20260824_029_contract_template_superseded_status_forward_check.sql"
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ContractTemplateSupersededMigration029Tests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(MIGRATION_029.is_file(), "migration 029 package is missing")
        self.assertTrue(FORWARD_029.is_file(), "migration 029 forward check is missing")
        runtime_root = ROOT / "SAFEBOX" / "contract-template-permission-editor-audit-20260824" / "test-runtime"
        runtime_root.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=runtime_root)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.database = self.root / "editor.sqlite3"
        self.migrations = self.root / "migrations"
        (self.migrations / "checks").mkdir(parents=True)
        shutil.copy2(MIGRATION_029, self.migrations / MIGRATION_029.name)
        shutil.copy2(FORWARD_029, self.migrations / "checks" / FORWARD_029.name)
        self._create_fixture()
        self.backup_metadata = self._create_backup_metadata()

    def _create_fixture(self):
        snapshot = json.dumps(
            {
                "template_type": "TransferApplicationContract",
                "blocks": [],
                "services": [],
                "commercial": {},
            },
            sort_keys=True,
        )
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.executescript(MIGRATION_025.read_text(encoding="utf-8"))
            connection.execute(
                """INSERT INTO contract_template_identities(
                       id,template_type,display_name,current_published_version_id,
                       created_at,updated_at
                   ) VALUES(1,'TransferApplicationContract','Transfer',NULL,'now','now')"""
            )
            for version_id, version_number, source_version_id in (
                (1, 1, None),
                (2, 2, 1),
                (3, 3, 2),
            ):
                connection.execute(
                    """INSERT INTO contract_template_versions(
                           id,identity_id,version_number,status,source_version_id,
                           revision,snapshot_json,content_hash,immutable,created_by,
                           created_at,published_by,published_at
                       ) VALUES(?,1,?,'Draft',?,1,?,'hash',0,7,'now',NULL,NULL)""",
                    (version_id, version_number, source_version_id, snapshot),
                )
            for version_id in (1, 2):
                connection.execute(
                    """UPDATE contract_template_versions
                       SET status='Published',immutable=1,published_by=7,published_at='now'
                       WHERE id=?""",
                    (version_id,),
                )
            connection.execute(
                "UPDATE contract_template_identities SET current_published_version_id=2 WHERE id=1"
            )
            connection.execute(
                "UPDATE contract_template_versions SET status='Historical' WHERE id=1"
            )
            connection.execute(
                """INSERT INTO contract_template_clause_blocks(
                       id,version_id,block_key,title,body,sort_order,enabled,
                       fixed_layout,required,legal_kind,required_variables_json
                   ) VALUES(1,3,'scope','Scope','Body',1,1,0,0,NULL,'[]')"""
            )
            connection.execute(
                """INSERT INTO contract_template_service_items(
                       id,version_id,service_id,label,sort_order,enabled,default_selected
                   ) VALUES(1,3,'consulting','Consulting',1,1,1)"""
            )
            connection.commit()

    def _create_backup_metadata(self):
        backup = self.root / "before-029.sqlite3"
        with closing(sqlite3.connect(self.database)) as source:
            with closing(sqlite3.connect(backup)) as target:
                source.backup(target)
        manifest = self.root / "before-029.manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "database_path": str(self.database.resolve()),
                    "backup_path": str(backup.resolve()),
                    "backup_method": "sqlite_online_backup",
                    "created_at": "2026-08-24T00:00:00+10:00",
                    "source_database_sha256": sha256(self.database),
                    "backup_sha256": sha256(backup),
                    "quick_check": "ok",
                    "integrity_check": "ok",
                }
            ),
            encoding="utf-8",
        )
        return {"backup_path": str(backup), "manifest_path": str(manifest)}

    def _apply(self):
        return migration_runner.apply_migrations(
            self.database,
            self.migrations,
            applied_by="superseded-migration-test",
            source_commit="test",
            formal_database_path=None,
            required_backup_metadata=self.backup_metadata,
        )

    def test_migration_is_a_guarded_rebuild_and_reapply_is_noop(self):
        discovered = migration_runner.discover_migrations(self.migrations)
        self.assertEqual(len(discovered), 1)
        self.assertEqual(discovered[0].version, "20260824_029")
        self.assertEqual(discovered[0].mode, migration_runner.SQLITE_FK_OFF_REBUILD_MODE)

        first = self._apply()
        second = self._apply()
        verified = migration_runner.verify_migrations(self.database, self.migrations)

        self.assertEqual(first["applied"], ["20260824_029"])
        self.assertEqual(second["applied"], [])
        self.assertEqual(second["already_applied"], ["20260824_029"])
        self.assertEqual(verified["verified"], ["20260824_029"])
        self.assertEqual(verified["foreign_key_violations"], [])
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            self.assertEqual(
                connection.execute(
                    "SELECT source_version_id FROM contract_template_versions WHERE id=3"
                ).fetchone()[0],
                2,
            )
            foreign_keys = {
                (row[2], row[3], row[4], row[5], row[6])
                for row in connection.execute(
                    "PRAGMA foreign_key_list(contract_template_versions)"
                )
            }
            self.assertEqual(
                foreign_keys,
                {
                    (
                        "contract_template_versions",
                        "source_version_id",
                        "id",
                        "NO ACTION",
                        "RESTRICT",
                    ),
                    (
                        "contract_template_identities",
                        "identity_id",
                        "id",
                        "NO ACTION",
                        "RESTRICT",
                    ),
                },
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO contract_template_versions(
                           identity_id,version_number,status,source_version_id,
                           revision,snapshot_json,content_hash,immutable,
                           created_by,created_at
                       ) VALUES(1,4,'Draft',999,1,'{}','hash',0,7,'now')"""
                )

    def test_formal_cli_accepts_and_validates_rebuild_backup_metadata(self):
        output = StringIO()
        with redirect_stdout(output):
            exit_code = run_migrations.main(
                [
                    "apply",
                    "--database",
                    str(self.database),
                    "--migrations-dir",
                    str(self.migrations),
                    "--applied-by",
                    "superseded-cli-test",
                    "--source-commit",
                    "test",
                    "--rebuild-backup",
                    self.backup_metadata["backup_path"],
                    "--rebuild-backup-manifest",
                    self.backup_metadata["manifest_path"],
                    "--json",
                ]
            )
        self.assertEqual(exit_code, 0, output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["applied"], ["20260824_029"])

    def test_superseded_is_terminal_mutable_draft_provenance_only(self):
        self._apply()
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """UPDATE contract_template_versions
                       SET status='Superseded',snapshot_json='{}',revision=99
                       WHERE id=3"""
                )
            self.assertEqual(
                connection.execute(
                    "SELECT status,revision FROM contract_template_versions WHERE id=3"
                ).fetchone(),
                ("Draft", 1),
            )
            connection.execute(
                "UPDATE contract_template_versions SET status='Superseded' WHERE id=3"
            )
            self.assertEqual(
                connection.execute(
                    "SELECT status,immutable,published_by,published_at FROM contract_template_versions WHERE id=3"
                ).fetchone(),
                ("Superseded", 0, None, None),
            )
            blocked_updates = (
                "UPDATE contract_template_versions SET status='Draft' WHERE id=3",
                "UPDATE contract_template_versions SET snapshot_json='{}' WHERE id=3",
                "UPDATE contract_template_versions SET status='Superseded' WHERE id=2",
                "UPDATE contract_template_versions SET status='Superseded',immutable=0,published_by=NULL,published_at=NULL WHERE id=1",
            )
            for statement in blocked_updates:
                with self.subTest(statement=statement):
                    with self.assertRaises(sqlite3.IntegrityError):
                        connection.execute(statement)
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO contract_template_versions(
                           identity_id,version_number,status,revision,snapshot_json,
                           content_hash,immutable,created_by,created_at
                       ) VALUES(1,4,'Superseded',1,'{}','hash',0,7,'now')"""
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE contract_template_clause_blocks SET body='changed' WHERE version_id=3"
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "DELETE FROM contract_template_service_items WHERE version_id=3"
                )
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")

    def test_active_workspace_excludes_superseded_but_history_includes_it(self):
        self._apply()
        with closing(sqlite3.connect(self.database)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(
                "UPDATE contract_template_versions SET status='Superseded' WHERE id=3"
            )
            connection.commit()
            service = editor.ContractTemplateEditorService(connection)
            with self.assertRaises(editor.ImmutableVersionError):
                service.acquire_lock(
                    "TransferApplicationContract", 3, owner_user_id=7
                )
            before = service.workspace("TransferApplicationContract")
            self.assertEqual(before["active_version"]["id"], 2)
            self.assertIn(
                3,
                [row["id"] for row in before["versions"] if row["status"] == "Superseded"],
            )
            historical = service.workspace(
                "TransferApplicationContract", version_id=3
            )
            detail = editor.render_template_editor_detail(
                historical,
                csrf="test",
                can_edit=True,
                can_publish=True,
            )
            self.assertIn('data-editable="false"', detail)
            self.assertIn('data-template-action="publish" disabled', detail)
            self.assertIn("当前模板只读", detail)

            created = service.create_draft("TransferApplicationContract", actor_user_id=7)
            again = service.create_draft("TransferApplicationContract", actor_user_id=7)
            after = service.workspace("TransferApplicationContract")
            self.assertEqual(created["id"], again["id"])
            self.assertEqual(after["active_version"]["id"], created["id"])
            self.assertEqual(created["source_version_id"], 2)
            self.assertEqual(created["status"], "Draft")


if __name__ == "__main__":
    unittest.main()
