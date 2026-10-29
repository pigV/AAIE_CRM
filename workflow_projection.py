"""Read-only projection of active application cycles through one resolver."""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3
from typing import Iterable

from student_lifecycle.projection import load_lifecycle_projection_map
from workflow_resolver import ACTIVE_WORKFLOW_STAGES


@dataclass(frozen=True)
class WorkflowProjectionRow:
    cycle_id: int
    cycle_uid: str
    student_id: str | None
    entity_kind: str
    stage_key: str
    stage_number: int
    stage_title: str
    blocker: str | None
    pending_action: str | None
    issue_codes: tuple[str, ...]


@dataclass(frozen=True)
class StageDistributionEntry:
    key: str
    number: int
    title: str
    count: int
    cycle_ids: tuple[int, ...]


@dataclass(frozen=True)
class StageDistribution:
    stages: tuple[StageDistributionEntry, ...]

    @property
    def by_key(self) -> dict[str, StageDistributionEntry]:
        return {stage.key: stage for stage in self.stages}


def load_current_workflow_projection(
    conn: sqlite3.Connection,
    *,
    cycle_ids: Iterable[int] | None = None,
) -> tuple[WorkflowProjectionRow, ...]:
    """Resolve current active cycles, optionally restricted to requested IDs."""

    requested_ids = None
    if cycle_ids is not None:
        requested_ids = tuple(dict.fromkeys(int(value) for value in cycle_ids))
        if not requested_ids:
            return ()
        marks = ",".join("?" for _ in requested_ids)
        sql = f"""SELECT id FROM application_cycles
                    WHERE status='active' AND id IN ({marks})
                    ORDER BY opened_at,id"""
        params = requested_ids
    else:
        sql = "SELECT id FROM application_cycles WHERE status='active' ORDER BY opened_at,id"
        params = ()
    active_cycle_ids = tuple(
        int(row[0]) for row in conn.execute(sql, params).fetchall()
    )
    projections = load_lifecycle_projection_map(conn, active_cycle_ids)
    rows: list[WorkflowProjectionRow] = []
    for cycle_id in active_cycle_ids:
        result = projections.get(cycle_id)
        if result is None:
            continue
        # Terminal business facts take precedence over a stale active flag. The
        # reconciliation layer reports that contradiction separately.
        if result.stage_key == "closed":
            continue
        rows.append(
            WorkflowProjectionRow(
                cycle_id=cycle_id,
                cycle_uid=result.cycle_uid,
                student_id=result.student_id,
                entity_kind="student" if result.student_id else "workflow_case",
                stage_key=result.stage_key,
                stage_number=result.stage_order,
                stage_title=result.stage_label,
                blocker=result.blocker,
                pending_action=result.next_action,
                issue_codes=result.issue_codes,
            )
        )
    return tuple(rows)


def load_stage_distribution(conn: sqlite3.Connection, *, read_context=None) -> StageDistribution:
    rows = (read_context.workflow(conn) if read_context is not None
            else load_current_workflow_projection(conn))
    cycle_ids_by_key: dict[str, list[int]] = {
        stage.key: [] for stage in ACTIVE_WORKFLOW_STAGES
    }
    definitions = {
        stage.key: (stage.number, stage.title) for stage in ACTIVE_WORKFLOW_STAGES
    }
    for row in rows:
        cycle_ids_by_key.setdefault(row.stage_key, []).append(row.cycle_id)
        definitions.setdefault(row.stage_key, (row.stage_number, row.stage_title))
    return StageDistribution(
        tuple(
            StageDistributionEntry(
                key=key,
                number=definitions[key][0],
                title=definitions[key][1],
                count=len(cycle_ids_by_key[key]),
                cycle_ids=tuple(cycle_ids_by_key[key]),
            )
            for key in sorted(definitions, key=lambda item: (definitions[item][0], item))
        )
    )
