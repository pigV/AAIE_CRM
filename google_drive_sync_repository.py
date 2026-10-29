from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from typing import Callable

from google_drive_source_metadata import normalize_source_created_time, normalize_source_modified_time


def normalized_content_fingerprint(title: str, body: str) -> str:
    normalized = "\n".join(
        line.strip()
        for line in f"{title or ''}\n{body or ''}".splitlines()
        if line.strip()
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class IdentityResolution:
    state: str
    script_id: int | None
    topic_id: int | None
    material_id: int | None
    matching_script_ids: tuple[int, ...]
    reason_code: str
    stored_version_fingerprint: str


@dataclass(frozen=True)
class ConfirmedDriveContent:
    drive_file_id: str
    name: str
    mime_type: str
    modified_time: str
    created_time: str
    version_fingerprint: str
    content_fingerprint: str
    title: str
    body: str
    source_url: str
    content_date: str


@dataclass(frozen=True)
class WriteOutcome:
    status: str
    script_id: int | None
    reason_code: str


class GoogleDriveSyncRepository:
    def __init__(self, db, *, now_iso: Callable[[], str]):
        self.db = db
        self.now_iso = now_iso

    @staticmethod
    def _identity_rows(conn, drive_file_id: str):
        source_path = f"gdrive://{drive_file_id}"
        material_rows = conn.execute(
            """SELECT m.id AS material_id,m.script_id,m.file_hash,
                      s.topic_id,s.title,s.body
               FROM content_materials m
               LEFT JOIN content_scripts s ON s.id=m.script_id
               WHERE m.source_path=?
               ORDER BY m.id DESC""",
            (source_path,),
        ).fetchall()
        legacy_rows = conn.execute(
            """SELECT NULL AS material_id,s.id AS script_id,NULL AS file_hash,
                      s.topic_id,s.title,s.body
               FROM content_scripts s
               WHERE s.legacy_system_name='Google Drive'
                 AND s.legacy_record_id IS NOT NULL
                 AND CAST(s.legacy_record_id AS TEXT)=?
               ORDER BY s.id""",
            (str(drive_file_id),),
        ).fetchall()
        return material_rows, legacy_rows

    @staticmethod
    def _content_duplicate_ids(conn, content_fingerprint: str) -> tuple[int, ...]:
        if not content_fingerprint:
            return ()
        matches = []
        for row in conn.execute("SELECT id,title,body FROM content_scripts ORDER BY id"):
            if normalized_content_fingerprint(row["title"], row["body"] or "") == content_fingerprint:
                matches.append(int(row["id"]))
        return tuple(matches)

    def _resolve_identity(
        self,
        conn,
        drive_file_id: str,
        content_fingerprint: str,
        version_fingerprint: str,
    ) -> IdentityResolution:
        material_rows, legacy_rows = self._identity_rows(conn, drive_file_id)
        matching_ids = tuple(
            sorted(
                {
                    int(row["script_id"])
                    for row in [*material_rows, *legacy_rows]
                    if row["script_id"] is not None
                }
            )
        )
        if len(matching_ids) > 1:
            return IdentityResolution(
                "conflict",
                None,
                None,
                None,
                matching_ids,
                "drive_id_maps_to_multiple_scripts",
                "",
            )
        if len(matching_ids) == 1:
            script_id = matching_ids[0]
            material = next(
                (row for row in material_rows if row["script_id"] == script_id),
                None,
            )
            representative = material or next(
                row for row in legacy_rows if row["script_id"] == script_id
            )
            stored = str(material["file_hash"] or "") if material else ""
            if material and stored and stored == str(version_fingerprint or ""):
                state = "current"
                reason = "same_drive_version"
            else:
                state = "changed"
                reason = "version_changed" if material else "legacy_identity_only"
            return IdentityResolution(
                state,
                script_id,
                int(representative["topic_id"]) if representative["topic_id"] is not None else None,
                int(material["material_id"]) if material else None,
                matching_ids,
                reason,
                stored,
            )

        content_matches = self._content_duplicate_ids(conn, content_fingerprint)
        if content_matches:
            return IdentityResolution(
                "content_duplicate",
                None,
                None,
                None,
                content_matches,
                "same_content_different_drive_id",
                "",
            )
        return IdentityResolution("new", None, None, None, (), "unseen_drive_file", "")

    def resolve_identity(
        self,
        drive_file_id: str,
        content_fingerprint: str,
        version_fingerprint: str = "",
    ) -> IdentityResolution:
        conn = self.db.connect()
        try:
            return self._resolve_identity(
                conn,
                str(drive_file_id),
                str(content_fingerprint or ""),
                str(version_fingerprint or ""),
            )
        finally:
            self.db._release_connection(conn)

    @staticmethod
    def _date_parts(content_date: str):
        try:
            parsed = date.fromisoformat(str(content_date or ""))
        except ValueError:
            return None, None, None
        return parsed.year, parsed.month, parsed.isocalendar().week

    def write_confirmed(
        self,
        item: ConfirmedDriveContent,
        actor_user_id: int | None,
    ) -> WriteOutcome:
        stamp = self.now_iso()
        source_modified_at = normalize_source_modified_time(item.modified_time)
        source_created_at = normalize_source_created_time(item.created_time)
        year, month, week = self._date_parts(item.content_date)
        tags = json.dumps(["同步", "google_drive_video"], ensure_ascii=False)
        with self.db.transaction() as conn:
            identity = self._resolve_identity(
                conn,
                item.drive_file_id,
                item.content_fingerprint,
                item.version_fingerprint,
            )
            if identity.state in {"conflict", "content_duplicate"}:
                return WriteOutcome("manual_review", None, identity.reason_code)
            if identity.state == "current":
                return WriteOutcome("skipped_duplicate", identity.script_id, identity.reason_code)

            if identity.state == "changed" and identity.script_id is not None:
                script = conn.execute(
                    "SELECT * FROM content_scripts WHERE id=?",
                    (identity.script_id,),
                ).fetchone()
                if script is None:
                    return WriteOutcome("manual_review", None, "matched_script_missing")
                new_version = int(script["version_number"] or 1) + 1
                conn.execute(
                    """INSERT INTO content_script_versions(
                           script_id,title,body,version_number,version_note,
                           legacy_system_name,legacy_table_name,legacy_record_id,
                           imported_at,imported_by,import_batch_id,potential_duplicate,
                           migration_notes,created_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        script["id"],
                        item.title,
                        item.body,
                        new_version,
                        "Confirmed Google Drive source update",
                        script["legacy_system_name"],
                        script["legacy_table_name"],
                        script["legacy_record_id"],
                        stamp,
                        actor_user_id,
                        "google_drive_confirmed",
                        0,
                        f"Drive file {item.drive_file_id}",
                        stamp,
                    ),
                )
                conn.execute(
                    """UPDATE content_scripts
                       SET title=?,body=?,category='Google Drive 视频',version_number=?,
                           source_type='google_drive_video',content_year=?,content_month=?,
                           content_week=?,tags_json=?,updated_at=?
                       WHERE id=?""",
                    (
                        item.title,
                        item.body,
                        new_version,
                        year,
                        month,
                        week,
                        tags,
                        stamp,
                        script["id"],
                    ),
                )
                if script["topic_id"] is not None:
                    conn.execute(
                        """UPDATE content_topics
                           SET topic_name=?,category='Google Drive 视频',content_year=?,
                               source_type='google_drive_video',source_reference=?,
                               tags_json=?,updated_at=?
                           WHERE id=?""",
                        (
                            item.title,
                            year,
                            f"gdrive://{item.drive_file_id}",
                            tags,
                            stamp,
                            script["topic_id"],
                        ),
                    )
                if identity.material_id is not None:
                    conn.execute(
                        """UPDATE content_materials
                           SET title=?,body=?,category='Google Drive 视频',source_url=?,
                               file_hash=?,source_modified_at=?,updated_at=?
                           WHERE id=?""",
                        (
                            item.title,
                            item.body,
                            item.source_url,
                            item.version_fingerprint,
                            source_modified_at,
                            stamp,
                            identity.material_id,
                        ),
                    )
                else:
                    conn.execute(
                        """INSERT INTO content_materials(
                               script_id,material_type,title,body,category,source_path,
                               source_url,file_hash,legacy_system_name,legacy_table_name,
                               legacy_record_id,imported_at,imported_by,import_batch_id,
                               potential_duplicate,migration_notes,created_at,updated_at,
                               source_modified_at
                           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            script["id"],
                            "Script Source",
                            item.title,
                            item.body,
                            "Google Drive 视频",
                            f"gdrive://{item.drive_file_id}",
                            item.source_url,
                            item.version_fingerprint,
                            "Google Drive",
                            "drive_files",
                            item.drive_file_id,
                            stamp,
                            actor_user_id,
                            "google_drive_confirmed",
                            0,
                            "Canonical Drive material attached to existing script.",
                            source_created_at,
                            stamp,
                            source_modified_at,
                        ),
                    )
                return WriteOutcome("updated", int(script["id"]), identity.reason_code)

            topic_id = conn.execute(
                """INSERT INTO content_topics(
                       topic_name,category,status,content_year,source,source_type,
                       source_reference,tags_json,legacy_system_name,legacy_table_name,
                       legacy_record_id,imported_at,imported_by,import_batch_id,
                       potential_duplicate,migration_notes,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    item.title,
                    "Google Drive 视频",
                    "Active",
                    year,
                    "Google Drive",
                    "google_drive_video",
                    f"gdrive://{item.drive_file_id}",
                    tags,
                    "Google Drive",
                    "drive_files",
                    item.drive_file_id,
                    stamp,
                    actor_user_id,
                    "google_drive_confirmed",
                    0,
                    "",
                    stamp,
                    stamp,
                ),
            ).lastrowid
            script_id = conn.execute(
                """INSERT INTO content_scripts(
                       title,body,category,topic_id,status,review_status,version_number,
                       source_type,content_year,content_month,content_week,tags_json,
                       legacy_system_name,legacy_table_name,legacy_record_id,
                       imported_at,imported_by,import_batch_id,potential_duplicate,
                       migration_notes,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    item.title,
                    item.body,
                    "Google Drive 视频",
                    topic_id,
                    "Draft",
                    "Not Reviewed",
                    1,
                    "google_drive_video",
                    year,
                    month,
                    week,
                    tags,
                    "Google Drive",
                    "drive_files",
                    item.drive_file_id,
                    stamp,
                    actor_user_id,
                    "google_drive_confirmed",
                    0,
                    "",
                    source_created_at,
                    stamp,
                ),
            ).lastrowid
            conn.execute(
                """INSERT INTO content_script_versions(
                       script_id,title,body,version_number,version_note,
                       legacy_system_name,legacy_table_name,legacy_record_id,
                       imported_at,imported_by,import_batch_id,potential_duplicate,
                       migration_notes,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    script_id,
                    item.title,
                    item.body,
                    1,
                    "Initial confirmed Google Drive import",
                    "Google Drive",
                    "drive_files",
                    item.drive_file_id,
                    stamp,
                    actor_user_id,
                    "google_drive_confirmed",
                    0,
                    "",
                    stamp,
                ),
            )
            conn.execute(
                """INSERT INTO content_materials(
                       script_id,material_type,title,body,category,source_path,source_url,
                       file_hash,legacy_system_name,legacy_table_name,legacy_record_id,
                       imported_at,imported_by,import_batch_id,potential_duplicate,
                       migration_notes,created_at,updated_at,source_modified_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    script_id,
                    "Script Source",
                    item.title,
                    item.body,
                    "Google Drive 视频",
                    f"gdrive://{item.drive_file_id}",
                    item.source_url,
                    item.version_fingerprint,
                    "Google Drive",
                    "drive_files",
                    item.drive_file_id,
                    stamp,
                    actor_user_id,
                    "google_drive_confirmed",
                    0,
                    "",
                    source_created_at,
                    stamp,
                    source_modified_at,
                ),
            )
            return WriteOutcome("created", int(script_id), "unseen_drive_file")
