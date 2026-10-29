"""Deterministic, read-only historical workflow backfill preview."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from pathlib import Path
import sqlite3

from workflow_offer_events import resolve_offer_received_identity


class BackfillClassification(str, Enum):
    SAFE = "SAFE"
    AMBIGUOUS = "AMBIGUOUS"
    MISSING_SOURCE_FACT = "MISSING_SOURCE_FACT"


@dataclass(frozen=True)
class BackfillProposal:
    action: str
    target: str
    values: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class BackfillItem:
    classification: BackfillClassification
    subject_kind: str
    source_ids: tuple[str, ...]
    rule_id: str
    evidence: tuple[str, ...]
    proposals: tuple[BackfillProposal, ...]
    reason: str


@dataclass(frozen=True)
class BackfillPreview:
    items: tuple[BackfillItem, ...]

    def _of(self, classification: BackfillClassification) -> tuple[BackfillItem, ...]:
        return tuple(item for item in self.items if item.classification == classification)

    @property
    def safe(self) -> tuple[BackfillItem, ...]:
        return self._of(BackfillClassification.SAFE)

    @property
    def ambiguous(self) -> tuple[BackfillItem, ...]:
        return self._of(BackfillClassification.AMBIGUOUS)

    @property
    def missing_source_fact(self) -> tuple[BackfillItem, ...]:
        return self._of(BackfillClassification.MISSING_SOURCE_FACT)


@dataclass(frozen=True)
class BackfillApplyResult:
    preview_sha256: str
    inserted_cycles: int
    updated_cycles: int
    linked_records: int
    materialized_applications: int
    materialized_events: int
    unchanged_records: int
    skipped_ambiguous: int
    skipped_missing_source_fact: int

    def to_json(self) -> str:
        return json.dumps(self.__dict__, indent=2, sort_keys=True) + "\n"


_OWNED_TABLES = (
    "contracts",
    "school_plan_reports",
    "offers",
    "coe_oshc",
    "visa_cases",
)
_EVIDENCE_TABLES = ("parent_form_tokens", *_OWNED_TABLES)


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _student_rows(conn: sqlite3.Connection, tables: set[str]):
    columns = _columns(conn, "students")
    deleted = " AND coalesce(deleted_at,'')=''" if "deleted_at" in columns else ""
    return conn.execute(
        f"SELECT student_id FROM students WHERE coalesce(student_id,'')<>''{deleted} ORDER BY student_id"
    ).fetchall()


def _evidence_for_student(
    conn: sqlite3.Connection, tables: set[str], student_id: str
) -> tuple[str, ...]:
    evidence: list[str] = []
    for table in _EVIDENCE_TABLES:
        if table not in tables or "student_id" not in _columns(conn, table):
            continue
        rows = conn.execute(
            f"SELECT count(*) FROM {table} WHERE student_id=?", (student_id,)
        ).fetchone()
        if rows and int(rows[0]):
            evidence.append(f"{table}:{int(rows[0])}")
    return tuple(evidence)


def _case_candidates(conn: sqlite3.Connection, tables: set[str], student_id: str):
    if "student_cases" not in tables:
        return ()
    return tuple(
        (int(row[0]), str(row[1]).strip().lower())
        for row in conn.execute(
            """
            SELECT id,case_type FROM student_cases
            WHERE student_id=? AND lower(trim(case_type)) IN ('transfer','progression')
            ORDER BY id
            """,
            (student_id,),
        ).fetchall()
    )


def _existing_candidates(conn: sqlite3.Connection, tables: set[str], student_id: str):
    if "application_cycles" not in tables:
        return ()
    return tuple(
        f"cycle:{int(row[0])}"
        for row in conn.execute(
            "SELECT id FROM application_cycles WHERE student_id=? ORDER BY sequence_no,id",
            (student_id,),
        ).fetchall()
    )


def _existing_case_ids(conn: sqlite3.Connection, tables: set[str], student_id: str) -> set[int]:
    if "application_cycles" not in tables:
        return set()
    return {
        int(row[0])
        for row in conn.execute(
            """
            SELECT source_student_case_id FROM application_cycles
            WHERE student_id=? AND source_student_case_id IS NOT NULL
            """,
            (student_id,),
        ).fetchall()
    }


def _safe_item(subject, ids, rule, evidence, proposal, reason) -> BackfillItem:
    return BackfillItem(
        BackfillClassification.SAFE,
        subject,
        tuple(ids),
        rule,
        tuple(evidence),
        (proposal,),
        reason,
    )


def _legacy_terminal_grant(
    conn: sqlite3.Connection, tables: set[str], student_id: str
) -> tuple[str | None, tuple[str, ...], bool]:
    """Return one explicit legacy Grant date, its evidence, and conflict state."""

    if "visas" not in tables:
        return None, (), False
    columns = _columns(conn, "visas")
    if not {"id", "student_id", "visa_status", "visa_grant_date"}.issubset(columns):
        return None, (), False
    rows = conn.execute(
        """SELECT id,lower(trim(coalesce(visa_status,''))),trim(coalesce(visa_grant_date,''))
             FROM visas WHERE student_id=? ORDER BY id""",
        (student_id,),
    ).fetchall()
    meaningful = [row for row in rows if str(row[1]) or str(row[2])]
    if not meaningful:
        return None, (), False
    statuses = {str(row[1]) for row in meaningful if str(row[1])}
    dates = {str(row[2]) for row in meaningful if str(row[2])}
    evidence = tuple(
        f"visas:{int(row[0])}:status={str(row[1]) or 'blank'}:grant_date={str(row[2]) or 'blank'}"
        for row in meaningful
    )
    if statuses == {"granted"} and len(dates) == 1:
        return next(iter(dates)), evidence, False
    if "granted" in statuses or dates:
        return None, evidence, True
    return None, evidence, False


def build_backfill_preview(conn: sqlite3.Connection) -> BackfillPreview:
    """Inspect legacy facts without writing or inventing absent business events."""

    tables = _tables(conn)
    items: list[BackfillItem] = []
    if "parent_form_tokens" in tables:
        token_columns = _columns(conn, "parent_form_tokens")
        if {"token", "created_at"}.issubset(token_columns):
            student_filter = (
                "AND coalesce(student_id,'')=''" if "student_id" in token_columns else ""
            )
            submitted_filter = (
                "AND submitted_at IS NULL" if "submitted_at" in token_columns else ""
            )
            invalidated_filter = (
                "AND invalidated_at IS NULL" if "invalidated_at" in token_columns else ""
            )
            expiry_filter = (
                "AND expires_at>(SELECT max(created_at) FROM parent_form_tokens)"
                if "expires_at" in token_columns
                else ""
            )
            existing_filter = (
                "AND NOT EXISTS(SELECT 1 FROM application_cycles cycle "
                "WHERE cycle.parent_form_token=parent_form_tokens.token)"
                if "application_cycles" in tables
                else ""
            )
            for row in conn.execute(
                f"""SELECT token,created_at FROM parent_form_tokens
                     WHERE coalesce(created_at,'')<>'' {student_filter} {submitted_filter}
                       {invalidated_filter} {expiry_filter} {existing_filter}
                     ORDER BY created_at,token"""
            ).fetchall():
                token, created_at = str(row[0]), str(row[1])
                items.append(
                    _safe_item(
                        "workflow_case",
                        (token,),
                        "UNSUBMITTED_PARENT_FORM_WORKFLOW_CYCLE",
                        ("parent_form_token:unsubmitted", f"created_at:{created_at}"),
                        BackfillProposal(
                            "insert",
                            "application_cycles",
                            (
                                ("parent_form_token", token),
                                ("cycle_type", "initial"),
                                ("opened_at", created_at),
                            ),
                        ),
                        "A valid unsubmitted Parent Form is an auditable Stage 01 workflow case.",
                    )
                )
    for student_row in _student_rows(conn, tables):
        student_id = str(student_row[0])
        evidence = _evidence_for_student(conn, tables, student_id)
        cases = _case_candidates(conn, tables, student_id)
        existing = _existing_candidates(conn, tables, student_id)
        existing_case_ids = _existing_case_ids(conn, tables, student_id)
        candidates: list[str] = list(existing)
        pending_cycles: list[tuple[tuple[str, ...], str, tuple[str, ...], BackfillProposal, str]] = []
        if not existing and evidence:
            initial_key = f"proposal:initial:{student_id}"
            candidates.append(initial_key)
            pending_cycles.append(
                (
                    (student_id,),
                    "LEGACY_INITIAL_CYCLE_EVIDENCE",
                    evidence,
                    BackfillProposal(
                        "insert",
                        "application_cycles",
                        (("student_id", student_id), ("cycle_type", "initial")),
                    ),
                    "Legacy workflow records establish one initial cycle candidate.",
                )
            )
        elif not existing and not evidence and not cases:
            items.append(
                BackfillItem(
                    BackfillClassification.MISSING_SOURCE_FACT,
                    "application_cycle",
                    (student_id,),
                    "NO_WORKFLOW_CYCLE_EVIDENCE",
                    (),
                    (),
                    "No source business fact safely establishes an application cycle.",
                )
            )

        for case_id, case_type in cases:
            if case_id in existing_case_ids:
                continue
            key = f"proposal:{case_type}:case:{case_id}"
            candidates.append(key)
            pending_cycles.append(
                (
                    (student_id, str(case_id)),
                    "LEGACY_CASE_EXPLICIT_CYCLE",
                    (f"student_cases:{case_id}", f"case_type:{case_type}"),
                    BackfillProposal(
                        "insert",
                        "application_cycles",
                        (
                            ("student_id", student_id),
                            ("cycle_type", case_type),
                            ("source_student_case_id", str(case_id)),
                        ),
                    ),
                    "An explicit Transfer/Progression case establishes its own cycle.",
                )
            )

        active_existing = bool(
            existing
            and conn.execute(
                "SELECT 1 FROM application_cycles WHERE student_id=? AND status='active'",
                (student_id,),
            ).fetchone()
        )
        dates_available = bool(_date_candidates(conn, student_id))
        cycle_conflict = active_existing or len(pending_cycles) > 1
        for source_ids, rule_id, proposal_evidence, proposal, reason in pending_cycles:
            if not dates_available:
                items.append(
                    BackfillItem(
                        BackfillClassification.MISSING_SOURCE_FACT,
                        "application_cycle",
                        source_ids,
                        "APPLICATION_CYCLE_OPEN_DATE_MISSING",
                        proposal_evidence,
                        (),
                        "No reliable source date can open this Application Cycle.",
                    )
                )
            elif cycle_conflict:
                items.append(
                    BackfillItem(
                        BackfillClassification.AMBIGUOUS,
                        "application_cycle",
                        source_ids,
                        (
                            "ACTIVE_CYCLE_CONFLICTS_WITH_EXPLICIT_CASE"
                            if active_existing and rule_id == "LEGACY_CASE_EXPLICIT_CYCLE"
                            else "MULTIPLE_PLAUSIBLE_CYCLES"
                        ),
                        proposal_evidence + tuple(existing),
                        (),
                        (
                            "An active Application Cycle already exists; the explicit case "
                            "requires a manual close/current-cycle decision."
                            if active_existing and rule_id == "LEGACY_CASE_EXPLICIT_CYCLE"
                            else "Multiple cycle candidates lack deterministic ordering or closure facts."
                        ),
                    )
                )
            else:
                items.append(
                    _safe_item(
                        "application_cycle",
                        source_ids,
                        rule_id,
                        proposal_evidence,
                        proposal,
                        reason,
                    )
                )

        grant_date, terminal_evidence, terminal_conflict = _legacy_terminal_grant(
            conn, tables, student_id
        )
        if grant_date and len(candidates) == 1:
            items.append(
                _safe_item(
                    "application_cycle_state",
                    (student_id,),
                    "LEGACY_GRANTED_CYCLE_CLOSURE",
                    terminal_evidence + (candidates[0],),
                    BackfillProposal(
                        "update",
                        "application_cycles",
                        (
                            ("cycle_candidate", candidates[0]),
                            ("status", "closed"),
                            ("outcome", "granted"),
                            ("closed_at", grant_date),
                        ),
                    ),
                    "Consistent explicit legacy Visa Grant facts close the unique Cycle without creating a Grant Letter event.",
                )
            )
        elif terminal_conflict or (grant_date and len(candidates) != 1):
            items.append(
                BackfillItem(
                    BackfillClassification.AMBIGUOUS,
                    "application_cycle_state",
                    (student_id,),
                    "CONFLICTING_LEGACY_TERMINAL_FACTS",
                    terminal_evidence + tuple(candidates),
                    (),
                    "Legacy terminal facts or Cycle ownership are not unique enough for automatic closure.",
                )
            )

        for table in _OWNED_TABLES:
            if table not in tables:
                continue
            columns = _columns(conn, table)
            if not {"id", "student_id"}.issubset(columns):
                continue
            ownership_filter = (
                " AND application_cycle_id IS NULL"
                if "application_cycle_id" in columns
                else ""
            )
            rows = conn.execute(
                f"SELECT id FROM {table} WHERE student_id=?{ownership_filter} ORDER BY id",
                (student_id,),
            ).fetchall()
            for row in rows:
                record_id = str(row[0])
                source_ids = (student_id, record_id)
                record_evidence = (f"{table}:{record_id}", f"student_id:{student_id}")
                if len(candidates) == 1:
                    items.append(
                        _safe_item(
                            table,
                            source_ids,
                            "UNAMBIGUOUS_SINGLE_CYCLE_OWNERSHIP",
                            record_evidence + (candidates[0],),
                            BackfillProposal(
                                "link",
                                table,
                                (("id", record_id), ("cycle_candidate", candidates[0])),
                            ),
                            "Exactly one evidence-backed cycle can own this legacy record.",
                        )
                    )
                elif len(candidates) > 1:
                    items.append(
                        BackfillItem(
                            BackfillClassification.AMBIGUOUS,
                            table,
                            source_ids,
                            "MULTIPLE_PLAUSIBLE_CYCLES",
                            record_evidence + tuple(candidates),
                            (),
                            "Multiple cycles could own this record; manual review is required.",
                        )
                    )
                else:
                    items.append(
                        BackfillItem(
                            BackfillClassification.MISSING_SOURCE_FACT,
                            table,
                            source_ids,
                            "NO_CYCLE_OWNERSHIP_EVIDENCE",
                            record_evidence,
                            (),
                            "No evidence-backed cycle can own this legacy record.",
                        )
                    )

        if "coe_oshc" in tables:
            coe_columns = _columns(conn, "coe_oshc")
            kind_filter = (
                " AND upper(coalesce(document_kind,''))='COE'"
                if "document_kind" in coe_columns
                else ""
            )
            for row in conn.execute(
                f"SELECT id FROM coe_oshc WHERE student_id=?{kind_filter} ORDER BY id",
                (student_id,),
            ).fetchall():
                items.append(
                    BackfillItem(
                        BackfillClassification.MISSING_SOURCE_FACT,
                        "final_school_submission_event",
                        (student_id, str(row[0])),
                        "FINAL_SCHOOL_SUBMISSION_NOT_RECORDED",
                        (f"coe_oshc:{row[0]}",),
                        (),
                        "COE presence cannot prove when acceptance documents were submitted back.",
                    )
                )

    rank = {
        BackfillClassification.SAFE: 0,
        BackfillClassification.AMBIGUOUS: 1,
        BackfillClassification.MISSING_SOURCE_FACT: 2,
    }
    items.sort(
        key=lambda item: (
            rank[item.classification],
            item.subject_kind,
            item.source_ids,
            item.rule_id,
        )
    )
    return BackfillPreview(tuple(items))


def _proposal_dict(proposal: BackfillProposal) -> dict:
    values = dict(proposal.values)
    if "parent_form_token" in values:
        values["parent_form_token_sha256_12"] = hashlib.sha256(
            values.pop("parent_form_token").encode("utf-8")
        ).hexdigest()[:12]
    return {
        "action": proposal.action,
        "target": proposal.target,
        "values": values,
    }


def _item_dict(item: BackfillItem) -> dict:
    source_ids = list(item.source_ids)
    if item.subject_kind == "workflow_case":
        source_ids = [
            "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
            for value in source_ids
        ]
    return {
        "classification": item.classification.value,
        "subject_kind": item.subject_kind,
        "source_ids": source_ids,
        "rule_id": item.rule_id,
        "evidence": list(item.evidence),
        "proposals": [_proposal_dict(proposal) for proposal in item.proposals],
        "reason": item.reason,
    }


def preview_to_json(preview: BackfillPreview) -> str:
    payload = {
        "counts": {
            "safe": len(preview.safe),
            "ambiguous": len(preview.ambiguous),
            "missing_source_fact": len(preview.missing_source_fact),
        },
        "items": [_item_dict(item) for item in preview.items],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def preview_to_markdown(preview: BackfillPreview) -> str:
    lines = [
        "# Historical Workflow Backfill Preview",
        "",
        "This report is read-only. Only SAFE / DETERMINISTIC items contain proposals.",
        "",
        f"- SAFE / DETERMINISTIC: {len(preview.safe)}",
        f"- AMBIGUOUS: {len(preview.ambiguous)}",
        f"- MISSING SOURCE FACT: {len(preview.missing_source_fact)}",
        "",
    ]
    for item in preview.items:
        ids = (
            ", ".join(
                "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
                for value in item.source_ids
            )
            if item.subject_kind == "workflow_case"
            else ", ".join(item.source_ids)
        )
        lines.extend(
            [
                f"## {item.classification.value}: {item.subject_kind} [{ids}]",
                "",
                f"- Rule: `{item.rule_id}`",
                f"- Reason: {item.reason}",
                f"- Evidence: {', '.join(item.evidence) or 'none'}",
                f"- Automatic proposals: {len(item.proposals)}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


_DEFAULT_FORMAL_DATABASE = Path(__file__).resolve().parent / "database" / "aaie_crm.db"


def _date_candidates(conn: sqlite3.Connection, student_id: str) -> tuple[str, ...]:
    tables = _tables(conn)
    values: list[str] = []
    sources = (
        ("students", ("created_date", "created_at")),
        ("parent_form_tokens", ("created_at", "submitted_at")),
        ("contracts", ("created_at", "signed_at", "fully_executed_at")),
        ("school_plan_reports", ("created_at", "updated_at", "confirmed_at")),
        ("offers", ("uploaded_at", "offer_date")),
        ("coe_oshc", ("uploaded_at", "created_at")),
        ("visa_cases", ("created_at", "actual_lodgement_date")),
    )
    for table, names in sources:
        if table not in tables or "student_id" not in _columns(conn, table):
            continue
        available = [name for name in names if name in _columns(conn, table)]
        if not available:
            continue
        for row in conn.execute(
            f"SELECT {','.join(available)} FROM {table} WHERE student_id=?",
            (student_id,),
        ).fetchall():
            values.extend(
                str(value).strip()
                for value in row
                if value is not None and str(value).strip()
            )
    return tuple(sorted(values))


def _stable_cycle_uid(values: dict[str, str]) -> str:
    if values.get("parent_form_token"):
        token_hash = hashlib.sha256(values["parent_form_token"].encode("utf-8")).hexdigest()[:24]
        return f"legacy-parent-form-{token_hash}"
    case_id = values.get("source_student_case_id")
    return (
        f"legacy-{values['cycle_type']}-case-{case_id}"
        if case_id
        else f"legacy-{values['cycle_type']}-student-{values['student_id']}"
    )


def _apply_parent_form_cycle(
    conn: sqlite3.Connection, values: dict[str, str], actor: str
) -> bool:
    token = values["parent_form_token"]
    if conn.execute(
        "SELECT 1 FROM application_cycles WHERE parent_form_token=?", (token,)
    ).fetchone():
        return False
    opened_at = values["opened_at"]
    conn.execute(
        """INSERT INTO application_cycles(
             cycle_uid,parent_form_token,cycle_type,sequence_no,status,opened_at,
             created_by_name,created_at,updated_by_name,updated_at
           ) VALUES(?,?,'initial',1,'active',?,?,?,?,?)""",
        (
            _stable_cycle_uid(values), token, opened_at, actor, opened_at, actor, opened_at,
        ),
    )
    return True


def _apply_cycle(
    conn: sqlite3.Connection, values: dict[str, str], actor: str
) -> tuple[int | None, bool]:
    uid = _stable_cycle_uid(values)
    row = conn.execute("SELECT id FROM application_cycles WHERE cycle_uid=?", (uid,)).fetchone()
    if row is not None:
        return int(row[0]), False
    student_id = values["student_id"]
    dates = _date_candidates(conn, student_id)
    if not dates:
        return None, False
    sequence_no = int(
        conn.execute(
            "SELECT coalesce(max(sequence_no),0)+1 FROM application_cycles WHERE student_id=?",
            (student_id,),
        ).fetchone()[0]
    )
    cursor = conn.execute(
        """INSERT INTO application_cycles(
             cycle_uid,student_id,source_student_case_id,cycle_type,sequence_no,status,
             opened_at,created_by_name,created_at,updated_by_name,updated_at
           ) VALUES(?,?,?,?,?,'active',?,?,?,?,?)""",
        (
            uid,
            student_id,
            int(values["source_student_case_id"])
            if values.get("source_student_case_id")
            else None,
            values["cycle_type"],
            sequence_no,
            dates[0],
            actor,
            dates[0],
            actor,
            dates[0],
        ),
    )
    return int(cursor.lastrowid), True


def _application_status(result_status: object) -> str:
    value = str(result_status or "").strip().lower()
    return {
        "offer_received": "offer_received",
        "offer": "offer_received",
        "rejected": "rejected",
        "withdrawn": "withdrawn",
        "waiting_list": "waiting_list",
    }.get(value, "submitted")


def _materialize_submitted_applications(conn: sqlite3.Connection) -> int:
    tables = _tables(conn)
    required = {
        "school_plan_school_submission_events",
        "school_plan_confirmed_schools",
        "school_plan_reports",
        "public_application_groups",
        "public_application_preferences",
        "private_school_applications",
    }
    if not required.issubset(tables):
        return 0
    selected_columns = _columns(conn, "school_plan_confirmed_schools")
    selected_clause = " AND school.is_selected=1" if "is_selected" in selected_columns else ""
    rows = conn.execute(
        f"""SELECT report.id,report.application_cycle_id,report.student_id,
                   lower(trim(coalesce(report.plan_sector,''))),report.city_id,
                   school.id,school.school_id,school.school_name_snapshot,
                   school.result_status,event.submitted_at,event.submitted_by_user_id,
                   event.submitted_by_name,school.confirmation_id
              FROM school_plan_reports report
              JOIN school_plan_confirmed_schools school ON school.report_id=report.id
              JOIN school_plan_school_submission_events event
                ON event.confirmed_school_id=school.id
               AND event.new_submission_status='submitted'
             WHERE report.application_cycle_id IS NOT NULL
               AND report.cycle_is_current=1
               AND event.submitted_at IS NOT NULL{selected_clause}
             ORDER BY report.id,school.id,event.submitted_at,event.id"""
    ).fetchall()
    by_school: dict[int, sqlite3.Row] = {}
    for row in rows:
        by_school.setdefault(int(row[5]), row)
    inserted = 0
    public_groups: dict[int, int] = {}
    for row in by_school.values():
        report_id, cycle_id, student_id, sector, city_id = row[:5]
        confirmed_school_id, school_id, school_name, result_status = row[5:9]
        submitted_at, user_id, user_name, confirmation_id = row[9:13]
        if sector == "public":
            group_id = public_groups.get(int(report_id))
            if group_id is None:
                existing = conn.execute(
                    "SELECT id FROM public_application_groups WHERE application_cycle_id=? AND report_id=?",
                    (cycle_id, report_id),
                ).fetchone()
                if existing is None:
                    cursor = conn.execute(
                        """INSERT INTO public_application_groups(
                             application_cycle_id,student_id,city_id,report_id,confirmation_id,
                             status,submitted_at,submitted_by_user_id,submitted_by_name,
                             created_by_user_id,created_at,updated_at
                           ) VALUES(?,?,?,?,?,'submitted',?,?,?,?,?,?)""",
                        (
                            cycle_id, student_id, city_id, report_id, confirmation_id,
                            submitted_at, user_id, user_name, user_id, submitted_at, submitted_at,
                        ),
                    )
                    group_id = int(cursor.lastrowid)
                    inserted += 1
                else:
                    group_id = int(existing[0])
                public_groups[int(report_id)] = group_id
            cursor = conn.execute(
                """INSERT OR IGNORE INTO public_application_preferences(
                     public_application_group_id,confirmed_school_id,school_id,
                     school_name_snapshot,application_status,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?)""",
                (
                    group_id, confirmed_school_id, school_id, school_name,
                    _application_status(result_status), submitted_at, submitted_at,
                ),
            )
            inserted += int(cursor.rowcount)
        elif sector == "private":
            cursor = conn.execute(
                """INSERT OR IGNORE INTO private_school_applications(
                     application_cycle_id,student_id,confirmed_school_id,school_id,
                     school_name_snapshot,city_id,status,submitted_at,
                     submitted_by_user_id,submitted_by_name,created_by_user_id,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    cycle_id, student_id, confirmed_school_id, school_id, school_name, city_id,
                    _application_status(result_status), submitted_at, user_id, user_name,
                    user_id, submitted_at, submitted_at,
                ),
            )
            inserted += int(cursor.rowcount)
    return inserted


