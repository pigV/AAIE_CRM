from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
from typing import Callable
from uuid import uuid4

from student_lifecycle.transfer_v2_cutover import (
    cutover_applied_at,
    is_new_transfer_cycle,
)


class TransferV2DocumentError(RuntimeError):
    pass


class TransferV2DocumentValidationError(TransferV2DocumentError):
    pass


@dataclass(frozen=True)
class TransferV2Requirement:
    requirement_id: int
    requirement_uid: str
    document_type: str
    label: str
    requires_current_cycle: bool


@dataclass(frozen=True)
class TransferV2Binding:
    binding_id: int
    binding_uid: str
    document_id: int
    document_type: str
    source_cycle_id: int | None
    current_cycle_id: int
    branch_id: int
    document_date: str | None
    validity: str


@dataclass(frozen=True)
class TransferV2ChecklistItem:
    requirement_id: int
    requirement_uid: str
    document_type: str
    label: str
    requires_current_cycle: bool
    satisfied: bool
    adopted_binding_id: int | None


@dataclass(frozen=True)
class TransferV2BranchTransition:
    event_uid: str
    previous_state: str | None
    new_state: str
    missing_requirement_uids: tuple[str, ...]


@dataclass(frozen=True)
class TransferV2DocumentItem:
    binding_id: int
    document_id: int
    document_type: str
    document_date: str | None
    uploaded_at: str
    file_path: str
    source_cycle_id: int | None
    source_cycle_label: str
    validity: str = "valid"
    branch_id: int | None = None


@dataclass(frozen=True)
class TransferV2DocumentSections:
    current: tuple[TransferV2DocumentItem, ...]
    existing: tuple[TransferV2DocumentItem, ...]


def _default_uid(prefix: str) -> str:
    return f"{prefix}:{uuid4().hex}"


