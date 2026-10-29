"""Persistence boundary for secure parent-form access and draft state."""

from dataclasses import dataclass
import sqlite3

from application_cycle import record_application_lifecycle_event
from workflow_cutover import is_workflow_v2_active


ACCESS_EVENT_TYPES = frozenset({"opened", "authenticated"})


class BindingIdentityConflict(Exception):
    """The link is already bound to another WeChat identity."""


class DraftRevisionConflict(Exception):
    """The submitted draft revision is older than the stored revision."""

    def __init__(self, current_revision, last_saved_at=None):
        super().__init__("parent form draft revision conflict")
        self.current_revision = int(current_revision or 0)
        self.last_saved_at = last_saved_at


class LinkRegenerationConflict(Exception):
    def __init__(self, reason):
        super().__init__(f"parent form link regeneration conflict: {reason}")
        self.reason = reason


@dataclass(frozen=True)
class DraftRecord:
    token: str
    payload_encrypted: str
    revision: int
    save_source: str
    last_saved_at: str


class ParentFormRepository:
    def __init__(self, database):
        self.database = database

    def get_link(self, token):
        return self.database.one(
            "SELECT * FROM parent_form_tokens WHERE token=?",
            (token,),
        )

    @staticmethod
    def _has_intake_type(conn):
        return any(
            row[1] == "intake_type"
            for row in conn.execute("PRAGMA table_info(parent_form_tokens)")
        )

    def create_link(
        self,
        token,
        consultant_id,
        student_hint,
        expires_at,
        created_by,
        created_at,
        student_id=None,
        intake_type=None,
    ):
        with self.database.transaction() as conn:
            if self._has_intake_type(conn):
                conn.execute(
                    """INSERT INTO parent_form_tokens(
                         token,consultant_id,student_hint,expires_at,student_id,
                         created_by,created_at,intake_type
                       ) VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        token,
                        consultant_id,
                        student_hint,
                        expires_at,
                        student_id,
                        created_by,
                        created_at,
                        intake_type,
                    ),
                )
            else:
                conn.execute(
                    """INSERT INTO parent_form_tokens(
                         token,consultant_id,student_hint,expires_at,student_id,created_by,created_at
                       ) VALUES(?,?,?,?,?,?,?)""",
                    (
                        token,
                        consultant_id,
                        student_hint,
                        expires_at,
                        student_id,
                        created_by,
                        created_at,
                    ),
                )
            if is_workflow_v2_active(conn):
                record_application_lifecycle_event(
                    conn,
                    "PARENT_FORM_CREATED",
                    occurred_at=created_at,
                    actor_user_id=created_by,
                    student_id=student_id,
                    parent_form_token=token,
                )
        return self.get_link(token)

    def invalidate_if_writable(self, token, stamp):
        with self.database.transaction() as conn:
            cursor = conn.execute(
                """UPDATE parent_form_tokens
                   SET invalidated_at=?
                   WHERE token=?
                     AND submitted_at IS NULL
                     AND invalidated_at IS NULL""",
                (stamp, token),
            )
            return cursor.rowcount == 1

    def regenerate_link(
        self,
        old_token,
        new_token,
        expires_at,
        created_by,
        stamp,
        intake_type=None,
    ):
        with self.database.transaction() as conn:
            old_link = conn.execute(
                "SELECT * FROM parent_form_tokens WHERE token=?",
                (old_token,),
            ).fetchone()
            if not old_link:
                raise LinkRegenerationConflict("missing")
            if old_link["submitted_at"]:
                raise LinkRegenerationConflict("submitted")
            if not old_link["invalidated_at"]:
                cursor = conn.execute(
                    """UPDATE parent_form_tokens
                       SET invalidated_at=?
                       WHERE token=?
                         AND submitted_at IS NULL
                         AND invalidated_at IS NULL""",
                    (stamp, old_token),
                )
                if cursor.rowcount != 1:
                    raise LinkRegenerationConflict("changed")
            if self._has_intake_type(conn):
                conn.execute(
                    """INSERT INTO parent_form_tokens(
                         token,consultant_id,student_hint,expires_at,student_id,
                         created_by,created_at,intake_type
                       ) VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        new_token,
                        old_link["consultant_id"],
                        old_link["student_hint"],
                        expires_at,
                        old_link["student_id"],
                        created_by,
                        stamp,
                        intake_type,
                    ),
                )
            else:
                conn.execute(
                    """INSERT INTO parent_form_tokens(
                         token,consultant_id,student_hint,expires_at,student_id,created_by,created_at
                       ) VALUES(?,?,?,?,?,?,?)""",
                    (
                        new_token,
                        old_link["consultant_id"],
                        old_link["student_hint"],
                        expires_at,
                        old_link["student_id"],
                        created_by,
                        stamp,
                    ),
                )
            if is_workflow_v2_active(conn):
                record_application_lifecycle_event(
                    conn,
                    "PARENT_FORM_CREATED",
                    occurred_at=stamp,
                    actor_user_id=created_by,
                    student_id=old_link["student_id"],
                    parent_form_token=new_token,
                )
            return conn.execute(
                "SELECT * FROM parent_form_tokens WHERE token=?",
                (new_token,),
            ).fetchone()

    def correct_intake_type(
        self,
        token,
        previous_type,
        new_type,
        changed_by,
        changed_at,
    ):
        with self.database.transaction() as conn:
            conn.execute(
                """INSERT INTO parent_form_intake_type_change_events(
                     parent_form_token,previous_intake_type,new_intake_type,
                     changed_by,changed_at
                   ) VALUES(?,?,?,?,?)""",
                (token, previous_type, new_type, changed_by, changed_at),
            )
            return conn.execute(
                "SELECT * FROM parent_form_tokens WHERE token=?",
                (token,),
            ).fetchone()

    def get_binding(self, token):
        return self.database.one(
            "SELECT * FROM parent_form_access_bindings WHERE token=?",
            (token,),
        )

    @staticmethod
    def _insert_access_event(conn, token, event_type, stamp):
        if event_type not in ACCESS_EVENT_TYPES:
            raise ValueError("unsupported parent form access event")
        conn.execute(
            """INSERT INTO parent_form_access_events(
                 token,event_type,occurred_at,created_at
               ) VALUES(?,?,?,?)""",
            (token, event_type, stamp, stamp),
        )

    def record_access_event(self, token, event_type, stamp):
        with self.database.transaction() as conn:
            self._insert_access_event(conn, token, event_type, stamp)
        return True

    def list_access_events(self, token):
        return self.database.all(
            """SELECT id,token,event_type,occurred_at,created_at
               FROM parent_form_access_events
               WHERE token=?
               ORDER BY occurred_at DESC,id DESC""",
            (token,),
        )

    def access_event_summaries(self, tokens):
        unique_tokens = list(dict.fromkeys(str(token) for token in tokens if token))
        if not unique_tokens:
            return {}
        summaries = {
            token: {
                "opened_count": 0,
                "authenticated_count": 0,
                "last_opened_at": None,
                "last_authenticated_at": None,
            }
            for token in unique_tokens
        }
        placeholders = ",".join("?" for _ in unique_tokens)
        rows = self.database.all(
            f"""SELECT token,
                       SUM(CASE WHEN event_type='opened' THEN 1 ELSE 0 END)
                         AS opened_count,
                       SUM(CASE WHEN event_type='authenticated' THEN 1 ELSE 0 END)
                         AS authenticated_count,
                       MAX(CASE WHEN event_type='opened' THEN occurred_at END)
                         AS last_opened_at,
                       MAX(CASE WHEN event_type='authenticated' THEN occurred_at END)
                         AS last_authenticated_at
                FROM parent_form_access_events
                WHERE token IN ({placeholders})
                GROUP BY token""",
            tuple(unique_tokens),
        )
        for row in rows:
            summaries[row["token"]] = {
                "opened_count": int(row["opened_count"] or 0),
                "authenticated_count": int(row["authenticated_count"] or 0),
                "last_opened_at": row["last_opened_at"],
                "last_authenticated_at": row["last_authenticated_at"],
            }
        return summaries

    def bind_identity(self, token, identity, stamp):
        openid = str(identity.get("openid") or "").strip()
        if not openid:
            raise ValueError("WeChat openid is required")

        with self.database.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM parent_form_access_bindings WHERE token=?",
                (token,),
            ).fetchone()
            if existing and existing["bound_wechat_openid"] != openid:
                raise BindingIdentityConflict()

            values = (
                identity.get("unionid"),
                identity.get("nickname"),
                identity.get("avatar_url") or identity.get("headimgurl"),
                stamp,
                stamp,
                token,
                openid,
            )
            if existing:
                conn.execute(
                    """UPDATE parent_form_access_bindings
                       SET bound_wechat_unionid=?,
                           bound_wechat_nickname=?,
                           bound_wechat_avatar_url=?,
                           last_login_at=?,
                           updated_at=?
                       WHERE token=? AND bound_wechat_openid=?""",
                    values,
                )
            else:
                try:
                    conn.execute(
                        """INSERT INTO parent_form_access_bindings(
                             token,bound_wechat_openid,bound_wechat_unionid,
                             bound_wechat_nickname,bound_wechat_avatar_url,
                             bound_at,last_login_at,created_at,updated_at
                           ) VALUES(?,?,?,?,?,?,?,?,?)""",
                        (
                            token,
                            openid,
                            identity.get("unionid"),
                            identity.get("nickname"),
                            identity.get("avatar_url") or identity.get("headimgurl"),
                            stamp,
                            stamp,
                            stamp,
                            stamp,
                        ),
                    )
                except sqlite3.IntegrityError:
                    current = conn.execute(
                        "SELECT bound_wechat_openid FROM parent_form_access_bindings WHERE token=?",
                        (token,),
                    ).fetchone()
                    if current and current["bound_wechat_openid"] != openid:
                        raise BindingIdentityConflict()
                    raise

            self._insert_access_event(conn, token, "authenticated", stamp)

        return self.get_binding(token)

    def load_draft(self, token):
        row = self.database.one(
            "SELECT * FROM parent_form_drafts WHERE token=?",
            (token,),
        )
        return self._draft_record(row) if row else None

    def save_draft(
        self,
        token,
        payload_encrypted,
        expected_revision,
        source,
        stamp,
    ):
        if source not in {"auto", "manual"}:
            raise ValueError("draft source must be auto or manual")
        try:
            expected_revision = int(expected_revision)
        except (TypeError, ValueError) as exc:
            raise ValueError("expected revision must be an integer") from exc
        if expected_revision < 0:
            raise ValueError("expected revision cannot be negative")

        with self.database.transaction() as conn:
            if expected_revision == 0:
                try:
                    conn.execute(
                        """INSERT INTO parent_form_drafts(
                             token,payload_encrypted,revision,save_source,
                             last_saved_at,created_at,updated_at
                           ) VALUES(?,?,1,?,?,?,?)""",
                        (
                            token,
                            payload_encrypted,
                            source,
                            stamp,
                            stamp,
                            stamp,
                        ),
                    )
                except sqlite3.IntegrityError:
                    self._raise_revision_conflict(conn, token)
            else:
                cursor = conn.execute(
                    """UPDATE parent_form_drafts
                       SET payload_encrypted=?,
                           revision=revision + 1,
                           save_source=?,
                           last_saved_at=?,
                           updated_at=?
                       WHERE token=? AND revision=?""",
                    (
                        payload_encrypted,
                        source,
                        stamp,
                        stamp,
                        token,
                        expected_revision,
                    ),
                )
                if cursor.rowcount != 1:
                    self._raise_revision_conflict(conn, token)

            row = conn.execute(
                "SELECT * FROM parent_form_drafts WHERE token=?",
                (token,),
            ).fetchone()
            return self._draft_record(row)

    def delete_draft(self, conn, token):
        conn.execute("DELETE FROM parent_form_drafts WHERE token=?", (token,))

    @staticmethod
    def _raise_revision_conflict(conn, token):
        row = conn.execute(
            "SELECT revision,last_saved_at FROM parent_form_drafts WHERE token=?",
            (token,),
        ).fetchone()
        raise DraftRevisionConflict(
            row["revision"] if row else 0,
            row["last_saved_at"] if row else None,
        )

    @staticmethod
    def _draft_record(row):
        return DraftRecord(
            token=row["token"],
            payload_encrypted=row["payload_encrypted"],
            revision=int(row["revision"]),
            save_source=row["save_source"],
            last_saved_at=row["last_saved_at"],
        )
