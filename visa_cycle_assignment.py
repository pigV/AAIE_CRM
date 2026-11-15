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
