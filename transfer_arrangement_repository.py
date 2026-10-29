"""Connection-scoped SQL primitives for Transfer arrangements.

The caller owns the explicit SQLite connection, transaction, and commit.
This module never opens a database and never reads application session state.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Mapping


REQUIRED_TABLES = frozenset(
    {
        "users",
        "students",
        "student_cases",
        "transfer_cases",
        "transfer_school_applications",
        "case_events",
        "case_record_links",
        "case_arrangement_confirmations",
        "transfer_guardianship_arrangements",
        "transfer_accommodation_arrangements",
        "case_arrangement_evidence_links",
        "student_documents",
        "visas",
        "coe_oshc",
    }
)


def _row_dict(cursor: sqlite3.Cursor, row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):
        return dict(row)
    names = [column[0] for column in cursor.description or ()]
    return dict(zip(names, row))


def _one(cursor: sqlite3.Cursor) -> dict[str, Any] | None:
    return _row_dict(cursor, cursor.fetchone())


def _many(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [
        item
        for row in cursor.fetchall()
        if (item := _row_dict(cursor, row)) is not None
    ]


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info('{table}')")
    }


def schema_ready(connection: sqlite3.Connection) -> bool:
    placeholders = ",".join("?" for _ in REQUIRED_TABLES)
    rows = connection.execute(
        f"""SELECT name FROM sqlite_master
            WHERE type='table' AND name IN ({placeholders})""",
        tuple(sorted(REQUIRED_TABLES)),
    ).fetchall()
    if {str(row[0]) for row in rows} != REQUIRED_TABLES:
        return False
    confirmation_columns = _columns(
        connection, "case_arrangement_confirmations"
    )
    return {
        "guardianship_arrangement_id",
        "accommodation_arrangement_id",
        "rule_version",
        "requirement_reason",
        "updated_by",
    }.issubset(confirmation_columns)


def get_case_context(
    connection: sqlite3.Connection, case_id: int
) -> dict[str, Any] | None:
    student_columns = _columns(connection, "students")
    dob_name = next(
        (
            name
            for name in ("date_of_birth", "dob", "birth_date")
            if name in student_columns
        ),
        None,
    )
    dob_select = f"s.{dob_name}" if dob_name else "NULL"
    return _one(
        connection.execute(
            f"""SELECT sc.id AS case_id,sc.student_id,sc.case_type,
                       sc.status AS student_case_status,sc.active_slot,
                       tc.workflow_state,tc.final_application_id,
                       final.expected_start_date,
                       {dob_select} AS date_of_birth
                FROM student_cases sc
                JOIN transfer_cases tc ON tc.case_id=sc.id
                LEFT JOIN transfer_school_applications final
                  ON final.id=tc.final_application_id
                 AND final.case_id=tc.case_id
                 AND final.is_final=1
                LEFT JOIN students s ON s.student_id=sc.student_id
                WHERE sc.id=? AND sc.case_type='transfer'""",
            (case_id,),
        )
    )


def user_exists(connection: sqlite3.Connection, user_id: int) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM users WHERE id=? AND is_active=1", (user_id,)
        ).fetchone()
        is not None
    )


def _available_records(
    connection: sqlite3.Connection,
    *,
    table: str,
    student_id: str,
    record_type: str,
    where: str = "",
    parameters: tuple[Any, ...] = (),
) -> list[dict[str, Any]]:
    columns = _columns(connection, table)
    optional = [
        name
        for name in (
            "document_kind",
            "visa_type",
            "visa_subclass",
            "expiry_date",
            "visa_expiry_date",
            "provider",
            "provider_name",
            "policy_number",
            "policy_no",
            "start_date",
            "end_date",
            "visa_grant_date",
            "created_at",
            "oshc_expiry_date",
            "oshc_end_date",
            "oshc_commencement_date",
            "oshc_start_date",
        )
        if name in columns
    ]
    selected = ",".join(f"r.{name}" for name in optional)
    if selected:
        selected = "," + selected
    date_names = (
        ("visa_expiry_date", "visa_grant_date", "created_at")
        if record_type == "visa"
        else (
            "oshc_expiry_date", "oshc_end_date",
            "oshc_commencement_date", "oshc_start_date", "created_at",
        )
    )
    date_parts = [
        f"NULLIF(r.{name},'')" for name in date_names if name in columns
    ]
    business_order = (
        f"COALESCE({','.join(date_parts)})" if len(date_parts) > 1
        else date_parts[0] if date_parts else "r.id"
    )
    created_order = "r.created_at" if "created_at" in columns else "r.id"
    return _many(
        connection.execute(
            f"""SELECT r.id AS record_id,r.student_id,
                       ? AS record_type{selected}
                FROM {table} r
                WHERE r.student_id=? {where}
                ORDER BY {business_order} DESC,{created_order} DESC,r.id DESC""",
            (record_type, student_id, *parameters),
        )
    )


def list_available_visas(
    connection: sqlite3.Connection, student_id: str
) -> list[dict[str, Any]]:
    return _available_records(
        connection,
        table="visas",
        student_id=student_id,
        record_type="visa",
    )


def list_available_oshc_records(
    connection: sqlite3.Connection, student_id: str
) -> list[dict[str, Any]]:
    return _available_records(
        connection,
        table="coe_oshc",
        student_id=student_id,
        record_type="oshc",
        where="AND r.document_kind='OSHC'",
    )


def get_source_record(
    connection: sqlite3.Connection,
    record_type: str,
    record_id: int,
) -> dict[str, Any] | None:
    if record_type == "visa":
        rows = _available_records(
            connection,
            table="visas",
            student_id=(
                _one(
                    connection.execute(
                        "SELECT student_id FROM visas WHERE id=?", (record_id,)
                    )
                )
                or {}
            ).get("student_id", ""),
            record_type="visa",
        )
    elif record_type == "oshc":
        row = _one(
            connection.execute(
                "SELECT student_id FROM coe_oshc WHERE id=?", (record_id,)
            )
        )
        rows = _available_records(
            connection,
            table="coe_oshc",
            student_id=(row or {}).get("student_id", ""),
            record_type="oshc",
            where="AND r.document_kind='OSHC'",
        )
    else:
        return None
    return next((row for row in rows if row["record_id"] == record_id), None)


RECORD_LINK_COLUMNS = """
    id AS link_id,case_id,record_type,record_id,document_id,link_role,
    validity_status,effective_from,superseded_by,created_by,created_at
