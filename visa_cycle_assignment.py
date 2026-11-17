"""Choose the exact active cycle owned by the student's Visa Case."""

def active_visa_cycles(connection, student_id):
    columns = {row[1] for row in connection.execute("PRAGMA table_info(application_cycles)")}
    if not {"id", "student_id", "status", "sequence_no", "cycle_type", "education_stream"}.issubset(columns):
        return ()
    return connection.execute(
        """SELECT id,sequence_no,cycle_type,education_stream FROM application_cycles
             WHERE student_id=? AND status='active' ORDER BY sequence_no,id""",
        (student_id,),
    ).fetchall()


def resolve_visa_cycle(connection, student_id, requested="", *, infer_single=True):
    cycles = active_visa_cycles(connection, student_id)
    if requested:
        try:
            selected = int(requested)
        except (TypeError, ValueError):
            raise ValueError("请选择有效的申请周期") from None
        if any(row[0] == selected for row in cycles):
            return selected
        raise ValueError("请选择该学生当前有效的申请周期")
    if not cycles:
        return None
    if len(cycles) == 1 and infer_single:
        return cycles[0][0]
    raise ValueError("请选择签证案件所属的申请周期")


def link_recorded_visa_grant(connection, student_id, case_id, requested_cycle, *, actor, timestamp):
    """Bind a locked grant to an active cycle and audit it in the same transaction."""
    if not requested_cycle:
        raise ValueError("请选择该签证所属的申请周期。")
    cycle_id = resolve_visa_cycle(connection, student_id, requested_cycle, infer_single=False)
    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE name='visa_result_status_current'"
    ).fetchone():
        raise ValueError("当前系统无法核对签证结果。")
    from dashboard_visa_metrics import is_student_visa_case

    case = connection.execute(
        """SELECT visa.* FROM visa_cases visa
             JOIN visa_result_status_current result ON result.visa_case_id=visa.id
            WHERE visa.id=? AND visa.student_id=? AND visa.archived_at IS NULL
              AND visa.application_cycle_id IS NULL AND visa.lodgement_recorded_at IS NOT NULL
              AND lower(result.effective_result)='granted' AND coalesce(result.conflict,0)=0""",
        (case_id, student_id),
    ).fetchone()
    if case is None or not is_student_visa_case(dict(case)):
        raise ValueError("只可补关联尚未关联周期且已确认下签的学生签证。")
    updated = connection.execute(
        """UPDATE visa_cases SET application_cycle_id=?
            WHERE id=? AND student_id=? AND application_cycle_id IS NULL AND archived_at IS NULL""",
        (cycle_id, case_id, student_id),
    )
    if updated.rowcount != 1:
        raise ValueError("签证案件已变更，请刷新后重试。")
    connection.execute(
        """INSERT INTO audit_logs(user_id,username,action_type,module_name,module_key,
                                   record_id,student_id,old_value,new_value,status,created_at)
           VALUES (?,?,'visa_application_cycle_linked','visa_cases','visa_cases',
                   ?,?,NULL,?,'success',?)""",
        (actor["id"], actor["username"], case_id, student_id, str(cycle_id), timestamp),
    )
    return cycle_id


_AUTO_LINK_COLUMNS = {
    "students": {"student_id", "deleted_at"},
    "application_cycles": {"id", "student_id", "status", "opened_at", "sequence_no",
                           "cycle_type", "education_stream"},
    "visa_cases": {"id", "student_id", "archived_at", "application_cycle_id",
                   "actual_lodgement_date", "lodgement_recorded_at"},
    "visa_result_status_current": {"visa_case_id", "effective_result", "conflict"},
    "visa_case_documents": {"visa_case_id", "student_document_id", "archived_at", "file_path"},
    "student_documents": {"id", "student_id", "document_type", "document_status",
                          "is_active", "voided_at", "file_path"},
    "audit_logs": {"user_id", "username", "action_type", "module_name", "module_key",
                   "record_id", "student_id", "old_value", "new_value", "status", "created_at"},
}


def reconcile_recorded_visa_grants(connection, *, timestamp, case_id=None):
    """Associate grants with a single provable owner in the caller's transaction."""
    from dashboard_business_time import business_date_iso
    from dashboard_visa_metrics import is_student_visa_case

    for table, required in _AUTO_LINK_COLUMNS.items():
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if not required <= columns:
            return 0

    case_filter = "" if case_id is None else "AND visa.id=?"
    candidates = connection.execute(
        f"""SELECT visa.*, cycle.id AS owner_cycle_id, cycle.opened_at AS owner_opened_at
              FROM visa_cases visa
              JOIN students student ON student.student_id=visa.student_id AND student.deleted_at IS NULL
              JOIN visa_result_status_current result ON result.visa_case_id=visa.id
              JOIN application_cycles cycle ON cycle.student_id=visa.student_id AND cycle.status='active'
             WHERE visa.archived_at IS NULL AND visa.application_cycle_id IS NULL
               AND visa.lodgement_recorded_at IS NOT NULL
               AND lower(result.effective_result)='granted' AND coalesce(result.conflict,0)=0
               AND NOT EXISTS (
                   SELECT 1 FROM application_cycles other
                    WHERE other.student_id=visa.student_id AND other.id<>cycle.id
               )
               AND NOT EXISTS (
                   SELECT 1 FROM visa_cases other
                   JOIN visa_result_status_current other_result ON other_result.visa_case_id=other.id
                    WHERE other.student_id=visa.student_id AND other.id<>visa.id
                      AND other.archived_at IS NULL AND lower(other_result.effective_result)='granted'
               )
               AND EXISTS (
                   SELECT 1 FROM visa_case_documents attachment
                   JOIN student_documents document ON document.id=attachment.student_document_id
                    WHERE attachment.visa_case_id=visa.id AND attachment.archived_at IS NULL
                      AND document.student_id=visa.student_id AND document.document_type='VisaGrantLetter'
                      AND document.is_active=1 AND document.document_status='active'
                      AND document.voided_at IS NULL AND document.file_path<>''
                      AND document.file_path=attachment.file_path
               )
               {case_filter}
             ORDER BY visa.id""",
        () if case_id is None else (case_id,),
    ).fetchall()
    linked = 0
    for candidate in candidates:
        if not is_student_visa_case(dict(candidate)):
            continue
        lodged = business_date_iso(candidate["actual_lodgement_date"])
        opened = business_date_iso(candidate["owner_opened_at"])
        if not lodged or not opened or lodged < opened:
            continue
        link_recorded_visa_grant(
            connection, candidate["student_id"], candidate["id"], candidate["owner_cycle_id"],
            actor={"id": None, "username": "System"}, timestamp=timestamp,
        )
        linked += 1
    return linked
