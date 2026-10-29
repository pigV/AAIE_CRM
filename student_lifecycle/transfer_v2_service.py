from __future__ import annotations

from contextlib import closing
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
from typing import Callable
from uuid import uuid4

from revenue.cycle_integration import ensure_revenue_case_for_cycle
from revenue.operational_integration import process_revenue_contract_completion

from student_lifecycle.transfer_contract_gate import evaluate_transfer_contract_gate
from student_lifecycle import transfer_source_authority as source_authority
from student_lifecycle.transfer_state import (
    evaluate_transfer_creation_state,
    supersede_safe_initial_for_transfer,
)
from student_lifecycle.errors import LifecycleIdempotencyConflictError
from student_lifecycle import repository as lifecycle_repository
from student_lifecycle.transfer_v2_models import (
    TRANSFER_SOURCE_LABELS,
    TRANSFER_TERMS,
    TRANSFER_YEAR_LEVELS,
    TransferV2CreateInput,
    TransferV2Eligibility,
    TransferV2Overview,
)
from student_lifecycle import transfer_v2_repository as repository
from student_lifecycle.transfer_v2_cutover import (
    cutover_applied_at,
    is_legacy_transfer,
    is_new_transfer_cycle,
)


class TransferV2Error(RuntimeError):
    pass


class TransferV2ValidationError(TransferV2Error):
    pass


class TransferV2ActiveConflict(TransferV2Error):
    pass


class TransferV2NotFound(TransferV2Error):
    pass


def _default_uid(prefix: str) -> str:
    return f"{prefix}:{uuid4().hex}"