"""


def _list_arrangement_record_links(
    connection: sqlite3.Connection,
    case_id: int,
    record_type: str,
) -> list[dict[str, Any]]:
    return _many(
        connection.execute(
            f"""SELECT {RECORD_LINK_COLUMNS}
                FROM case_record_links
                WHERE case_id=? AND transfer_application_id IS NULL
                  AND record_type=?
                ORDER BY
                  CASE validity_status
                    WHEN 'current' THEN 0
                    WHEN 'carried_forward' THEN 0
                    ELSE 1
                  END,
                  id DESC""",
            (case_id, record_type),
        )
    )


def list_visa_record_links(
    connection: sqlite3.Connection, case_id: int
) -> list[dict[str, Any]]:
    return _list_arrangement_record_links(connection, case_id, "visa")


def list_oshc_record_links(
    connection: sqlite3.Connection, case_id: int
) -> list[dict[str, Any]]:
    return _list_arrangement_record_links(connection, case_id, "oshc")


def get_record_link(
    connection: sqlite3.Connection, link_id: int
) -> dict[str, Any] | None:
    return _one(
        connection.execute(
            f"SELECT {RECORD_LINK_COLUMNS} FROM case_record_links WHERE id=?",
            (link_id,),
        )
    )


def _get_current_record_link(
    connection: sqlite3.Connection,
    case_id: int,
    record_type: str,
) -> dict[str, Any] | None:
    return _one(
        connection.execute(
            f"""SELECT {RECORD_LINK_COLUMNS}
                FROM case_record_links
                WHERE case_id=? AND transfer_application_id IS NULL
                  AND record_type=?
                  AND validity_status IN ('current','carried_forward')
                ORDER BY id DESC LIMIT 1""",
            (case_id, record_type),
        )
    )


def get_current_visa_record_link(
    connection: sqlite3.Connection, case_id: int
) -> dict[str, Any] | None:
    return _get_current_record_link(connection, case_id, "visa")


def get_current_oshc_record_link(
    connection: sqlite3.Connection, case_id: int
) -> dict[str, Any] | None:
    return _get_current_record_link(connection, case_id, "oshc")


def get_record_link_for_source(
    connection: sqlite3.Connection,
    case_id: int,
    record_type: str,
    record_id: int,
) -> dict[str, Any] | None:
    return _one(
        connection.execute(
            f"""SELECT {RECORD_LINK_COLUMNS}
                FROM case_record_links
                WHERE case_id=? AND transfer_application_id IS NULL
                  AND record_type=? AND record_id=?""",
            (case_id, record_type, record_id),
        )
    )


def insert_record_link(
    connection: sqlite3.Connection, values: Mapping[str, Any]
) -> int:
    payload = dict(values)
    payload.setdefault(
        "link_id",
        int(
            connection.execute(
                "SELECT coalesce(max(id),0)+1 FROM case_record_links"
            ).fetchone()[0]
        ),
    )
    connection.execute(
        """INSERT INTO case_record_links(
               id,case_id,transfer_application_id,record_type,record_id,
               document_id,link_role,validity_status,effective_from,
               superseded_by,created_by,created_at
           ) VALUES(
               :link_id,:case_id,NULL,:record_type,:record_id,
               :document_id,:link_role,:validity_status,:effective_from,
               NULL,:created_by,:created_at
           )""",
        payload,
    )
    return int(payload["link_id"])


def supersede_record_link(
    connection: sqlite3.Connection,
    link_id: int,
    superseded_by: int,
) -> bool:
    cursor = connection.execute(
        """UPDATE case_record_links
           SET validity_status='superseded',superseded_by=?
           WHERE id=? AND validity_status IN ('current','carried_forward')""",
        (superseded_by, link_id),
    )
    return cursor.rowcount == 1


def update_record_link_validity(
    connection: sqlite3.Connection,
    link_id: int,
    *,
    link_role: str,
    validity_status: str,
) -> bool:
    cursor = connection.execute(
        """UPDATE case_record_links
           SET link_role=?,validity_status=?
           WHERE id=? AND validity_status IN (
               'pending_confirmation','current','carried_forward'
           )""",
        (link_role, validity_status, link_id),
    )
    return cursor.rowcount == 1


insert_visa_record_link = insert_record_link
insert_oshc_record_link = insert_record_link
supersede_visa_record_link = supersede_record_link
supersede_oshc_record_link = supersede_record_link
update_visa_record_link_validity = update_record_link_validity
update_oshc_record_link_validity = update_record_link_validity


CONFIRMATION_COLUMNS = """
    id AS confirmation_id,case_id,arrangement_type,arrangement_status,
    linked_record_id,document_id,notes,confirmed_by,confirmed_at,
    guardianship_arrangement_id,accommodation_arrangement_id,rule_version,
    requirement_reason,updated_by,created_at,updated_at
