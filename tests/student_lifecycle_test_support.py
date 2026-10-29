"""Public-API lifecycle helpers shared by Transfer and Progression tests."""

from contextlib import closing
import sqlite3

from security import now_iso
from student_lifecycle.case_enrolment import bind_case_current_enrolment
from student_lifecycle.enrolment import SchoolEnrolmentService
from student_lifecycle.service import StudentLifecycleService


def lifecycle_service(database_path):
    return StudentLifecycleService(database_path, clock=now_iso)


def _ensure_case_current(service, case_id, lifecycle_type, actor_user_id):
    database_path = service.database_path
    with closing(sqlite3.connect(database_path)) as connection:
        row = connection.execute(
            "SELECT student_id FROM student_cases WHERE id=?", (int(case_id),)
        ).fetchone()
        student_id = str(row[0])
        current = connection.execute(
            """SELECT id FROM student_school_enrolments
                WHERE student_id=? AND enrolment_role='current' AND status='active'""",
            (student_id,),
        ).fetchone()
        table = "transfer_cases" if lifecycle_type == "transfer" else "progression_cases"
        snapshot = connection.execute(
            f"SELECT current_school_name_snapshot,current_school_id FROM {table} WHERE case_id=?",
            (int(case_id),),
        ).fetchone()
    if current is None:
        current = SchoolEnrolmentService(database_path).confirm_current(
            student_id=student_id,
            school_id=snapshot[1],
            school_name_snapshot=str(snapshot[0]),
            actor_user_id=actor_user_id,
            reason="test fixture canonical Current",
        )
        current_id = current.enrolment_id
    else:
        current_id = int(current[0])
        with closing(sqlite3.connect(database_path)) as connection:
            connection.execute(
                """UPDATE student_school_enrolments
                      SET school_id=?,school_name_snapshot=?
                    WHERE id=?""",
                (snapshot[1], str(snapshot[0]), current_id),
            )
            connection.commit()
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        bind_case_current_enrolment(
            connection, case_id=case_id, lifecycle_type=lifecycle_type
        )
        connection.commit()
    return current_id


def _ensure_transfer_contract(service, draft, actor_user_id):
    if draft.source_contract_id is not None:
        return draft.source_contract_id
    with closing(sqlite3.connect(service.database_path)) as connection:
        row = connection.execute(
            """SELECT contract.id FROM contracts AS contract
                JOIN student_cases AS student_case ON student_case.student_id=contract.student_id
                WHERE student_case.id=? AND contract.application_cycle_id IS NULL
                  AND NOT EXISTS(
                    SELECT 1 FROM transfer_cases
                    WHERE source_contract_id=contract.id)
                ORDER BY contract.id LIMIT 1""",
            (int(draft.case_id),),
        ).fetchone()
        if row is None:
            case_student = connection.execute(
                "SELECT student_id FROM student_cases WHERE id=?",
                (int(draft.case_id),),
            ).fetchone()[0]
            columns = {
                str(item[1]): item
                for item in connection.execute("PRAGMA table_info(contracts)")
            }
            if "final_signed_file_path" not in columns:
                connection.execute(
                    "ALTER TABLE contracts ADD COLUMN final_signed_file_path TEXT"
                )
                columns = {
                    str(item[1]): item
                    for item in connection.execute("PRAGMA table_info(contracts)")
                }
            values = {
                "student_id": case_student,
                "contract_category": "Transfer",
                "contract_type": "TransferApplicationContract",
                "contract_title": "Test Transfer Contract",
                "contract_status": "FullyExecuted",
                "service_scope_json": "[]",
                "is_standalone_contract": 0,
                "updated_at": now_iso(),
                "deleted_at": None,
                "voided_at": None,
                "application_cycle_id": None,
                "final_signed_file_path": "/test/final-transfer-contract.pdf",
            }
            insertable = {
                key: value for key, value in values.items() if key in columns
            }
            names = ",".join(insertable)
            marks = ",".join("?" for _ in insertable)
            contract_id = int(connection.execute(
                f"INSERT INTO contracts({names}) VALUES({marks})",
                tuple(insertable.values()),
            ).lastrowid)
        else:
            contract_id = int(row[0])
            columns = {
                str(item[1]): item
                for item in connection.execute("PRAGMA table_info(contracts)")
            }
            if "final_signed_file_path" not in columns:
                connection.execute(
                    "ALTER TABLE contracts ADD COLUMN final_signed_file_path TEXT"
                )
            connection.execute(
                """UPDATE contracts SET contract_type='TransferApplicationContract',
                                          contract_status='FullyExecuted',
                                          deleted_at=NULL,voided_at=NULL,
                                          final_signed_file_path=CASE
                                              WHEN final_signed_file_path IS NULL
                                                OR TRIM(final_signed_file_path)=''
                                              THEN '/test/final-transfer-contract.pdf'
                                              ELSE final_signed_file_path
                                          END
                    WHERE id=?""",
                (contract_id,),
            )
        connection.commit()
    service.ensure_contract_ownership(
        draft.case_id,
        source_contract_id=contract_id,
        actor_user_id=actor_user_id,
    )
    return contract_id


def activate_transfer(service, draft, *, actor_user_id=1, key=None):
    _ensure_case_current(service, draft.case_id, "transfer", actor_user_id)
    _ensure_transfer_contract(service, draft, actor_user_id)
    return service.activate_transfer_case(
        draft.case_id,
        actor_user_id=actor_user_id,
        idempotency_key=key or f"test-activate-transfer-{draft.case_id}",
    )


def activate_progression(service, draft, *, actor_user_id=1, key=None):
    _ensure_case_current(service, draft.case_id, "progression", actor_user_id)
    return service.activate_case(
        draft.case_id,
        actor_user_id=actor_user_id,
        idempotency_key=key or f"test-activate-progression-{draft.case_id}",
    )


def close_canonical(
    database_path, case_id, *, outcome="completed", actor_user_id=1, key=None
):
    return lifecycle_service(database_path).close_lifecycle(
        case_id=case_id,
        outcome=outcome,
        actor_user_id=actor_user_id,
        idempotency_key=key or f"test-close-lifecycle-{case_id}-{outcome}",
    )
