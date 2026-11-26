"""Current uploaded course evidence within one student and application cycle."""

import re
from dataclasses import dataclass

from revenue.school_courses import _provider_school_key, school_coe_course_stage


@dataclass(frozen=True)
class CourseEvidence:
    current: frozenset[int]
    replacements: dict[int, int]

    @property
    def historical(self):
        return self.replacements.keys()

COE_KINDS = {"COE", "COE_CAAW"}


def _columns(connection, table):
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}



def durable_replacements_available(connection):
    return "superseded_by_coe_id" in _columns(connection, "coe_oshc")


def current_replacement_id(evidence, coe_id):
    while coe_id in evidence.replacements:
        coe_id = evidence.replacements[coe_id]
    return coe_id if coe_id in evidence.current else None

def _name_key(text):
    return " ".join(str(text or "").casefold().split())


def name_matches_student(name, given, family):
    if not name or not given or not family:
        return True
    return _name_key(name) in {_name_key(f"{given} {family}"), _name_key(f"{family} {given}")}


def course_identity(row):
    """Do not conflate unidentified courses or similar courses at different providers."""
    provider = _provider_school_key(row["coe_provider"] or row["school_name"])
    course = _name_key(row["coe_course"])
    if not (provider and course):
        return None
    stage = school_coe_course_stage(course) if row["education_stream"] == "school_education" else ""
    code = re.search(r"\[([0-9]{5,7}[a-z])\]", course)
    if stage == "language":
        name = _name_key(re.sub(r"\s*\(elicos\)", "", course))
        if name in {"elicos", "english language intensive courses for overseas students"}:
            name = "english language"
        return provider, stage, code[1] if code else name
    if stage:
        return provider, stage
    if code:
        return provider, code[1]
    if course in {"secondary", "main", "main course", "主课"}:
        if not row["coe_start_date"]:
            return None
        return provider, course, row["coe_start_date"]
    return provider, course


def read_course_evidence(connection, *, student_id=None, cycle_ids=()):
    """Project saved uploads in ID order and retain records replaced by a later upload."""
    required = {"id", "student_id", "application_cycle_id", "document_kind", "file_path",
                "coe_provider", "school_name", "coe_course", "coe_start_date"}
    columns = _columns(connection, "coe_oshc")
    documents = _columns(connection, "student_documents")
    if not required <= columns or not {"id", "student_id", "file_path", "is_active"} <= documents:
        return CourseEvidence(frozenset(), {})
    filters = ["upper(coe.document_kind) IN ('COE','COE_CAAW')"]
    args = []
    if student_id is not None:
        filters.append("coe.student_id=?")
        args.append(student_id)
    if cycle_ids:
        filters.append("coe.application_cycle_id IN (" + ",".join("?" for _ in cycle_ids) + ")")
        args.extend(cycle_ids)
    if not args:
        return CourseEvidence(frozenset(), {})
    cycle_columns = _columns(connection, "application_cycles")
    stream = "cycle.education_stream" if "education_stream" in cycle_columns else "'school_education'"
    cycle_type = "cycle.cycle_type" if "cycle_type" in cycle_columns else "'initial'"
    cycle_join = (
        "LEFT JOIN application_cycles cycle ON cycle.id=coe.application_cycle_id AND cycle.student_id=coe.student_id"
        if "education_stream" in cycle_columns or "cycle_type" in cycle_columns else ""
    )
    successor = "coe.superseded_by_coe_id" if "superseded_by_coe_id" in columns else "NULL"
    name = "coe.student_name_on_coe" if "student_name_on_coe" in columns else "NULL"
    names = {"student_id", "passport_first_name", "passport_last_name"} <= _columns(connection, "students")
    student_join = "JOIN students student ON student.student_id=coe.student_id" if names else ""
    student_fields = "student.passport_first_name, student.passport_last_name" if names else "NULL AS passport_first_name, NULL AS passport_last_name"
    has_he_links = bool(_columns(connection, "higher_education_coe_links"))
    he_join = "LEFT JOIN higher_education_coe_links he ON he.coe_oshc_id=coe.id" if has_he_links else ""
    he_fields = "he.application_id, he.package_segment_id" if has_he_links else "NULL AS application_id, NULL AS package_segment_id"
    active = ["doc.is_active=1"]
    if "document_status" in documents:
        active.append("doc.document_status='active'")
    if "voided_at" in documents:
        active.append("doc.voided_at IS NULL")
    doc_kind = "AND upper(d.document_type) IN ('COE','COE_CAAW')" if "document_type" in documents else ""
    cursor = connection.execute(f"""
        SELECT coe.id, coe.student_id, coe.application_cycle_id, coe.coe_provider, coe.school_name,
               coe.coe_course, coe.coe_start_date, {stream} AS education_stream, {he_fields},
               {cycle_type} AS cycle_type, {successor} AS superseded_by_coe_id, {name} AS student_name_on_coe, {student_fields}, doc.id AS document_id,
               ({' AND '.join(active)}) AS is_active
          FROM coe_oshc coe
          {cycle_join} {student_join} {he_join}
          JOIN student_documents doc ON doc.id=(
             SELECT max(d.id) FROM student_documents d
              WHERE d.student_id=coe.student_id AND d.file_path=coe.file_path {doc_kind}
          )
         WHERE {' AND '.join(filters)}
         ORDER BY coe.id DESC
    """, args)
    fields = [item[0] for item in cursor.description]
    current, replacements, seen = set(), {}, {}
    for values in cursor:
        row = dict(zip(fields, values))
        if row["superseded_by_coe_id"] is not None:
            replacements[row["id"]] = row["superseded_by_coe_id"]
            continue
        if row["cycle_type"] not in {"initial", None}:
            # Transfer and progression already own explicit Branch/pathway links.
            continue
        if not name_matches_student(
            row["student_name_on_coe"], row["passport_first_name"], row["passport_last_name"],
        ):
            continue
        identity = course_identity(row)
        if identity is not None and row["application_cycle_id"] is not None:
            key = (row["student_id"], row["application_cycle_id"], row["application_id"],
                   row["package_segment_id"], identity)
            if key in seen:
                replacements[row["id"]] = seen[key]
                continue
            seen[key] = row["id"]
        if row["is_active"]:
            current.add(row["id"])
    return CourseEvidence(frozenset(current), replacements)


def preserve_course_replacements(connection, student_id, coe_id):
    """Freeze prior versions when a new upload wins or the latest record is edited."""
    evidence = read_course_evidence(connection, student_id=student_id)
    if durable_replacements_available(connection):
        connection.executemany(
            """UPDATE coe_oshc SET superseded_by_coe_id=?
               WHERE id=? AND student_id=? AND superseded_by_coe_id IS NULL""",
            [(current, previous, student_id) for previous, current in evidence.replacements.items()
             if current == int(coe_id)],
        )
    return evidence


def require_editable_course_evidence(connection, student_id, coe_id):
    evidence = read_course_evidence(connection, student_id=student_id)
    if int(coe_id) in evidence.historical:
        raise ValueError("这份 CoE 已由同一课程的新文件替代，历史记录仅供查阅。")
    return evidence