"""


def get_confirmation(
    connection: sqlite3.Connection,
    case_id: int,
    arrangement_type: str,
) -> dict[str, Any] | None:
    return _one(
        connection.execute(
            f"""SELECT {CONFIRMATION_COLUMNS}
                FROM case_arrangement_confirmations
                WHERE case_id=? AND arrangement_type=?""",
            (case_id, arrangement_type),
        )
    )


def get_visa_confirmation(
    connection: sqlite3.Connection, case_id: int
) -> dict[str, Any] | None:
    return get_confirmation(connection, case_id, "visa")


def get_oshc_confirmation(
    connection: sqlite3.Connection, case_id: int
) -> dict[str, Any] | None:
    return get_confirmation(connection, case_id, "oshc")


def get_guardianship_confirmation(
    connection: sqlite3.Connection, case_id: int
) -> dict[str, Any] | None:
    return get_confirmation(connection, case_id, "guardianship")


def get_accommodation_confirmation(
    connection: sqlite3.Connection, case_id: int
) -> dict[str, Any] | None:
    return get_confirmation(connection, case_id, "accommodation")


def insert_or_update_confirmation(
    connection: sqlite3.Connection, values: Mapping[str, Any]
) -> int:
    payload = dict(values)
    current = get_confirmation(
        connection, int(payload["case_id"]), str(payload["arrangement_type"])
    )
    if current is None:
        payload.setdefault(
            "confirmation_id",
            int(
                connection.execute(
                    """SELECT coalesce(max(id),0)+1
                       FROM case_arrangement_confirmations"""
                ).fetchone()[0]
            ),
        )
        connection.execute(
            """INSERT INTO case_arrangement_confirmations(
                   id,case_id,arrangement_type,arrangement_status,
                   linked_record_id,document_id,notes,confirmed_by,confirmed_at,
                   created_at,updated_at,guardianship_arrangement_id,
                   accommodation_arrangement_id,rule_version,
                   requirement_reason,updated_by
               ) VALUES(
                   :confirmation_id,:case_id,:arrangement_type,
                   :arrangement_status,:linked_record_id,:document_id,:notes,
                   :confirmed_by,:confirmed_at,:created_at,:updated_at,
                   :guardianship_arrangement_id,
                   :accommodation_arrangement_id,:rule_version,
                   :requirement_reason,:updated_by
               )""",
            payload,
        )
        return int(payload["confirmation_id"])
    connection.execute(
        """UPDATE case_arrangement_confirmations
           SET arrangement_status=:arrangement_status,
               linked_record_id=:linked_record_id,
               document_id=:document_id,
               notes=:notes,
               confirmed_by=:confirmed_by,
               confirmed_at=:confirmed_at,
               guardianship_arrangement_id=:guardianship_arrangement_id,
               accommodation_arrangement_id=:accommodation_arrangement_id,
               rule_version=:rule_version,
               requirement_reason=:requirement_reason,
               updated_by=:updated_by,
               updated_at=:updated_at
           WHERE id=:confirmation_id""",
        {**payload, "confirmation_id": current["confirmation_id"]},
    )
    return int(current["confirmation_id"])


insert_or_update_visa_confirmation = insert_or_update_confirmation
insert_or_update_oshc_confirmation = insert_or_update_confirmation
insert_or_update_guardianship_confirmation = insert_or_update_confirmation
insert_or_update_accommodation_confirmation = insert_or_update_confirmation


GUARDIANSHIP_COLUMNS = """
    id AS arrangement_id,case_id,version_no,arrangement_mode,
    guardian_person_name,guardian_organisation_name,relationship_to_student,
    provider_name,start_date,end_date,source_type,arrangement_status,
    is_current,superseded_by,notes,created_by,created_at,updated_by,updated_at
