import sqlite3
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path


SCHEMA = """
CREATE TABLE reminders (
  id INTEGER PRIMARY KEY,
  student_id TEXT,
  reminder_type TEXT NOT NULL,
  title TEXT,
  description TEXT,
  related_module TEXT,
  related_record_id TEXT,
  assigned_to_user_id INTEGER,
  assigned_to_name TEXT,
  priority TEXT NOT NULL DEFAULT 'Normal',
  due_date TEXT NOT NULL,
  due_datetime TEXT,
  reminder_datetime TEXT,
  repeat_rule TEXT,
  status TEXT NOT NULL DEFAULT 'Pending',
  snooze_until TEXT,
  snoozed_at TEXT,
  snoozed_by INTEGER,
  completed_at TEXT,
  completed_by INTEGER,
  dismissed_at TEXT,
  dismissed_by INTEGER,
  created_by INTEGER,
  created_at TEXT NOT NULL,
  updated_by INTEGER,
  updated_at TEXT,
  source_type TEXT,
  source_record_id TEXT,
  auto_generated INTEGER NOT NULL DEFAULT 0,
  suggested_action TEXT,
  notes TEXT,
  dedupe_key TEXT,
  source_module TEXT,
  source_object_type TEXT,
  source_object_id TEXT,
  source_event_type TEXT,
  source_event_id TEXT,
  opened_at TEXT,
  opened_by INTEGER,
  metadata_json TEXT,
  canonical_version INTEGER NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX uq_reminders_dedupe_key
ON reminders(dedupe_key)
WHERE dedupe_key IS NOT NULL AND dedupe_key<>'';
"""


class MiniDatabase:
    def __init__(self, path):
        self.path = str(path)

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def transaction(self):
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def one(self, sql, params=()):
        connection = self.connect()
        try:
            return connection.execute(sql, params).fetchone()
        finally:
            connection.close()

    def all(self, sql, params=()):
        connection = self.connect()
        try:
            return connection.execute(sql, params).fetchall()
        finally:
            connection.close()


def load_api(testcase):
    try:
        import reminder_center
    except ModuleNotFoundError:
        testcase.fail("reminder_center module is missing")
    return reminder_center


class ReminderCenterTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "reminders.sqlite3"
        connection = sqlite3.connect(self.path)
        connection.executescript(SCHEMA)
        connection.close()
        self.database = MiniDatabase(self.path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def payload(self, **overrides):
        value = {
            "dedupe_key": "contract:41:link:8:parent_engaged",
            "student_id": "S1001",
            "reminder_type": "contract.parent_engaged",
            "title": "家长已打开合同链接",
            "description": "家长已打开合同链接：S1001 / C-41",
            "related_module": "contracts",
            "related_record_id": "41",
            "priority": "High",
            "due_datetime": "2026-08-20T10:00:00+10:00",
            "reminder_datetime": "2026-08-20T10:00:00+10:00",
            "source_type": "business_event:contract",
            "source_record_id": "contract:41:link:8:parent_engaged",
            "source_module": "contract",
            "source_object_type": "contract",
            "source_object_id": "41",
            "source_event_type": "contract.parent_engaged",
            "source_event_id": "link:8:parent_engaged",
            "created_at": "2026-08-20T10:00:00+10:00",
            "metadata": {"raw_event": "contract.opened"},
            "suggested_action": "/students/S1001?tab=contracts",
            "auto_generated": True,
        }
        value.update(overrides)
        return value

    def test_repeated_event_returns_one_reminder(self):
        api = load_api(self)
        results = [api.create_from_event(self.database, self.payload()) for _ in range(10)]
        ids = [result["id"] for result in results]
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual([result.get("_created") for result in results], [True] + [False] * 9)
        self.assertEqual(self.database.one("SELECT COUNT(*) count FROM reminders")["count"], 1)

    def test_concurrent_event_returns_one_reminder(self):
        api = load_api(self)
        barrier = threading.Barrier(10)
        ids = []
        errors = []

        def emit():
            try:
                barrier.wait()
                ids.append(api.create_from_event(MiniDatabase(self.path), self.payload())["id"])
            except Exception as exc:  # asserted below with full exception values
                errors.append(repr(exc))

        threads = [threading.Thread(target=emit) for _ in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(self.database.one("SELECT COUNT(*) count FROM reminders")["count"], 1)

    def test_terminal_event_never_reopens_and_new_event_is_independent(self):
        api = load_api(self)
        first = api.create_from_event(self.database, self.payload())
        api.complete(self.database, first["id"], actor_id=7, now="2026-08-20T11:00:00+10:00")
        observed_again = api.create_from_event(
            self.database,
            self.payload(title="changed presentation", metadata={"raw_event": "contract.viewed"}),
        )
        second = api.create_from_event(
            self.database,
            self.payload(
                dedupe_key="contract:41:signature:99:signed",
                reminder_type="contract.signed",
                source_event_type="contract.signed",
                source_event_id="signature:99",
            ),
        )
        self.assertEqual(observed_again["id"], first["id"])
        self.assertEqual(observed_again["status"], "Completed")
        self.assertNotEqual(second["id"], first["id"])

    def test_adopting_duplicate_legacy_events_cancels_every_shadow(self):
        api = load_api(self)
        connection = self.database.connect()
        try:
            for title in ("legacy one", "legacy two"):
                connection.execute(
                    """INSERT INTO reminders(
                         student_id,reminder_type,title,due_date,due_datetime,reminder_datetime,
                         status,created_at,source_type,source_record_id,auto_generated
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,1)""",
                    (
                        "S1001", "contract.parent_engaged", title, "2026-08-20",
                        "2026-08-20T10:00:00+10:00", "2026-08-20T10:00:00+10:00",
                        "Pending", "2026-08-20T09:00:00+10:00", "business_event:contract",
                        "contract:41:link:8:parent_engaged",
                    ),
                )
            connection.commit()
        finally:
            connection.close()

        adopted = api.create_from_event(self.database, self.payload())
        rows = self.database.all(
            "SELECT id,status,dedupe_key,notes FROM reminders ORDER BY id"
        )
        self.assertEqual(adopted["id"], rows[0]["id"])
        self.assertEqual(rows[0]["dedupe_key"], self.payload()["dedupe_key"])
        self.assertEqual(rows[1]["status"], "Cancelled")
        self.assertIsNone(rows[1]["dedupe_key"])
        self.assertIn(f"Superseded by canonical reminder #{rows[0]['id']}", rows[1]["notes"])

    def test_snooze_is_hidden_until_expiry_and_open_does_not_complete(self):
        api = load_api(self)
        row = api.create_from_event(self.database, self.payload())
        api.mark_opened(self.database, row["id"], actor_id=8, now="2026-08-20T10:05:00+10:00")
        opened = self.database.one("SELECT status,opened_at,opened_by FROM reminders WHERE id=?", (row["id"],))
        self.assertEqual((opened["status"], opened["opened_by"]), ("Pending", 8))
        api.snooze(
            self.database,
            row["id"],
            actor_id=8,
            until="2026-08-20T12:00:00+10:00",
            now="2026-08-20T10:06:00+10:00",
        )
        snoozed = self.database.one(
            "SELECT status,snooze_until,snoozed_at,snoozed_by FROM reminders WHERE id=?",
            (row["id"],),
        )
        self.assertEqual(tuple(snoozed), (
            "Snoozed", "2026-08-20T12:00:00+10:00",
            "2026-08-20T10:06:00+10:00", 8,
        ))
        hidden = api.list_actionable(self.database, None, now="2026-08-20T11:59:59+10:00")
        visible = api.list_actionable(self.database, None, now="2026-08-20T12:00:01+10:00")
        self.assertEqual(hidden, [])
        self.assertEqual([item["id"] for item in visible], [row["id"]])

    def test_canonical_key_rejects_empty_or_ambiguous_parts(self):
        api = load_api(self)
        self.assertEqual(
            api.canonical_dedupe_key("contract", 41, "link", 8, "parent_engaged"),
            "contract:41:link:8:parent_engaged",
        )
        with self.assertRaises(ValueError):
            api.canonical_dedupe_key("contract", "", "opened")


if __name__ == "__main__":
    unittest.main()