def _materialize_explicit_acceptances(conn: sqlite3.Connection) -> int:
    tables = _tables(conn)
    required = {
        "student_offer_acceptances",
        "offers",
        "application_cycles",
        "offer_received_events",
        "cycle_final_acceptances",
    }
    if not required.issubset(tables):
        return 0
    acceptance_columns = _columns(conn, "student_offer_acceptances")
    offer_columns = _columns(conn, "offers")
    option = lambda table, columns, name: (
        f"{table}.{name}" if name in columns else f"NULL AS {name}"
    )
    rows = conn.execute(
        f"""SELECT acceptance.student_id,acceptance.offer_id,
                   {option('acceptance', acceptance_columns, 'accepted_at')},
                   {option('acceptance', acceptance_columns, 'accepted_by_user_id')},
                   {option('acceptance', acceptance_columns, 'accepted_by_name')},
                   offer.application_cycle_id,offer.school_id,offer.school_name,
                   {option('offer', offer_columns, 'uploaded_at')},
                   {option('offer', offer_columns, 'signed_offer_file_path')},
                   {option('offer', offer_columns, 'payment_receipt_file_path')},
                   {option('offer', offer_columns, 'deposit_paid_date')}
              FROM student_offer_acceptances acceptance
              JOIN offers offer ON offer.id=acceptance.offer_id
                              AND offer.student_id=acceptance.student_id
             WHERE offer.application_cycle_id IS NOT NULL
             ORDER BY acceptance.student_id,acceptance.offer_id"""
    ).fetchall()
    inserted = 0
    for row in rows:
        (
            student_id, offer_id, accepted_at, user_id, user_name, cycle_id,
            school_id, school_name, uploaded_at, signed_path, receipt_path, deposit_date,
        ) = tuple(row)
        timestamp = str(uploaded_at or accepted_at or "").strip()
        selected_at = str(accepted_at or uploaded_at or "").strip()
        if not timestamp or not selected_at:
            continue
        private = conn.execute(
            """SELECT id FROM private_school_applications
                 WHERE application_cycle_id=?
                   AND lower(trim(school_name_snapshot))=lower(trim(?))
                 ORDER BY id""",
            (cycle_id, school_name),
        ).fetchall()
        public = conn.execute(
            """SELECT DISTINCT group_.id
                 FROM public_application_groups group_
                 LEFT JOIN public_application_preferences preference
                   ON preference.public_application_group_id=group_.id
                WHERE group_.application_cycle_id=?
                  AND (lower(trim(preference.school_name_snapshot))=lower(trim(?))
                       OR NOT EXISTS(
                         SELECT 1 FROM public_application_groups other
                         WHERE other.application_cycle_id=group_.application_cycle_id
                           AND other.id<>group_.id
                       ))
                ORDER BY group_.id""",
            (cycle_id, school_name),
        ).fetchall()
        candidates = [("private", int(item[0])) for item in private] + [
            ("public", int(item[0])) for item in public
        ]
        if len(candidates) != 1:
            continue
        source_kind, source_id = candidates[0]
        external_key = f"legacy-accepted-offer-{int(offer_id)}"
        existing_event = resolve_offer_received_identity(
            conn,
            int(cycle_id),
            source_kind=source_kind,
            source_id=source_id,
            external_offer_key=external_key,
            school_name=str(school_name),
            received_at=timestamp,
            legacy_offer_id=int(offer_id),
        )
        if existing_event is None:
            cursor = conn.execute(
                """INSERT INTO offer_received_events(
                     application_cycle_id,student_id,source_kind,
                     public_application_group_id,private_application_id,legacy_offer_id,
                     school_id,school_name_snapshot,external_offer_key,idempotency_key,
                     received_at,recorded_by_user_id,recorded_by_name,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    cycle_id, student_id, source_kind,
                    source_id if source_kind == "public" else None,
                    source_id if source_kind == "private" else None,
                    offer_id, school_id, school_name, external_key,
                    f"legacy-accepted-offer:{int(offer_id)}", timestamp,
                    user_id, user_name, timestamp,
                ),
            )
            event_id = int(cursor.lastrowid)
            inserted += 1
        else:
            event_id = existing_event.id
        from canonical_acceptance import active_acceptance_predicate

        active_acceptance = active_acceptance_predicate(conn)
        existing_acceptance = conn.execute(
            f"""SELECT id,offer_received_event_id FROM cycle_final_acceptances
                 WHERE application_cycle_id=? AND {active_acceptance}""",
            (cycle_id,),
        ).fetchone()
        if existing_acceptance is not None:
            continue
        cursor = conn.execute(
            """INSERT INTO cycle_final_acceptances(
                 application_cycle_id,student_id,offer_received_event_id,school_id,
                 school_name_snapshot,signed_offer_completed_at,payment_completed_at,
                 selected_at,selected_by_user_id,selected_by_name,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                cycle_id, student_id, event_id, school_id, school_name,
                timestamp if str(signed_path or "").strip() else None,
                timestamp if str(receipt_path or "").strip() and str(deposit_date or "").strip() else None,
                selected_at, user_id, user_name, selected_at, selected_at,
            ),
        )
        acceptance_id = int(cursor.lastrowid)
        inserted += 1
        conn.execute(
            """INSERT INTO cycle_final_acceptance_history(
                 application_cycle_id,final_acceptance_id,event_type,new_value_json,
                 occurred_at,actor_user_id,actor_name,created_at
               ) VALUES(?,?,'selected',?,?,?,?,?)""",
            (
                cycle_id, acceptance_id,
                json.dumps({"legacy_offer_id": int(offer_id)}, sort_keys=True),
                selected_at, user_id, user_name, selected_at,
            ),
        )
        inserted += 1
    return inserted