"""

ACCOMMODATION_COLUMNS = """
    id AS arrangement_id,case_id,version_no,accommodation_type,provider_name,
    host_name,address_line1,address_line2,suburb,state_code,postcode,
    country_code,start_date,end_date,source_type,arrangement_status,
    is_current,superseded_by,notes,created_by,created_at,updated_by,updated_at
"""


def list_guardianship_versions(
    connection: sqlite3.Connection, case_id: int
) -> list[dict[str, Any]]:
    return _many(
        connection.execute(
            f"""SELECT {GUARDIANSHIP_COLUMNS}
                FROM transfer_guardianship_arrangements
                WHERE case_id=?
                ORDER BY is_current DESC,version_no DESC,id DESC""",
            (case_id,),
        )
    )


def get_current_guardianship(
    connection: sqlite3.Connection, case_id: int
) -> dict[str, Any] | None:
    return _one(
        connection.execute(
            f"""SELECT {GUARDIANSHIP_COLUMNS}
                FROM transfer_guardianship_arrangements
                WHERE case_id=? AND is_current=1""",
            (case_id,),
        )
    )


def get_guardianship_version(
    connection: sqlite3.Connection, case_id: int, arrangement_id: int
) -> dict[str, Any] | None:
    return _one(
        connection.execute(
            f"""SELECT {GUARDIANSHIP_COLUMNS}
                FROM transfer_guardianship_arrangements
                WHERE case_id=? AND id=?""",
            (case_id, arrangement_id),
        )
    )


def next_guardianship_identity(
    connection: sqlite3.Connection, case_id: int
) -> tuple[int, int]:
    row = connection.execute(
        """SELECT coalesce(max(id),0)+1,
                  coalesce(max(CASE WHEN case_id=? THEN version_no END),0)+1
           FROM transfer_guardianship_arrangements""",
        (case_id,),
    ).fetchone()
    return int(row[0]), int(row[1])


def insert_guardianship_version(
    connection: sqlite3.Connection, values: Mapping[str, Any]
) -> int:
    payload = dict(values)
    connection.execute(
        """INSERT INTO transfer_guardianship_arrangements(
               id,case_id,version_no,arrangement_mode,guardian_person_name,
               guardian_organisation_name,relationship_to_student,
               provider_name,start_date,end_date,source_type,
               arrangement_status,is_current,superseded_by,notes,created_by,
               created_at,updated_by,updated_at
           ) VALUES(
               :arrangement_id,:case_id,:version_no,:arrangement_mode,
               :guardian_person_name,:guardian_organisation_name,
               :relationship_to_student,:provider_name,:start_date,:end_date,
               :source_type,:arrangement_status,1,NULL,:notes,:created_by,
               :created_at,:updated_by,:updated_at
           )""",
        payload,
    )
    return int(payload["arrangement_id"])


def supersede_guardianship_version(
    connection: sqlite3.Connection,
    arrangement_id: int,
    superseded_by: int,
    *,
    updated_by: int,
    updated_at: str,
) -> bool:
    cursor = connection.execute(
        """UPDATE transfer_guardianship_arrangements
           SET is_current=0,superseded_by=?,updated_by=?,updated_at=?
           WHERE id=? AND is_current=1""",
        (superseded_by, updated_by, updated_at, arrangement_id),
    )
    return cursor.rowcount == 1


def list_accommodation_versions(
    connection: sqlite3.Connection, case_id: int
) -> list[dict[str, Any]]:
    return _many(
        connection.execute(
            f"""SELECT {ACCOMMODATION_COLUMNS}
                FROM transfer_accommodation_arrangements
                WHERE case_id=?
                ORDER BY is_current DESC,version_no DESC,id DESC""",
            (case_id,),
        )
    )


def get_current_accommodation(
    connection: sqlite3.Connection, case_id: int
) -> dict[str, Any] | None:
    return _one(
        connection.execute(
            f"""SELECT {ACCOMMODATION_COLUMNS}
                FROM transfer_accommodation_arrangements
                WHERE case_id=? AND is_current=1""",
            (case_id,),
        )
    )


def get_accommodation_version(
    connection: sqlite3.Connection, case_id: int, arrangement_id: int
) -> dict[str, Any] | None:
    return _one(
        connection.execute(
            f"""SELECT {ACCOMMODATION_COLUMNS}
                FROM transfer_accommodation_arrangements
                WHERE case_id=? AND id=?""",
            (case_id, arrangement_id),
        )
    )


def next_accommodation_identity(
    connection: sqlite3.Connection, case_id: int
) -> tuple[int, int]:
    row = connection.execute(
        """SELECT coalesce(max(id),0)+1,
                  coalesce(max(CASE WHEN case_id=? THEN version_no END),0)+1
           FROM transfer_accommodation_arrangements""",
        (case_id,),
    ).fetchone()
    return int(row[0]), int(row[1])


def insert_accommodation_version(
    connection: sqlite3.Connection, values: Mapping[str, Any]
) -> int:
    payload = dict(values)
    connection.execute(
        """INSERT INTO transfer_accommodation_arrangements(
               id,case_id,version_no,accommodation_type,provider_name,
               host_name,address_line1,address_line2,suburb,state_code,
               postcode,country_code,start_date,end_date,source_type,
               arrangement_status,is_current,superseded_by,notes,created_by,
               created_at,updated_by,updated_at
           ) VALUES(
               :arrangement_id,:case_id,:version_no,:accommodation_type,
               :provider_name,:host_name,:address_line1,:address_line2,
               :suburb,:state_code,:postcode,:country_code,:start_date,
               :end_date,:source_type,:arrangement_status,1,NULL,:notes,
               :created_by,:created_at,:updated_by,:updated_at
           )""",
        payload,
    )
    return int(payload["arrangement_id"])


def supersede_accommodation_version(
    connection: sqlite3.Connection,
    arrangement_id: int,
    superseded_by: int,
    *,
    updated_by: int,
    updated_at: str,
) -> bool:
    cursor = connection.execute(
        """UPDATE transfer_accommodation_arrangements
           SET is_current=0,superseded_by=?,updated_by=?,updated_at=?
           WHERE id=? AND is_current=1""",
        (superseded_by, updated_by, updated_at, arrangement_id),
    )
    return cursor.rowcount == 1


EVIDENCE_COLUMNS = """
    id AS evidence_id,case_id,arrangement_type,arrangement_confirmation_id,
    guardianship_arrangement_id,accommodation_arrangement_id,
    case_record_link_id,document_id,evidence_role,validity_status,
    effective_from,superseded_by,created_by,created_at
