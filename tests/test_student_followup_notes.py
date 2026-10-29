import sqlite3
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo


class StudentFollowupNoteTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            CREATE TABLE students(student_id TEXT PRIMARY KEY, deleted_at TEXT);
            CREATE TABLE users(
              id INTEGER PRIMARY KEY, username TEXT, display_name TEXT,
              is_active INTEGER NOT NULL DEFAULT 1, deleted_at TEXT
            );
            CREATE TABLE reminders(
              id INTEGER PRIMARY KEY, student_id TEXT, reminder_type TEXT NOT NULL,
              title TEXT, description TEXT, related_module TEXT, related_record_id TEXT,
              assigned_to_user_id INTEGER, assigned_to_name TEXT, priority TEXT,
              due_date TEXT, due_datetime TEXT, reminder_datetime TEXT, repeat_rule TEXT,
              status TEXT, snooze_until TEXT, completed_at TEXT, completed_by INTEGER,
              dismissed_at TEXT, dismissed_by INTEGER, created_by INTEGER, created_at TEXT,
              updated_by INTEGER, updated_at TEXT, source_type TEXT, source_record_id TEXT,
              auto_generated INTEGER, suggested_action TEXT, notes TEXT,
              dedupe_key TEXT, source_module TEXT, source_object_type TEXT,
              source_object_id TEXT, source_event_type TEXT, source_event_id TEXT,
              opened_at TEXT, opened_by INTEGER, metadata_json TEXT,
              canonical_version INTEGER NOT NULL DEFAULT 1
            );
            INSERT INTO students VALUES('S001',NULL),('S002',NULL),('DELETED','2026-01-01');
            INSERT INTO users VALUES
              (1,'creator','Creator User',1,NULL),
              (2,'other','Other User',1,NULL),
              (3,'inactive','Inactive User',0,NULL),
              (4,'admin','Admin User',1,NULL),
              (5,'manager','Manager User',1,NULL);
            """
        )
        self.creator = {"id": 1, "username": "creator", "role_name": "Consultant"}
        self.other = {"id": 2, "username": "other", "role_name": "Consultant"}
        self.admin = {"id": 4, "username": "admin", "role_name": "Admin"}
        self.manager = {"id": 5, "username": "manager", "role_name": "Manager"}
        self.now = datetime(2026, 8, 8, 10, 0, tzinfo=ZoneInfo("Australia/Melbourne"))

    def tearDown(self):
        self.connection.close()

    def create_note(self, **overrides):
        from student_followup_notes import create_student_followup_note

        values = {
            "student_id": "S001",
            "content": "请联系家长确认材料。",
            "reminder_at": "2026-08-09T11:30",
            "recipient_id": 1,
            "actor": self.creator,
            "now": self.now,
        }
        values.update(overrides)
        return create_student_followup_note(self.connection, **values)

    def test_create_persists_one_synchronized_reminder(self):
        note = self.create_note()
        row = dict(self.connection.execute("SELECT * FROM reminders WHERE id=?", (note.id,)).fetchone())

        self.assertEqual(row["reminder_type"], "student_followup")
        self.assertEqual(row["source_type"], "student_followup_note")
        self.assertEqual(row["related_module"], "students")
        self.assertEqual(row["related_record_id"], "S001")
        self.assertEqual(row["assigned_to_user_id"], 1)
        self.assertEqual(row["assigned_to_name"], "Creator User")
        self.assertEqual(row["status"], "Pending")
        self.assertEqual(row["description"], "请联系家长确认材料。")
        self.assertEqual(row["due_datetime"], "2026-08-09T11:30")
        self.assertEqual(row["reminder_datetime"], "2026-08-09T11:30")
        self.assertEqual(row["suggested_action"], "/students/S001?tab=reminders")
        self.assertEqual(row["dedupe_key"], f"manual:student_followup:{note.id}")
        self.assertEqual(row["source_module"], "students")
        self.assertEqual(row["source_event_id"], str(note.id))

    def test_create_rejects_invalid_values_without_insert(self):
        from student_followup_notes import FollowupValidationError

        invalid = [
            {"content": ""},
            {"reminder_at": "not-a-date"},
            {"reminder_at": "2026-08-08T09:59"},
            {"student_id": "MISSING"},
            {"student_id": "DELETED"},
            {"recipient_id": 3},
            {"recipient_id": 999},
        ]
        for values in invalid:
            with self.subTest(values=values):
                with self.assertRaises(FollowupValidationError):
                    self.create_note(**values)
        count = self.connection.execute("SELECT COUNT(*) FROM reminders").fetchone()[0]
        self.assertEqual(count, 0)

    def test_creator_admin_and_manager_can_update_but_other_staff_cannot(self):
        from student_followup_notes import FollowupPermissionError, update_student_followup_note

        note = self.create_note()
        with self.assertRaises(FollowupPermissionError):
            update_student_followup_note(
                self.connection, note.id, content="不应保存", reminder_at="2026-08-10T12:00",
                recipient_id=2, actor=self.other, now=self.now,
            )
        for actor, content in ((self.creator, "创建人修改"), (self.admin, "管理员修改"), (self.manager, "经理修改")):
            updated = update_student_followup_note(
                self.connection, note.id, content=content, reminder_at="2026-08-10T12:00",
                recipient_id=2, actor=actor, now=self.now,
            )
            self.assertEqual(updated.description, content)
            self.assertEqual(updated.assigned_to_user_id, 2)

    def test_update_rejects_terminal_note_and_preserves_terminal_metadata(self):
        from student_followup_notes import FollowupValidationError, update_student_followup_note

        note = self.create_note()
        self.connection.execute(
            "UPDATE reminders SET status='Completed',completed_at='2026-08-08',completed_by=1,dismissed_at='2026-08-08',dismissed_by=1 WHERE id=?",
            (note.id,),
        )
        with self.assertRaises(FollowupValidationError):
            update_student_followup_note(
                self.connection, note.id, content="重新跟进", reminder_at="2026-08-11T09:00",
                recipient_id=1, actor=self.creator, now=self.now,
            )
        row = self.connection.execute(
            "SELECT status,completed_at,completed_by,dismissed_at,dismissed_by,snooze_until FROM reminders WHERE id=?",
            (note.id,),
        ).fetchone()
        self.assertEqual(tuple(row), ("Completed", "2026-08-08", 1, "2026-08-08", 1, None))

    def test_soft_delete_retains_content_and_hides_note(self):
        from student_followup_notes import list_student_followup_notes, soft_delete_student_followup_note

        note = self.create_note()
        deleted = soft_delete_student_followup_note(self.connection, note.id, actor=self.creator, now=self.now)
        row = self.connection.execute(
            "SELECT description,status,source_type,updated_by FROM reminders WHERE id=?", (note.id,)
        ).fetchone()
        self.assertEqual(deleted.source_type, "student_followup_note_deleted")
        self.assertEqual(tuple(row), ("请联系家长确认材料。", "Cancelled", "student_followup_note_deleted", 1))
        self.assertEqual(list_student_followup_notes(self.connection, "S001"), [])

    def test_delete_permissions_match_update_permissions(self):
        from student_followup_notes import FollowupPermissionError, soft_delete_student_followup_note

        for actor, allowed in ((self.other, False), (self.creator, True), (self.admin, True), (self.manager, True)):
            note = self.create_note(content=f"note-{actor['id']}")
            if allowed:
                soft_delete_student_followup_note(self.connection, note.id, actor=actor, now=self.now)
            else:
                with self.assertRaises(FollowupPermissionError):
                    soft_delete_student_followup_note(self.connection, note.id, actor=actor, now=self.now)
                status = self.connection.execute("SELECT status FROM reminders WHERE id=?", (note.id,)).fetchone()[0]
                self.assertEqual(status, "Pending")

    def test_list_includes_names_and_audit_payload(self):
        from student_followup_notes import (
            build_followup_audit_payload,
            list_student_followup_notes,
            update_student_followup_note,
        )

        before = self.create_note()
        after = update_student_followup_note(
            self.connection, before.id, content="更新后的内容", reminder_at="2026-08-12T14:15",
            recipient_id=2, actor=self.admin, now=self.now,
        )
        listed = list_student_followup_notes(self.connection, "S001")
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0].creator_name, "Creator User")
        self.assertEqual(listed[0].editor_name, "Admin User")
        self.assertEqual(listed[0].recipient_name, "Other User")
        payload = build_followup_audit_payload(before, after, "update")
        self.assertEqual(payload["action"], "update")
        self.assertEqual(payload["before"]["content"], "请联系家长确认材料。")
        self.assertEqual(payload["after"]["content"], "更新后的内容")

    def test_next_active_note_uses_nearest_effective_time(self):
        notes = [
            self.create_note(content="已过期提醒", reminder_at="2026-08-09T10:00"),
            self.create_note(content="明天先联系家长", reminder_at="2026-08-09T09:00"),
            self.create_note(content="已延后提醒", reminder_at="2026-08-08T11:00"),
            self.create_note(content="已完成提醒", reminder_at="2026-08-08T10:30"),
            self.create_note(content="已忽略提醒", reminder_at="2026-08-08T10:45"),
            self.create_note(content="已取消提醒", reminder_at="2026-08-08T10:50"),
            self.create_note(content="已删除提醒", reminder_at="2026-08-09T08:00"),
        ]
        self.connection.executemany(
            "UPDATE reminders SET status=?,snooze_until=?,source_type=?,reminder_datetime=?,due_datetime=? WHERE id=?",
            (
                ("Pending", None, "student_followup_note", "2026-08-08T09:00", "2026-08-08T09:00", notes[0].id),
                ("Pending", None, "student_followup_note", "2026-08-09T09:00", "2026-08-09T09:00", notes[1].id),
                ("Snoozed", "2026-08-10T09:00", "student_followup_note", "2026-08-08T11:00", "2026-08-08T11:00", notes[2].id),
                ("Completed", None, "student_followup_note", "2026-08-08T10:30", "2026-08-08T10:30", notes[3].id),
                ("Dismissed", None, "student_followup_note", "2026-08-08T10:45", "2026-08-08T10:45", notes[4].id),
                ("Cancelled", None, "student_followup_note", "2026-08-08T10:50", "2026-08-08T10:50", notes[5].id),
                ("Pending", None, "student_followup_note_deleted", "2026-08-09T08:00", "2026-08-09T08:00", notes[6].id),
            ),
        )

        from student_followup_notes import get_next_student_followup_note

        note = get_next_student_followup_note(self.connection, "S001", now=self.now)

        self.assertEqual(note.description, "明天先联系家长")
        self.assertEqual(note.reminder_at, "2026-08-09T09:00")

    def test_next_active_note_returns_none_when_no_eligible_reminder_exists(self):
        note = self.create_note(reminder_at="2026-08-09T09:00")
        self.connection.execute("UPDATE reminders SET status='Completed' WHERE id=?", (note.id,))

        from student_followup_notes import get_next_student_followup_note

        self.assertIsNone(get_next_student_followup_note(self.connection, "S001", now=self.now))


if __name__ == "__main__":
    unittest.main()