class TransferV2DocumentService:
    def __init__(
        self,
        database_path: str | Path,
        *,
        clock: Callable[[], str],
        uid_factory: Callable[[str], str] = _default_uid,
    ) -> None:
        self.database_path = Path(database_path)
        self.clock = clock
        self.uid_factory = uid_factory

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @staticmethod
    def _required(value: object, label: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise TransferV2DocumentValidationError(f"{label} is required")
        return normalized

    @staticmethod
    def _require_actor(connection: sqlite3.Connection, actor_user_id: int) -> None:
        if connection.execute(
            "SELECT 1 FROM users WHERE id=?", (int(actor_user_id),)
        ).fetchone() is None:
            raise TransferV2DocumentValidationError("actor not found")

    @staticmethod
    def _branch(
        connection: sqlite3.Connection, cycle_id: int, branch_id: int
    ) -> sqlite3.Row:
        has_removal = any(
            str(column[1]) == "removed_at"
            for column in connection.execute(
                "PRAGMA table_info(transfer_school_applications)"
            )
        )
        active_clause = " AND branch.removed_at IS NULL" if has_removal else ""
        row = connection.execute(
            f"""SELECT branch.*,cycle.student_id,transfer_case.v2_version
                 FROM transfer_school_applications AS branch
                 JOIN application_cycles AS cycle
                   ON cycle.id=branch.application_cycle_id
                 JOIN transfer_cases AS transfer_case
                   ON transfer_case.case_id=branch.case_id
                WHERE branch.id=? AND branch.application_cycle_id=?
                  AND cycle.cycle_type='transfer'{active_clause}""",
            (int(branch_id), int(cycle_id)),
        ).fetchone()
        if row is None or int(row["v2_version"] or 0) != 2:
            raise TransferV2DocumentValidationError("Transfer V2 branch not found")
        if cutover_applied_at(connection) is not None and not is_new_transfer_cycle(
            connection, int(cycle_id)
        ):
            raise TransferV2DocumentValidationError("Legacy Transfer is read only")
        return row

    @staticmethod
    def _requirement_from_row(row: sqlite3.Row) -> TransferV2Requirement:
        return TransferV2Requirement(
            requirement_id=int(row["id"]),
            requirement_uid=str(row["requirement_uid"]),
            document_type=str(row["document_type"]),
            label=str(row["label"]),
            requires_current_cycle=bool(row["requires_current_cycle"]),
        )

    @staticmethod
    def _binding_from_row(row: sqlite3.Row) -> TransferV2Binding:
        return TransferV2Binding(
            binding_id=int(row["id"]),
            binding_uid=str(row["binding_uid"]),
            document_id=int(row["document_id"]),
            document_type=str(row["document_type"]),
            source_cycle_id=(
                int(row["source_cycle_id"])
                if row["source_cycle_id"] is not None
                else None
            ),
            current_cycle_id=int(row["current_cycle_id"]),
            branch_id=int(row["branch_id"]),
            document_date=str(row["document_date"]) if row["document_date"] else None,
            validity=str(row["current_validity"]),
        )

    def add_requirement(
        self,
        *,
        cycle_id: int,
        branch_id: int,
        document_type: str,
        label: str,
        requires_current_cycle: bool,
        actor_user_id: int,
        idempotency_key: str,
    ) -> TransferV2Requirement:
        key = self._required(idempotency_key, "idempotency key")
        uid = self.uid_factory(f"transfer-v2-requirement-{key}")
        timestamp = self.clock()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_actor(connection, actor_user_id)
            self._branch(connection, cycle_id, branch_id)
            existing = connection.execute(
                "SELECT * FROM transfer_document_requirements WHERE requirement_uid=?",
                (uid,),
            ).fetchone()
            if existing is not None:
                connection.commit()
                return self._requirement_from_row(existing)
            try:
                requirement_id = int(connection.execute(
                    """INSERT INTO transfer_document_requirements(
                         requirement_uid,application_cycle_id,branch_id,document_type,
                         label,is_required,requires_current_cycle,status,created_by,
                         created_at,updated_by,updated_at)
                       VALUES(?,?,?,?,?,1,?,'active',?,?,?,?)""",
                    (
                        uid, int(cycle_id), int(branch_id),
                        self._required(document_type, "document type"),
                        self._required(label, "label"),
                        1 if requires_current_cycle else 0,
                        int(actor_user_id), timestamp, int(actor_user_id), timestamp,
                    ),
                ).lastrowid)
            except sqlite3.IntegrityError as error:
                raise TransferV2DocumentValidationError(str(error)) from None
            row = connection.execute(
                "SELECT * FROM transfer_document_requirements WHERE id=?",
                (requirement_id,),
            ).fetchone()
            connection.commit()
            return self._requirement_from_row(row)

    @staticmethod
    def _binding_query() -> str:
        return """SELECT binding.*,
                  COALESCE((
                    SELECT state.new_state
                      FROM transfer_document_binding_state_events AS state
                     WHERE state.binding_id=binding.id
                     ORDER BY state.occurred_at DESC,state.id DESC LIMIT 1
                  ),binding.initial_validity) AS current_validity
             FROM transfer_document_bindings AS binding"""

    def bind_document(
        self,
        *,
        cycle_id: int,
        branch_id: int,
        document_id: int,
        source_cycle_id: int | None,
        document_type: str,
        document_date: str | None,
        actor_user_id: int,
        idempotency_key: str,
    ) -> TransferV2Binding:
        key = self._required(idempotency_key, "idempotency key")
        uid = self.uid_factory(f"transfer-v2-binding-{key}")
        timestamp = self.clock()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_actor(connection, actor_user_id)
            branch = self._branch(connection, cycle_id, branch_id)
            existing = connection.execute(
                self._binding_query() + " WHERE binding.binding_uid=?", (uid,)
            ).fetchone()
            if existing is not None:
                connection.commit()
                return self._binding_from_row(existing)
            document = connection.execute(
                "SELECT * FROM student_documents WHERE id=? AND student_id=?",
                (int(document_id), str(branch["student_id"])),
            ).fetchone()
            if document is None:
                raise TransferV2DocumentValidationError("student document not found")
            if source_cycle_id is not None:
                source = connection.execute(
                    "SELECT 1 FROM application_cycles WHERE id=? AND student_id=?",
                    (int(source_cycle_id), str(branch["student_id"])),
                ).fetchone()
                if source is None:
                    raise TransferV2DocumentValidationError("source Cycle mismatch")
            binding_id = int(connection.execute(
                """INSERT INTO transfer_document_bindings(
                     binding_uid,document_id,student_id,source_cycle_id,current_cycle_id,
                     branch_id,document_type,document_date,initial_validity,
                     actor_user_id,occurred_at,created_at)
                   VALUES(?,?,?,?,?,?,?,?,'valid',?,?,?)""",
                (
                    uid, int(document_id), str(branch["student_id"]), source_cycle_id,
                    int(cycle_id), int(branch_id),
                    self._required(document_type, "document type"),
                    str(document_date or "").strip() or document["document_date"],
                    int(actor_user_id), timestamp, timestamp,
                ),
            ).lastrowid)
            row = connection.execute(
                self._binding_query() + " WHERE binding.id=?", (binding_id,)
            ).fetchone()
            connection.commit()
            return self._binding_from_row(row)

    def adopt_document(
        self,
        *,
        requirement_id: int,
        binding_id: int,
        actor_user_id: int,
        idempotency_key: str,
    ) -> TransferV2Binding:
        key = self._required(idempotency_key, "idempotency key")
        uid = self.uid_factory(f"transfer-v2-adoption-{key}")
        timestamp = self.clock()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_actor(connection, actor_user_id)
            replay = connection.execute(
                "SELECT binding_id FROM transfer_document_adoption_events WHERE event_uid=?",
                (uid,),
            ).fetchone()
            if replay is not None:
                binding_id = int(replay[0])
            requirement = connection.execute(
                "SELECT * FROM transfer_document_requirements WHERE id=? AND status='active'",
                (int(requirement_id),),
            ).fetchone()
            binding = connection.execute(
                self._binding_query() + " WHERE binding.id=?", (int(binding_id),)
            ).fetchone()
            if requirement is None or binding is None:
                raise TransferV2DocumentValidationError("requirement or binding not found")
            if (
                int(requirement["application_cycle_id"]) != int(binding["current_cycle_id"])
                or int(requirement["branch_id"]) != int(binding["branch_id"])
                or str(requirement["document_type"]) != str(binding["document_type"])
                or str(binding["current_validity"]) != "valid"
            ):
                raise TransferV2DocumentValidationError("document adoption mismatch")
            if replay is None:
                connection.execute(
                    """INSERT INTO transfer_document_adoption_events(
                         event_uid,application_cycle_id,branch_id,requirement_id,binding_id,
                         action,actor_user_id,occurred_at,created_at)
                       VALUES(?,?,?,?,?,'adopted',?,?,?)""",
                    (
                        uid, int(requirement["application_cycle_id"]),
                        int(requirement["branch_id"]), int(requirement_id),
                        int(binding_id), int(actor_user_id), timestamp, timestamp,
                    ),
                )
            connection.commit()
            return self._binding_from_row(binding)

    def current_adopted(self, requirement_id: int) -> TransferV2Binding | None:
        with closing(self._connect()) as connection:
            event = connection.execute(
                """SELECT binding_id,action FROM transfer_document_adoption_events
                    WHERE requirement_id=? ORDER BY occurred_at DESC,id DESC LIMIT 1""",
                (int(requirement_id),),
            ).fetchone()
            if event is None or event["action"] == "unadopted" or event["binding_id"] is None:
                return None
            binding = connection.execute(
                self._binding_query() + " WHERE binding.id=?", (int(event["binding_id"]),)
            ).fetchone()
            if binding is None or str(binding["current_validity"]) != "valid":
                return None
            return self._binding_from_row(binding)

    def load_checklist(
        self, cycle_id: int, branch_id: int
    ) -> tuple[TransferV2ChecklistItem, ...]:
        with closing(self._connect()) as connection:
            self._branch(connection, cycle_id, branch_id)
            rows = connection.execute(
                """SELECT requirement.*,
                          adoption.binding_id AS adopted_binding_id,
                          binding.source_cycle_id,binding.current_cycle_id,
                          COALESCE((
                            SELECT state.new_state
                              FROM transfer_document_binding_state_events AS state
                             WHERE state.binding_id=binding.id
                             ORDER BY state.occurred_at DESC,state.id DESC LIMIT 1
                          ),binding.initial_validity) AS current_validity
                     FROM transfer_document_requirements AS requirement
                     LEFT JOIN transfer_document_adoption_events AS adoption
                       ON adoption.id=(
                         SELECT latest.id FROM transfer_document_adoption_events AS latest
                          WHERE latest.requirement_id=requirement.id
                          ORDER BY latest.occurred_at DESC,latest.id DESC LIMIT 1
                       ) AND adoption.action<>'unadopted'
                     LEFT JOIN transfer_document_bindings AS binding
                       ON binding.id=adoption.binding_id
                    WHERE requirement.application_cycle_id=?
                      AND requirement.branch_id=? AND requirement.status='active'
                    ORDER BY requirement.id""",
                (int(cycle_id), int(branch_id)),
            ).fetchall()
            return tuple(
                TransferV2ChecklistItem(
                    requirement_id=int(row["id"]),
                    requirement_uid=str(row["requirement_uid"]),
                    document_type=str(row["document_type"]),
                    label=str(row["label"]),
                    requires_current_cycle=bool(row["requires_current_cycle"]),
                    satisfied=(
                        row["adopted_binding_id"] is not None
                        and str(row["current_validity"] or "") == "valid"
                        and (
                            not bool(row["requires_current_cycle"])
                            or row["source_cycle_id"] is not None
                            and int(row["source_cycle_id"]) == int(cycle_id)
                        )
                    ),
                    adopted_binding_id=(
                        int(row["adopted_binding_id"])
                        if row["adopted_binding_id"] is not None
                        else None
                    ),
                )
                for row in rows
            )

    def transition_branch(
        self,
        *,
        cycle_id: int,
        branch_id: int,
        new_state: str,
        actor_user_id: int,
        allow_missing: bool,
        note: str | None,
        idempotency_key: str,
    ) -> TransferV2BranchTransition:
        key = self._required(idempotency_key, "idempotency key")
        uid = self.uid_factory(f"transfer-v2-branch-state-{key}")
        timestamp = self.clock()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_actor(connection, actor_user_id)
            self._branch(connection, cycle_id, branch_id)
            new_cycle = is_new_transfer_cycle(connection, int(cycle_id))
            if cutover_applied_at(connection) is not None and not new_cycle:
                raise TransferV2DocumentValidationError("Legacy Transfer is read only")
            allowed_states = (
                {
                    "application_submitted",
                    "awaiting_interview",
                    "awaiting_result",
                    "interview_failed",
                    "school_full",
                    "grades_insufficient",
                    "parent_withdrawn",
                    "other",
                }
                if new_cycle
                else {
                    "materials_preparation", "application_in_progress", "offer", "accepted",
                    "paid", "coe", "completed", "rejected", "withdrawn",
                    "closed_after_transfer_completion",
                }
            )
            if new_state not in allowed_states:
                raise TransferV2DocumentValidationError("invalid branch state")
            if new_cycle and new_state == "other" and not str(note or "").strip():
                raise TransferV2DocumentValidationError("其它状态必须填写原因")
            replay = connection.execute(
                "SELECT * FROM transfer_target_state_events WHERE event_uid=?", (uid,)
            ).fetchone()
            if replay is not None:
                missing = tuple(json.loads(replay["missing_checklist_json"] or "[]"))
                connection.commit()
                return TransferV2BranchTransition(
                    uid, replay["previous_state"], str(replay["new_state"]), missing
                )
            previous = connection.execute(
                """SELECT new_state FROM transfer_target_state_events
                    WHERE branch_id=? ORDER BY occurred_at DESC,id DESC LIMIT 1""",
                (int(branch_id),),
            ).fetchone()
            previous_state = str(previous[0]) if previous else None
            if previous_state in {
                "completed", "rejected", "withdrawn", "interview_failed",
                "school_full", "grades_insufficient", "parent_withdrawn",
                "closed_after_transfer_completion",
            }:
                raise TransferV2DocumentValidationError("terminal branch cannot advance")
            checklist = self.load_checklist(cycle_id, branch_id)
            missing = tuple(item.requirement_uid for item in checklist if not item.satisfied)
            submission_state = (
                "application_submitted" if new_cycle else "application_in_progress"
            )
            if new_state == submission_state and missing and not allow_missing:
                raise TransferV2DocumentValidationError("required documents are missing")
            connection.execute(
                """INSERT INTO transfer_target_state_events(
                     event_uid,application_cycle_id,branch_id,previous_state,new_state,
                     event_type,missing_checklist_json,note,actor_user_id,occurred_at,
                     created_at)
                   VALUES(?,?,?,?,?,'staff_transition',?,?,?,?,?)""",
                (
                    uid, int(cycle_id), int(branch_id), previous_state, new_state,
                    json.dumps(missing, ensure_ascii=False),
                    str(note or "").strip() or None, int(actor_user_id), timestamp, timestamp,
                ),
            )
            if not new_cycle and new_state in {"rejected", "withdrawn"}:
                connection.execute(
                    """UPDATE offers
                          SET offer_status=?
                        WHERE id IN (
                          SELECT link.record_id
                            FROM case_record_links AS link
                           WHERE link.transfer_application_id=?
                             AND link.record_type='offer'
                             AND link.validity_status='current'
                        )""",
                    (new_state, int(branch_id)),
                )
            connection.commit()
            return TransferV2BranchTransition(uid, previous_state, new_state, missing)

    def list_document_sections(
        self, cycle_id: int, branch_id: int
    ) -> TransferV2DocumentSections:
        with closing(self._connect()) as connection:
            branch = self._branch(connection, cycle_id, branch_id)
            rows = connection.execute(
                """SELECT binding.*,document.file_path,document.uploaded_at,
                          source.cycle_type AS source_cycle_type,
                          source.sequence_no AS source_cycle_sequence,
                          current.sequence_no AS current_cycle_sequence,
                          COALESCE((
                            SELECT state.new_state
                              FROM transfer_document_binding_state_events AS state
                             WHERE state.binding_id=binding.id
                             ORDER BY state.occurred_at DESC,state.id DESC LIMIT 1
                          ),binding.initial_validity) AS current_validity
                     FROM transfer_document_bindings AS binding
                     JOIN student_documents AS document ON document.id=binding.document_id
                     LEFT JOIN application_cycles AS source ON source.id=binding.source_cycle_id
                     JOIN application_cycles AS current ON current.id=binding.current_cycle_id
                    WHERE binding.current_cycle_id=? AND binding.branch_id=?
                      AND binding.student_id=?
                    ORDER BY COALESCE(binding.document_date,document.uploaded_at) DESC,
                             binding.id DESC""",
                (int(cycle_id), int(branch_id), str(branch["student_id"])),
            ).fetchall()

            def item(row: sqlite3.Row) -> TransferV2DocumentItem:
                if row["source_cycle_id"] is None:
                    source_label = "Student Master"
                elif int(row["source_cycle_id"]) == int(cycle_id):
                    source_label = f"Current Transfer #{int(row['current_cycle_sequence'])}"
                elif str(row["source_cycle_type"]) == "initial":
                    source_label = "Initial Cycle"
                else:
                    source_label = f"Transfer #{int(row['source_cycle_sequence'])}"
                return TransferV2DocumentItem(
                    binding_id=int(row["id"]),
                    document_id=int(row["document_id"]),
                    document_type=str(row["document_type"]),
                    document_date=str(row["document_date"]) if row["document_date"] else None,
                    uploaded_at=str(row["uploaded_at"]),
                    file_path=str(row["file_path"]),
                    source_cycle_id=(
                        int(row["source_cycle_id"])
                        if row["source_cycle_id"] is not None
                        else None
                    ),
                    source_cycle_label=source_label,
                    validity=str(row["current_validity"] or "valid"),
                    branch_id=int(row["branch_id"]) if row["branch_id"] is not None else None,
                )

            current = tuple(item(row) for row in rows if row["source_cycle_id"] == cycle_id)
            existing = tuple(item(row) for row in rows if row["source_cycle_id"] != cycle_id)
            return TransferV2DocumentSections(current=current, existing=existing)

    def invalidate_binding(
        self,
        *,
        binding_id: int,
        actor_user_id: int,
        reason: str | None,
        idempotency_key: str,
    ) -> TransferV2Binding | None:
        key = self._required(idempotency_key, "idempotency key")
        uid = self.uid_factory(f"transfer-v2-binding-invalid-{key}")
        timestamp = self.clock()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_actor(connection, actor_user_id)
            binding = connection.execute(
                self._binding_query() + " WHERE binding.id=?", (int(binding_id),)
            ).fetchone()
            if binding is None:
                raise TransferV2DocumentValidationError("binding not found")
            replay = connection.execute(
                "SELECT 1 FROM transfer_document_binding_state_events WHERE event_uid=?",
                (uid,),
            ).fetchone()
            if replay is None:
                connection.execute(
                    """INSERT INTO transfer_document_binding_state_events(
                         event_uid,binding_id,previous_state,new_state,reason,
                         actor_user_id,occurred_at,created_at)
                       VALUES(?,?,'valid','invalid',?,?,?,?)""",
                    (
                        uid, int(binding_id), str(reason or "").strip() or None,
                        int(actor_user_id), timestamp, timestamp,
                    ),
                )
            requirements = connection.execute(
                """SELECT requirement.*
                     FROM transfer_document_requirements AS requirement
                    WHERE requirement.id IN (
                      SELECT adoption.requirement_id
                        FROM transfer_document_adoption_events AS adoption
                       WHERE adoption.id=(
                         SELECT latest.id FROM transfer_document_adoption_events AS latest
                          WHERE latest.requirement_id=adoption.requirement_id
                          ORDER BY latest.occurred_at DESC,latest.id DESC LIMIT 1
                       ) AND adoption.binding_id=?
                    )""",
                (int(binding_id),),
            ).fetchall()
            first_fallback = None
            for requirement in requirements:
                parameters: list[object] = [
                    int(binding["current_cycle_id"]), int(binding["branch_id"]),
                    str(binding["document_type"]), int(binding_id),
                ]
                current_only = ""
                if bool(requirement["requires_current_cycle"]):
                    current_only = " AND candidate.source_cycle_id=?"
                    parameters.append(int(binding["current_cycle_id"]))
                candidate = connection.execute(
                    self._binding_query()
                    + """ WHERE binding.current_cycle_id=? AND binding.branch_id=?
                         AND binding.document_type=? AND binding.id<>?"""
                    + current_only
                    + """ AND COALESCE((
                           SELECT state.new_state
                             FROM transfer_document_binding_state_events AS state
                            WHERE state.binding_id=binding.id
                            ORDER BY state.occurred_at DESC,state.id DESC LIMIT 1
                         ),binding.initial_validity)='valid'
                       ORDER BY COALESCE(binding.document_date,'' ) DESC,binding.id DESC
                       LIMIT 1""",
                    tuple(parameters),
                ).fetchone()
                if candidate is None:
                    continue
                fallback_uid = self.uid_factory(
                    f"transfer-v2-auto-fallback-{uid}-{int(requirement['id'])}"
                )
                connection.execute(
                    """INSERT OR IGNORE INTO transfer_document_adoption_events(
                         event_uid,application_cycle_id,branch_id,requirement_id,binding_id,
                         action,reason,actor_user_id,occurred_at,created_at)
                       VALUES(?,?,?,?,?,'auto_fallback',?,?,?,?)""",
                    (
                        fallback_uid, int(requirement["application_cycle_id"]),
                        int(requirement["branch_id"]), int(requirement["id"]),
                        int(candidate["id"]), "Current adopted document became invalid",
                        int(actor_user_id), timestamp, timestamp,
                    ),
                )
                if first_fallback is None:
                    first_fallback = self._binding_from_row(candidate)
            connection.commit()
            return first_fallback