def _apply_deterministic_backfill_connection(
    conn: sqlite3.Connection,
    *,
    actor: str,
) -> BackfillApplyResult:
    """Apply the shared SAFE plan on a caller-owned transaction."""

    preview = build_backfill_preview(conn)
    digest = hashlib.sha256(preview_to_json(preview).encode("utf-8")).hexdigest()
    cycle_items: dict[str, list[BackfillItem]] = {}
    for item in preview.safe:
        if item.subject_kind == "application_cycle":
            cycle_items.setdefault(item.source_ids[0], []).append(item)
    cycle_ids: dict[str, int] = {}
    inserted = updated_cycles = linked = unchanged = 0
    skipped_ambiguous = len(preview.ambiguous)
    skipped_missing = len(preview.missing_source_fact)
    for item in preview.safe:
        if item.rule_id != "UNSUBMITTED_PARENT_FORM_WORKFLOW_CYCLE":
            continue
        values = dict(item.proposals[0].values)
        inserted += int(_apply_parent_form_cycle(conn, values, actor))
    students = {
        str(row[0])
        for row in conn.execute(
            "SELECT student_id FROM students WHERE coalesce(student_id,'')<>''"
        ).fetchall()
    }
    for student_id in sorted(students):
        for item in cycle_items.get(student_id, []):
            values = dict(item.proposals[0].values)
            cycle_id, created = _apply_cycle(conn, values, actor)
            if cycle_id is None:
                raise ValueError(
                    f"SAFE cycle proposal became non-executable at runtime: {student_id}"
                )
            inserted += int(created)
            unchanged += int(not created)
        existing = conn.execute(
            "SELECT id FROM application_cycles WHERE student_id=? ORDER BY id", (student_id,)
        ).fetchall()
        if len(existing) == 1:
            cycle_ids[student_id] = int(existing[0][0])

    for item in preview.safe:
        if item.rule_id != "LEGACY_GRANTED_CYCLE_CLOSURE":
            continue
        student_id = item.source_ids[0]
        cycle_id = cycle_ids.get(student_id)
        if cycle_id is None:
            continue
        values = dict(item.proposals[0].values)
        cursor = conn.execute(
            """UPDATE application_cycles
                  SET status=?,outcome=?,closed_at=?,updated_by_name=?,updated_at=?
                WHERE id=? AND student_id=? AND status='active' AND outcome IS NULL""",
            (
                values["status"], values["outcome"], values["closed_at"],
                actor, values["closed_at"], cycle_id, student_id,
            ),
        )
        updated_cycles += int(cursor.rowcount)
        unchanged += int(cursor.rowcount == 0)

    for item in preview.safe:
        if item.subject_kind not in _OWNED_TABLES or not item.proposals:
            continue
        student_id, record_id = item.source_ids[:2]
        cycle_id = cycle_ids.get(student_id)
        if cycle_id is None:
            continue
        table = item.subject_kind
        extra = ",cycle_is_current=is_current" if table == "school_plan_reports" else ""
        cursor = conn.execute(
            f"""UPDATE {table} SET application_cycle_id=?{extra}
                 WHERE id=? AND student_id=? AND application_cycle_id IS NULL""",
            (cycle_id, int(record_id), student_id),
        )
        linked += int(cursor.rowcount)
        unchanged += int(cursor.rowcount == 0)
    materialized = _materialize_submitted_applications(conn)
    materialized_events = _materialize_explicit_acceptances(conn)
    if conn.execute("PRAGMA foreign_key_check").fetchall():
        raise ValueError("deterministic backfill left foreign-key violations")
    quick = conn.execute("PRAGMA quick_check").fetchone()
    if not quick or str(quick[0]).lower() != "ok":
        raise ValueError("deterministic backfill quick_check failed")
    return BackfillApplyResult(
        digest, inserted, updated_cycles, linked, materialized, materialized_events,
        unchanged, skipped_ambiguous, skipped_missing
    )


def apply_deterministic_backfill(
    database_path: str | Path,
    *,
    actor: str,
    formal_database_path: str | Path | None = None,
) -> BackfillApplyResult:
    """Apply unique SAFE proposals transactionally to a non-formal DB copy."""

    target = Path(database_path).expanduser().resolve()
    formal = Path(formal_database_path or _DEFAULT_FORMAL_DATABASE).expanduser().resolve()
    if target == formal:
        raise ValueError("deterministic backfill must not target the formal CRM database")
    if not target.is_file():
        raise ValueError(f"backfill database is not a file: {target}")
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = _apply_deterministic_backfill_connection(conn, actor=actor)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