class TransferV2Service:
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
    def _required_text(value: object, label: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise TransferV2ValidationError(f"{label} is required")
        return normalized

    @staticmethod
    def _require_actor(connection: sqlite3.Connection, actor_user_id: int) -> str:
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info('users')")
        }
        name_column = "display_name" if "display_name" in columns else "name"
        row = connection.execute(
            f'SELECT COALESCE("{name_column}",\'\') FROM users WHERE id=?',
            (int(actor_user_id),),
        ).fetchone()
        if row is None:
            raise TransferV2ValidationError("actor not found")
        return str(row[0] or "")

    def creation_eligibility(self, student_id: str) -> TransferV2Eligibility:
        normalized_student_id = self._required_text(student_id, "student")
        with closing(self._connect()) as connection:
            decision = evaluate_transfer_creation_state(
                connection, normalized_student_id
            )
            return TransferV2Eligibility(
                decision.allowed,
                decision.contract_id,
                decision.reason,
                decision.label,
                decision.takeover_cycle_id,
            )

    def creation_source_label(
        self, student_id: str, source_contract_id: int | None = None
    ) -> str | None:
        """Display the exact saved authority; missing context never implies Existing."""
        normalized_student_id = self._required_text(student_id, "student")
        if source_contract_id is None:
            return None
        with closing(self._connect()) as connection:
            try:
                saved = source_authority.load_for_contract(
                    connection, normalized_student_id, source_contract_id
                )
                if saved is None:
                    return None
                if saved["lifecycle_state"] == "consumed":
                    return str(saved["source_classification"])
                resolved = source_authority.resolve_for_creation(
                    connection, normalized_student_id, source_contract_id
                )
                return str(resolved["source_classification"])
            except source_authority.SourceAuthorityError:
                return None

    def _validate_request(
        self, connection: sqlite3.Connection, request: TransferV2CreateInput
    ) -> dict[str, object]:
        student_id = self._required_text(request.student_id, "student")
        if connection.execute(
            "SELECT 1 FROM students WHERE student_id=?", (student_id,)
        ).fetchone() is None:
            raise TransferV2ValidationError("student not found")
        if not 2000 <= int(request.transfer_year) <= 2200:
            raise TransferV2ValidationError("invalid transfer year")
        if not request.targets:
            raise TransferV2ValidationError("at least one target school is required")
        target_rows: list[dict[str, object]] = []
        target_identities: set[tuple[str, object]] = set()
        for target in request.targets:
            name = self._required_text(target.school_name, "target school")
            identity = (
                ("school_id", int(target.school_id))
                if target.school_id is not None
                else ("manual_name", name.casefold())
            )
            if identity in target_identities:
                raise TransferV2ValidationError("target schools must be unique")
            target_identities.add(identity)
            if target.school_id is not None:
                snapshot = repository.school_snapshot(connection, int(target.school_id))
                if snapshot is None:
                    raise TransferV2ValidationError("target school not found")
                if name != str(snapshot["school_name"] or "").strip():
                    raise TransferV2ValidationError(
                        "school name and identifier do not match"
                    )
                target_rows.append({
                    "school_id": int(snapshot["id"]),
                    "school_name": str(snapshot["school_name"]),
                    "state": snapshot["state"],
                    "region": snapshot["region"],
                    "school_type": snapshot["school_type"],
                    "sector": snapshot["sector_key"],
                })
            else:
                target_rows.append({
                    "school_id": None,
                    "school_name": name,
                    "state": str(target.state or "").strip() or None,
                    "region": str(target.region or "").strip() or None,
                    "school_type": str(target.school_type or "").strip() or None,
                    "sector": None,
                })
        current_name = self._required_text(request.current_school_name, "current school")
        current_snapshot = None
        if request.current_school_id is not None:
            current_snapshot = repository.school_snapshot(
                connection, int(request.current_school_id)
            )
            if current_snapshot is None:
                raise TransferV2ValidationError("current school not found")
            if current_name != str(current_snapshot["school_name"] or "").strip():
                raise TransferV2ValidationError(
                    "school name and identifier do not match"
                )
            current_name = str(current_snapshot["school_name"])
        current_year_level = self._required_text(
            request.current_year_level, "current year level"
        )
        if current_year_level not in TRANSFER_YEAR_LEVELS:
            raise TransferV2ValidationError("invalid current year level")
        application_year_level = self._required_text(
            request.application_year_level, "application year level"
        )
        if application_year_level not in TRANSFER_YEAR_LEVELS:
            raise TransferV2ValidationError("invalid application year level")
        transfer_term = self._required_text(request.transfer_term, "transfer term")
        if transfer_term not in TRANSFER_TERMS:
            raise TransferV2ValidationError("invalid transfer term")
        return {
            "student_id": student_id,
            "source_label": request.source_label,
            "current_school_id": (
                int(current_snapshot["id"])
                if current_snapshot is not None
                else request.current_school_id
            ),
            "current_school_name": current_name,
            "current_state": (
                current_snapshot["state"]
                if current_snapshot is not None
                else str(request.current_school_state or "").strip() or None
            ),
            "current_region": (
                current_snapshot["region"]
                if current_snapshot is not None
                else str(request.current_school_region or "").strip() or None
            ),
            "current_school_type": (
                current_snapshot["school_type"]
                if current_snapshot is not None
                else str(request.current_school_type or "").strip() or None
            ),
            "current_sector": (
                current_snapshot["sector_key"] if current_snapshot is not None else None
            ),
            "current_year_level": current_year_level,
            "application_year_level": application_year_level,
            "transfer_year": int(request.transfer_year),
            "transfer_term": transfer_term,
            "targets": target_rows,
        }

    def create_cycle(
        self,
        request: TransferV2CreateInput,
        *,
        actor_user_id: int,
        idempotency_key: str,
    ) -> TransferV2Overview:
        key = self._required_text(idempotency_key, "idempotency key")
        student_id = self._required_text(request.student_id, "student")
        operation = "create_transfer_v2"
        request_payload = asdict(request)
        legacy_request_hash = lifecycle_repository.payload_hash(request_payload)
        request_payload.pop("source_label", None)
        request_hash = lifecycle_repository.payload_hash(request_payload)
        event_uid = self.uid_factory(f"transfer-v2-create-{key}")
        timestamp = self.clock()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            actor_name = self._require_actor(connection, actor_user_id)
            previous_receipt = connection.execute(
                "SELECT payload_sha256 FROM student_lifecycle_idempotency_receipts "
                "WHERE student_id=? AND operation=? AND idempotency_key=?",
                (student_id, operation, key),
            ).fetchone()
            replay_hash = request_hash
            if previous_receipt is not None and str(previous_receipt[0]) == legacy_request_hash:
                replay_hash = legacy_request_hash
            try:
                replay = lifecycle_repository.replay_receipt(
                    connection,
                    student_id=student_id,
                    operation=operation,
                    idempotency_key=key,
                    expected_hash=replay_hash,
                )
            except LifecycleIdempotencyConflictError:
                raise TransferV2ValidationError(
                    "转学记录已经建立，请勿重复提交。"
                ) from None
            if replay is not None:
                cycle_id = int(replay.cycle_id)
                connection.commit()
                return self.load_overview(cycle_id)
            values = self._validate_request(connection, request)
            creation = evaluate_transfer_creation_state(
                connection, str(values["student_id"])
            )
            if not creation.allowed:
                if creation.reason in {
                    "active_transfer_exists",
                    "active_school_cycle_has_business_facts",
                }:
                    raise TransferV2ActiveConflict(creation.label)
                raise TransferV2ValidationError(creation.label)
            contract_id = request.source_contract_id
            if (
                creation.contract_id is None
                or contract_id is None
                or int(contract_id) != int(creation.contract_id)
            ):
                raise TransferV2ValidationError("合同归属不匹配")
            gate = evaluate_transfer_contract_gate(
                connection,
                student_id=str(values["student_id"]),
                source_contract_id=contract_id,
                expected_cycle_id=None,
            )
            if not gate.allowed or gate.source_contract_id is None:
                raise TransferV2ValidationError(gate.label)
            takeover_cycle_id = creation.takeover_cycle_id
            try:
                authority = source_authority.resolve_for_creation(
                    connection, str(values["student_id"]), int(contract_id)
                )
            except source_authority.SourceAuthorityError as exc:
                raise TransferV2ValidationError(str(exc)) from exc
            values["source_label"] = authority["source_classification"]
            if takeover_cycle_id is not None:
                supersede_safe_initial_for_transfer(
                    connection,
                    student_id=str(values["student_id"]),
                    cycle_id=int(takeover_cycle_id),
                    actor_user_id=int(actor_user_id),
                    occurred_at=timestamp,
                )
            case_sequence = repository.next_case_sequence(
                connection, str(values["student_id"])
            )
            cycle_sequence = repository.next_cycle_sequence(
                connection, str(values["student_id"])
            )
            case_id = int(connection.execute(
                """INSERT INTO student_cases(
                     student_id,case_type,sequence_no,status,active_slot,started_at,
                     created_by,created_at,updated_by,updated_at)
                   VALUES(?,'transfer',?,'in_progress',1,?,?,?,?,?)""",
                (
                    values["student_id"], case_sequence, timestamp,
                    int(actor_user_id), timestamp, int(actor_user_id), timestamp,
                ),
            ).lastrowid)
            cycle_uid = self.uid_factory(
                f"transfer-v2-cycle-{values['student_id']}-{cycle_sequence}"
            )
            cycle_id = int(connection.execute(
                """INSERT INTO application_cycles(
                     cycle_uid,student_id,source_student_case_id,cycle_type,sequence_no,
                     status,opened_at,created_by_user_id,created_by_name,created_at,
                     updated_by_user_id,updated_by_name,updated_at,education_stream)
                   VALUES(?,?,?,'transfer',?,'active',?,?,?,?,?,?,?,'school_education')""",
                (
                    cycle_uid, values["student_id"], case_id, cycle_sequence,
                    timestamp, int(actor_user_id), actor_name, timestamp,
                    int(actor_user_id), actor_name, timestamp,
                ),
            ).lastrowid)
            transfer_origin = (
                "internal" if values["source_label"] == "aaie_existing" else "external"
            )
            connection.execute(
                """INSERT INTO transfer_cases(
                     case_id,transfer_origin,source_contract_id,workflow_state,
                     current_school_name_snapshot,current_school_id,current_school_sector,
                     rule_status,required_completion_document,tuition_requirement,
                     rule_version,created_at,updated_at,relationship_status,v2_version,
                     transfer_source_label,current_year_level_snapshot,
                     application_year_level,transfer_year,transfer_term,
                     current_school_state_snapshot,current_school_region_snapshot,
                     current_school_type_snapshot)
                   VALUES(?,?,?,'preparing',?,?,?,?,? ,?,'transfer-v2',?,?,
                          'needs_repair',2,?,?,?,?,?,?,?,?)""",
                (
                    case_id, transfer_origin, int(gate.source_contract_id),
                    values["current_school_name"], values["current_school_id"],
                    values["current_sector"], "pending_confirmation",
                    "pending_confirmation", "pending_confirmation", timestamp, timestamp,
                    values["source_label"], values["current_year_level"],
                    values["application_year_level"], values["transfer_year"],
                    values["transfer_term"], values["current_state"],
                    values["current_region"], values["current_school_type"],
                ),
            )
            for index, target in enumerate(values["targets"], start=1):
                branch_uid = self.uid_factory(f"transfer-v2-branch-{cycle_id}-{index}")
                branch_id = int(connection.execute(
                    """INSERT INTO transfer_school_applications(
                         case_id,school_id,school_name_snapshot,school_sector,
                         preference_order,application_status,created_by,created_at,
                         updated_at,branch_uid,application_cycle_id,original_school_id,
                         school_state_snapshot,school_region_snapshot,school_type_snapshot,
                         updated_by)
                       VALUES(?,?,?,?,?,'preparing',?,?,?,?,?,?,?,?,?,?)""",
                    (
                        case_id, target["school_id"], target["school_name"],
                        target["sector"], index, int(actor_user_id), timestamp, timestamp,
                        branch_uid, cycle_id, target["school_id"], target["state"],
                        target["region"], target["school_type"], int(actor_user_id),
                    ),
                ).lastrowid)
                if not is_new_transfer_cycle(connection, cycle_id):
                    connection.execute(
                        """INSERT INTO transfer_target_state_events(
                             event_uid,application_cycle_id,branch_id,previous_state,new_state,
                             event_type,actor_user_id,occurred_at,created_at)
                           VALUES(?,?,?,NULL,'materials_preparation','created',?,?,?)""",
                        (
                            self.uid_factory(f"transfer-v2-branch-created-{branch_id}"),
                            cycle_id, branch_id, int(actor_user_id), timestamp, timestamp,
                        ),
                    )
            connection.execute(
                """INSERT INTO transfer_v2_origin_events(
                     event_uid,application_cycle_id,previous_source_label,new_source_label,
                     actor_user_id,occurred_at,created_at)
                   VALUES(?,?,NULL,?,?,?,?)""",
                (
                    event_uid, cycle_id, values["source_label"], int(actor_user_id),
                    timestamp, timestamp,
                ),
            )
            attached = connection.execute(
                """UPDATE contracts SET application_cycle_id=?,updated_at=?
                    WHERE id=? AND student_id=? AND application_cycle_id IS NULL""",
                (
                    cycle_id, timestamp, int(gate.source_contract_id), values["student_id"],
                ),
            )
            if attached.rowcount != 1:
                raise TransferV2ActiveConflict("Transfer contract ownership changed")
            try:
                source_authority.consume_authority(
                    connection, int(authority["id"]), cycle_id,
                    actor_user_id=int(actor_user_id), occurred_at=timestamp,
                )
            except source_authority.SourceAuthorityError as exc:
                raise TransferV2ValidationError(str(exc)) from exc
            ensure_revenue_case_for_cycle(
                connection, cycle_id, int(actor_user_id),
                occurred_at=timestamp, integration_event="created",
            )
            process_revenue_contract_completion(
                connection, int(gate.source_contract_id), int(actor_user_id), timestamp,
            )
            connection.execute(
                """INSERT INTO case_events(
                     case_id,event_type,entity_type,entity_id,new_value_json,
                     metadata_json,actor_type,actor_user_id,request_id,created_at)
                   VALUES(?,'transfer_v2_created','application_cycle',?,?,?,'user',?,?,?)""",
                (
                    case_id, str(cycle_id),
                    json.dumps({"application_cycle_id": cycle_id}, sort_keys=True),
                    json.dumps({"idempotency_key": key}, sort_keys=True),
                    int(actor_user_id), key, timestamp,
                ),
            )
            if takeover_cycle_id is not None:
                connection.execute(
                    """INSERT INTO case_events(
                         case_id,event_type,entity_type,entity_id,new_value_json,
                         metadata_json,actor_type,actor_user_id,request_id,created_at)
                       VALUES(?,'initial_lifecycle_superseded','application_cycle',
                              ?,?,?,'user',?,?,?)""",
                    (
                        case_id,
                        str(takeover_cycle_id),
                        json.dumps(
                            {"outcome": "superseded_by_transfer"}, sort_keys=True
                        ),
                        json.dumps(
                            {
                                "source_cycle_id": int(takeover_cycle_id),
                                "target_cycle_id": cycle_id,
                                "target_case_id": case_id,
                                "reason": "fact_empty_initial_transfer_takeover",
                            },
                            sort_keys=True,
                        ),
                        int(actor_user_id),
                        key,
                        timestamp,
                    ),
                )
            lifecycle_result = lifecycle_repository.load_result(connection, case_id)
            lifecycle_repository.record_receipt(
                connection,
                student_id=str(values["student_id"]),
                operation=operation,
                idempotency_key=key,
                payload_sha256=request_hash,
                case_id=case_id,
                cycle_id=cycle_id,
                result=lifecycle_result,
                actor_user_id=int(actor_user_id),
                occurred_at=timestamp,
            )
            connection.commit()
        return self.load_overview(cycle_id)

    def load_overview(self, cycle_id: int) -> TransferV2Overview:
        with closing(self._connect()) as connection:
            row = repository.cycle_case_row(connection, int(cycle_id))
            if row is None:
                raise TransferV2NotFound("Transfer Cycle not found")
            is_legacy = is_legacy_transfer(
                connection,
                int(cycle_id),
                v2_version=int(row["v2_version"] or 0),
            )
            branches = repository.branch_rows(
                connection, cycle_id=int(cycle_id), case_id=int(row["case_id"])
            )
            if is_legacy:
                stage = "legacy_unconfirmed"
                label = "Legacy Transfer · Lifecycle unconfirmed"
            else:
                from student_lifecycle.transfer_v2_projection import (
                    load_transfer_v2_projection,
                )
                projection = load_transfer_v2_projection(connection, int(cycle_id))
                stage = projection.stage_key
                label = projection.stage_label
            return TransferV2Overview(
                student_id=str(row["student_id"]),
                cycle_id=int(row["cycle_id"]),
                case_id=int(row["case_id"]),
                source_label=(
                    str(row["transfer_source_label"])
                    if row["transfer_source_label"]
                    else None
                ),
                lifecycle_stage=stage,
                lifecycle_label=label,
                is_legacy=is_legacy,
                current_school_name=str(row["current_school_name_snapshot"]),
                current_year_level=(
                    str(row["current_year_level_snapshot"])
                    if row["current_year_level_snapshot"]
                    else None
                ),
                application_year_level=(
                    str(row["application_year_level"])
                    if row["application_year_level"]
                    else None
                ),
                transfer_year=int(row["transfer_year"]) if row["transfer_year"] else None,
                transfer_term=str(row["transfer_term"]) if row["transfer_term"] else None,
                branches=branches,
                current_school_id=(
                    int(row["current_school_id"])
                    if row["current_school_id"] is not None else None
                ),
            )

    def add_target_branch(
        self,
        cycle_id: int,
        *,
        target,
        actor_user_id: int,
        idempotency_key: str,
    ) -> TransferV2Overview:
        """Append one Target Branch to an existing active Transfer V2 Cycle."""

        key = self._required_text(idempotency_key, "idempotency key")
        timestamp = self.clock()
        branch_uid = self.uid_factory(f"transfer-v2-add-branch-{cycle_id}-{key}")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_actor(connection, actor_user_id)
            replay = connection.execute(
                "SELECT id FROM transfer_school_applications WHERE branch_uid=?",
                (branch_uid,),
            ).fetchone()
            if replay is not None:
                connection.commit()
                return self.load_overview(int(cycle_id))
            cycle = repository.cycle_case_row(connection, int(cycle_id))
            if (
                cycle is None
                or int(cycle["v2_version"] or 0) != 2
                or str(cycle["cycle_status"] or "") != "active"
            ):
                raise TransferV2ValidationError(
                    "target school requires an active Transfer V2 Cycle"
                )
            if cutover_applied_at(connection) is not None and not is_new_transfer_cycle(
                connection, int(cycle_id)
            ):
                raise TransferV2ValidationError("Legacy Transfer is read only")
            name = self._required_text(target.school_name, "target school")
            school_id = target.school_id
            has_removal = any(
                str(column[1]) == "removed_at"
                for column in connection.execute(
                    "PRAGMA table_info(transfer_school_applications)"
                )
            )
            active_clause = " AND removed_at IS NULL" if has_removal else ""
            if school_id is not None:
                snapshot = repository.school_snapshot(connection, int(school_id))
                if snapshot is None:
                    raise TransferV2ValidationError("target school not found")
                if name != str(snapshot["school_name"] or "").strip():
                    raise TransferV2ValidationError(
                        "school name and identifier do not match"
                    )
                values = {
                    "school_id": int(snapshot["id"]),
                    "school_name": str(snapshot["school_name"]),
                    "state": snapshot["state"],
                    "region": snapshot["region"],
                    "school_type": snapshot["school_type"],
                    "sector": snapshot["sector_key"],
                }
                duplicate = connection.execute(
                    f"""SELECT 1 FROM transfer_school_applications
                         WHERE application_cycle_id=? AND school_id=?
                           {active_clause}""",
                    (int(cycle_id), int(snapshot["id"])),
                ).fetchone()
            else:
                values = {
                    "school_id": None,
                    "school_name": name,
                    "state": str(target.state or "").strip() or None,
                    "region": str(target.region or "").strip() or None,
                    "school_type": str(target.school_type or "").strip() or None,
                    "sector": None,
                }
                duplicate = connection.execute(
                    f"""SELECT 1 FROM transfer_school_applications
                         WHERE application_cycle_id=? AND school_id IS NULL
                           AND lower(trim(school_name_snapshot))=lower(trim(?))
                           {active_clause}""",
                    (int(cycle_id), name),
                ).fetchone()
            if duplicate is not None:
                raise TransferV2ValidationError("target schools must be unique")
            preference_order = int(connection.execute(
                """SELECT COALESCE(MAX(preference_order),0)+1
                     FROM transfer_school_applications
                    WHERE application_cycle_id=?""",
                (int(cycle_id),),
            ).fetchone()[0])
            branch_id = int(connection.execute(
                """INSERT INTO transfer_school_applications(
                     case_id,school_id,school_name_snapshot,school_sector,
                     preference_order,application_status,created_by,created_at,
                     updated_at,branch_uid,application_cycle_id,original_school_id,
                     school_state_snapshot,school_region_snapshot,school_type_snapshot,
                     updated_by)
                   VALUES(?,?,?,?,?,'preparing',?,?,?,?,?,?,?,?,?,?)""",
                (
                    int(cycle["case_id"]), values["school_id"], values["school_name"],
                    values["sector"], preference_order, int(actor_user_id), timestamp,
                    timestamp, branch_uid, int(cycle_id), values["school_id"],
                    values["state"], values["region"], values["school_type"],
                    int(actor_user_id),
                ),
            ).lastrowid)
            if not is_new_transfer_cycle(connection, int(cycle_id)):
                connection.execute(
                    """INSERT INTO transfer_target_state_events(
                         event_uid,application_cycle_id,branch_id,previous_state,new_state,
                         event_type,actor_user_id,occurred_at,created_at)
                       VALUES(?,?,?,NULL,'materials_preparation','created',?,?,?)""",
                    (
                        self.uid_factory(f"transfer-v2-branch-created-{branch_id}-{key}"),
                        int(cycle_id), branch_id, int(actor_user_id), timestamp, timestamp,
                    ),
                )
            connection.execute(
                """INSERT INTO case_events(
                     case_id,event_type,entity_type,entity_id,new_value_json,
                     metadata_json,actor_type,actor_user_id,request_id,created_at)
                   VALUES(?,'transfer_v2_target_branch_added','transfer_school_application',
                          ?,?,?,'user',?,?,?)""",
                (
                    int(cycle["case_id"]), str(branch_id),
                    json.dumps({"application_cycle_id": int(cycle_id)}, sort_keys=True),
                    json.dumps({"idempotency_key": key}, sort_keys=True),
                    int(actor_user_id), key, timestamp,
                ),
            )
            connection.commit()
        return self.load_overview(int(cycle_id))

    def remove_target_branch(
        self,
        cycle_id: int,
        *,
        branch_id: int,
        actor_user_id: int,
        idempotency_key: str,
    ) -> TransferV2Overview:
        """Logically remove an unsubmitted Target School from a new Cycle."""

        key = self._required_text(idempotency_key, "idempotency key")
        timestamp = self.clock()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_actor(connection, actor_user_id)
            if cutover_applied_at(connection) is None or not is_new_transfer_cycle(
                connection, int(cycle_id)
            ):
                raise TransferV2ValidationError("Legacy Transfer is read only")
            branch = connection.execute(
                """SELECT branch.id,branch.case_id,branch.removed_at
                     FROM transfer_school_applications AS branch
                     JOIN application_cycles AS cycle
                       ON cycle.id=branch.application_cycle_id
                    WHERE branch.id=? AND branch.application_cycle_id=?
                      AND cycle.status='active'""",
                (int(branch_id), int(cycle_id)),
            ).fetchone()
            if branch is None:
                raise TransferV2ValidationError("Target School not found")
            if branch["removed_at"] is None:
                connection.execute(
                    """UPDATE transfer_school_applications
                          SET removed_at=?,removed_by=?,updated_by=?,updated_at=?
                        WHERE id=? AND removed_at IS NULL""",
                    (
                        timestamp,
                        int(actor_user_id),
                        int(actor_user_id),
                        timestamp,
                        int(branch_id),
                    ),
                )
                connection.execute(
                    """INSERT INTO case_events(
                         case_id,event_type,entity_type,entity_id,new_value_json,
                         metadata_json,actor_type,actor_user_id,request_id,created_at)
                       VALUES(?,'transfer_v2_target_branch_removed',
                         'transfer_school_application',?,?,?,'user',?,?,?)""",
                    (
                        int(branch["case_id"]),
                        str(branch_id),
                        json.dumps({"removed_at": timestamp}, sort_keys=True),
                        json.dumps({"idempotency_key": key}, sort_keys=True),
                        int(actor_user_id),
                        key,
                        timestamp,
                    ),
                )
            connection.commit()
        return self.load_overview(int(cycle_id))

    def change_origin(
        self,
        cycle_id: int,
        *,
        new_source_label: str,
        actor_user_id: int,
        note: str | None,
        idempotency_key: str,
    ) -> TransferV2Overview:
        if new_source_label not in TRANSFER_SOURCE_LABELS:
            raise TransferV2ValidationError("invalid Transfer source label")
        key = self._required_text(idempotency_key, "idempotency key")
        event_uid = self.uid_factory(f"transfer-v2-origin-{key}")
        timestamp = self.clock()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_actor(connection, actor_user_id)
            replay = connection.execute(
                "SELECT 1 FROM transfer_v2_origin_events WHERE event_uid=?",
                (event_uid,),
            ).fetchone()
            row = repository.cycle_case_row(connection, int(cycle_id))
            if row is None or int(row["v2_version"] or 0) != 2:
                raise TransferV2NotFound("Transfer V2 Cycle not found")
            if cutover_applied_at(connection) is not None and not is_new_transfer_cycle(
                connection, int(cycle_id)
            ):
                raise TransferV2ValidationError("Legacy Transfer is read only")
            if cutover_applied_at(connection) is not None or (
                source_authority.schema_ready(connection)
                and connection.execute(
                    "SELECT 1 FROM transfer_source_authorities "
                    "WHERE application_cycle_id=? AND lifecycle_state='consumed'",
                    (int(cycle_id),),
                ).fetchone() is not None
            ):
                raise TransferV2ValidationError(
                    "Transfer 来源由建立时的 Parent intake / Existing 双证据锁定"
                )
            if replay is not None:
                connection.commit()
                return self.load_overview(int(cycle_id))
            previous = str(row["transfer_source_label"])
            if previous == new_source_label:
                raise TransferV2ValidationError("Transfer source label is unchanged")
            connection.execute(
                """UPDATE transfer_cases SET transfer_source_label=?,transfer_origin=?,
                          updated_at=? WHERE case_id=? AND v2_version=2""",
                (
                    new_source_label,
                    "internal" if new_source_label == "aaie_existing" else "external",
                    timestamp, int(row["case_id"]),
                ),
            )
            connection.execute(
                """INSERT INTO transfer_v2_origin_events(
                     event_uid,application_cycle_id,previous_source_label,new_source_label,
                     note,actor_user_id,occurred_at,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (
                    event_uid, int(cycle_id), previous, new_source_label,
                    str(note or "").strip() or None, int(actor_user_id), timestamp, timestamp,
                ),
            )
            connection.commit()
        return self.load_overview(int(cycle_id))

    def request_school_library(
        self,
        cycle_id: int,
        *,
        branch_id: int | None,
        proposed_name: str,
        proposed_state: str | None,
        proposed_region: str | None,
        proposed_school_type: str | None,
        actor_user_id: int,
        idempotency_key: str,
    ) -> int:
        key = self._required_text(idempotency_key, "idempotency key")
        name = self._required_text(proposed_name, "proposed school name")
        request_uid = self.uid_factory(f"transfer-v2-school-library-{key}")
        timestamp = self.clock()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_actor(connection, actor_user_id)
            replay = connection.execute(
                "SELECT id FROM transfer_school_library_requests WHERE request_uid=?",
                (request_uid,),
            ).fetchone()
            if replay is not None:
                connection.commit()
                return int(replay[0])
            cycle = repository.cycle_case_row(connection, int(cycle_id))
            if cycle is None or int(cycle["v2_version"] or 0) != 2:
                raise TransferV2NotFound("Transfer V2 Cycle not found")
            if cutover_applied_at(connection) is not None and not is_new_transfer_cycle(
                connection, int(cycle_id)
            ):
                raise TransferV2ValidationError("Legacy Transfer is read only")
            if branch_id is None:
                if cycle["current_school_id"] is not None or str(
                    cycle["current_school_name_snapshot"] or ""
                ).strip().casefold() != name.casefold():
                    raise TransferV2ValidationError(
                        "manual current school does not match this Cycle"
                    )
            else:
                branch = connection.execute(
                    """SELECT school_id,school_name_snapshot
                        FROM transfer_school_applications
                        WHERE id=? AND application_cycle_id=?""",
                    (int(branch_id), int(cycle_id)),
                ).fetchone()
                if (
                    branch is None
                    or branch["school_id"] is not None
                    or str(branch["school_name_snapshot"] or "").strip().casefold()
                    != name.casefold()
                ):
                    raise TransferV2ValidationError(
                        "manual target school does not match this Cycle"
                    )
            request_id = int(connection.execute(
                """INSERT INTO transfer_school_library_requests(
                     request_uid,application_cycle_id,branch_id,proposed_name,
                     proposed_state,proposed_region,proposed_school_type,status,
                     requested_by,requested_at)
                   VALUES(?,?,?,?,?,?,?,'pending',?,?)""",
                (
                    request_uid,int(cycle_id),branch_id,name,
                    str(proposed_state or "").strip() or None,
                    str(proposed_region or "").strip() or None,
                    str(proposed_school_type or "").strip() or None,
                    int(actor_user_id),timestamp,
                ),
            ).lastrowid)
            connection.commit()
            return request_id