"""


def list_arrangement_evidence(
    connection: sqlite3.Connection,
    case_id: int,
    arrangement_type: str,
    *,
    arrangement_confirmation_id: int | None = None,
    guardianship_arrangement_id: int | None = None,
    accommodation_arrangement_id: int | None = None,
) -> list[dict[str, Any]]:
    clauses = ["case_id=?", "arrangement_type=?"]
    parameters: list[Any] = [case_id, arrangement_type]
    for column, value in (
        ("arrangement_confirmation_id", arrangement_confirmation_id),
        ("guardianship_arrangement_id", guardianship_arrangement_id),
        ("accommodation_arrangement_id", accommodation_arrangement_id),
    ):
        if value is not None:
            clauses.append(f"{column}=?")
            parameters.append(value)
    return _many(
        connection.execute(
            f"""SELECT {EVIDENCE_COLUMNS}
                FROM case_arrangement_evidence_links
                WHERE {' AND '.join(clauses)}
                ORDER BY
                  CASE validity_status WHEN 'current' THEN 0 ELSE 1 END,
                  id DESC""",
            tuple(parameters),
        )
    )


def get_arrangement_evidence(
    connection: sqlite3.Connection,
    case_id: int,
    evidence_id: int,
) -> dict[str, Any] | None:
    return _one(
        connection.execute(
            f"""SELECT {EVIDENCE_COLUMNS}
                FROM case_arrangement_evidence_links
                WHERE case_id=? AND id=?""",
            (case_id, evidence_id),
        )
    )


def find_current_evidence(
    connection: sqlite3.Connection, values: Mapping[str, Any]
) -> dict[str, Any] | None:
    return _one(
        connection.execute(
            f"""SELECT {EVIDENCE_COLUMNS}
                FROM case_arrangement_evidence_links
                WHERE case_id=:case_id
                  AND arrangement_type=:arrangement_type
                  AND arrangement_confirmation_id
                        IS :arrangement_confirmation_id
                  AND guardianship_arrangement_id
                        IS :guardianship_arrangement_id
                  AND accommodation_arrangement_id
                        IS :accommodation_arrangement_id
                  AND case_record_link_id IS :case_record_link_id
                  AND document_id IS :document_id
                  AND validity_status='current'""",
            dict(values),
        )
    )


def insert_arrangement_evidence(
    connection: sqlite3.Connection, values: Mapping[str, Any]
) -> int:
    payload = dict(values)
    payload.setdefault(
        "evidence_id",
        int(
            connection.execute(
                """SELECT coalesce(max(id),0)+1
                   FROM case_arrangement_evidence_links"""
            ).fetchone()[0]
        ),
    )
    connection.execute(
        """INSERT INTO case_arrangement_evidence_links(
               id,case_id,arrangement_type,arrangement_confirmation_id,
               guardianship_arrangement_id,accommodation_arrangement_id,
               case_record_link_id,document_id,evidence_role,validity_status,
               effective_from,superseded_by,created_by,created_at
           ) VALUES(
               :evidence_id,:case_id,:arrangement_type,
               :arrangement_confirmation_id,:guardianship_arrangement_id,
               :accommodation_arrangement_id,:case_record_link_id,
               :document_id,:evidence_role,'current',:effective_from,
               NULL,:created_by,:created_at
           )""",
        payload,
    )
    return int(payload["evidence_id"])


def supersede_arrangement_evidence(
    connection: sqlite3.Connection,
    evidence_id: int,
    superseded_by: int,
) -> bool:
    cursor = connection.execute(
        """UPDATE case_arrangement_evidence_links
           SET validity_status='superseded',superseded_by=?
           WHERE id=? AND validity_status='current'""",
        (superseded_by, evidence_id),
    )
    return cursor.rowcount == 1


def mark_arrangement_evidence_historical(
    connection: sqlite3.Connection, evidence_id: int
) -> bool:
    cursor = connection.execute(
        """UPDATE case_arrangement_evidence_links
           SET validity_status='historical'
           WHERE id=? AND validity_status='current'""",
        (evidence_id,),
    )
    return cursor.rowcount == 1


def list_guardianship_evidence(
    connection: sqlite3.Connection,
    case_id: int,
    arrangement_id: int,
) -> list[dict[str, Any]]:
    return list_arrangement_evidence(
        connection,
        case_id,
        "guardianship",
        guardianship_arrangement_id=arrangement_id,
    )


def get_guardianship_evidence(
    connection: sqlite3.Connection,
    case_id: int,
    evidence_id: int,
) -> dict[str, Any] | None:
    row = get_arrangement_evidence(connection, case_id, evidence_id)
    return (
        row
        if row is not None and row["arrangement_type"] == "guardianship"
        else None
    )


insert_guardianship_evidence = insert_arrangement_evidence
supersede_guardianship_evidence = supersede_arrangement_evidence
mark_guardianship_evidence_historical = mark_arrangement_evidence_historical


def list_accommodation_evidence(
    connection: sqlite3.Connection,
    case_id: int,
    arrangement_id: int,
) -> list[dict[str, Any]]:
    return list_arrangement_evidence(
        connection,
        case_id,
        "accommodation",
        accommodation_arrangement_id=arrangement_id,
    )


def get_accommodation_evidence(
    connection: sqlite3.Connection,
    case_id: int,
    evidence_id: int,
) -> dict[str, Any] | None:
    row = get_arrangement_evidence(connection, case_id, evidence_id)
    return (
        row
        if row is not None and row["arrangement_type"] == "accommodation"
        else None
    )


insert_accommodation_evidence = insert_arrangement_evidence
supersede_accommodation_evidence = supersede_arrangement_evidence
mark_accommodation_evidence_historical = mark_arrangement_evidence_historical


def get_student_document(
    connection: sqlite3.Connection, document_id: int
) -> dict[str, Any] | None:
    return _one(
        connection.execute(
            "SELECT id AS document_id,student_id FROM student_documents WHERE id=?",
            (document_id,),
        )
    )
