from __future__ import annotations

import json


def case_row(connection, case_id):
    return connection.execute(
        """SELECT c.*,p.* FROM student_cases c
           JOIN progression_cases p ON p.case_id=c.id
           WHERE c.id=? AND c.case_type='progression'""",
        (case_id,),
    ).fetchone()


def list_case_rows(connection, student_id):
    return connection.execute(
        """SELECT c.*,p.* FROM student_cases c
           JOIN progression_cases p ON p.case_id=c.id
           WHERE c.student_id=? AND c.case_type='progression'
           ORDER BY c.sequence_no DESC""",
        (student_id,),
    ).fetchall()


def append_event(
    connection, *, case_id, event_type, actor_user_id, created_at,
    entity_type=None, entity_id=None, old_value=None, new_value=None,
    metadata=None,
):
    cursor = connection.execute(
        """INSERT INTO progression_case_events(
               case_id,event_type,entity_type,entity_id,old_value_json,
               new_value_json,metadata_json,actor_user_id,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            case_id, event_type, entity_type,
            None if entity_id is None else str(entity_id),
            None if old_value is None else json.dumps(old_value, sort_keys=True),
            None if new_value is None else json.dumps(new_value, sort_keys=True),
            json.dumps(metadata or {}, sort_keys=True), actor_user_id, created_at,
        ),
    )
    return cursor.lastrowid


def history_rows(connection, case_id):
    return connection.execute(
        """SELECT * FROM progression_case_events
           WHERE case_id=? ORDER BY created_at,id""",
        (case_id,),
    ).fetchall()


def target_application_rows(connection, case_id):
    return connection.execute(
        """SELECT * FROM progression_target_applications
           WHERE case_id=? ORDER BY id""",
        (case_id,),
    ).fetchall()


def target_application_row(connection, case_id, application_id):
    return connection.execute(
        """SELECT * FROM progression_target_applications
           WHERE id=? AND case_id=?""",
        (application_id, case_id),
    ).fetchone()


def document_link_rows(connection, case_id):
    return connection.execute(
        """SELECT * FROM progression_document_links
           WHERE case_id=? ORDER BY target_application_id,document_role,id""",
        (case_id,),
    ).fetchall()


def arrangement_rows(connection, case_id):
    return connection.execute(
        """SELECT * FROM progression_case_arrangements
           WHERE case_id=? ORDER BY arrangement_type""",
        (case_id,),
    ).fetchall()
