"""Business-facing presentation helpers for Transfer Case audit data."""

from __future__ import annotations

from typing import Any, Mapping

from staff_display_formatter import format_staff_name


TRANSFER_WORKSPACE_STAGES = (
    ("准备材料", "transfer-materials"),
    ("目标学校申请", "transfer-applications"),
    ("最终学校办理", "transfer-final-school"),
    ("后续更新", "transfer-arrangements"),
    ("转学完成", "transfer-completion"),
)

_WORKFLOW_STAGE_INDEX = {
    "draft": 0,
    "preparing": 0,
    "school_search": 1,
    "target_confirmed": 1,
    "application_submitted": 1,
    "waiting_offer": 1,
    "offer_received": 2,
    "waiting_payment": 2,
    "payment_satisfied": 2,
    "waiting_completion_document": 2,
    "ready_to_complete": 4,
}

_WORKFLOW_STATUS_LABELS = {
    "draft": "准备中",
    "preparing": "准备中",
    "school_search": "选择目标学校",
    "target_confirmed": "准备申请",
    "application_submitted": "申请中",
    "waiting_offer": "等待 Offer",
    "offer_received": "已收到 Offer",
    "waiting_payment": "等待付款",
    "payment_satisfied": "付款已确认",
    "waiting_completion_document": "等待 COE",
    "ready_to_complete": "可完成",
    "paused": "已暂停",
    "completed": "已完成",
    "cancelled": "已取消",
    "failed": "未完成",
    "archived": "已归档",
}


def _case_is_completed(case: Any) -> bool:
    return bool(
        getattr(case, "student_case_status", "") == "completed"
        or getattr(case, "workflow_state", "") == "completed"
        or getattr(case, "completed_at", None)
    )


def transfer_case_status_label(case: Any) -> str:
    """Return a stable Chinese business label without leaking state codes."""
    if _case_is_completed(case):
        return "已完成"
    state = str(getattr(case, "workflow_state", "") or "")
    return _WORKFLOW_STATUS_LABELS.get(state, "准备中")


def transfer_current_stage_index(case: Any, completion_evaluation: Any) -> int:
    """Map persisted workflow state to the five visible business stages."""
    if _case_is_completed(case):
        return len(TRANSFER_WORKSPACE_STAGES) - 1
    if bool(getattr(completion_evaluation, "is_ready_to_complete", False)):
        return len(TRANSFER_WORKSPACE_STAGES) - 1
    state = str(getattr(case, "workflow_state", "") or "")
    return _WORKFLOW_STAGE_INDEX.get(state, 0)


def transfer_stage_rows(
    case: Any,
    completion_evaluation: Any,
) -> tuple[dict[str, object], ...]:
    """Return presentation-only stage rows for the Transfer workspace rail."""
    completed = _case_is_completed(case)
    current_index = transfer_current_stage_index(case, completion_evaluation)
    rows = []
    for offset, (label, target_id) in enumerate(TRANSFER_WORKSPACE_STAGES):
        state = (
            "complete"
            if completed or offset < current_index
            else "current" if offset == current_index else "pending"
        )
        rows.append(
            {
                "index": offset + 1,
                "label": label,
                "target_id": target_id,
                "state": state,
                "state_label": {
                    "complete": "已完成",
                    "current": "进行中",
                    "pending": "待开始",
                }[state],
            }
        )
    return tuple(rows)


_AUDIT_ACTIONS = {
    "case_created": "创建了转学记录",
    "transfer_case_created": "创建了转学记录",
    "transfer_case_updated": "更新了转学记录",
    "assignment_added": "更新了负责人或协作人员",
    "assignment_changed": "更新了负责人或协作人员",
    "assignment_ended": "更新了负责人或协作人员",
    "transfer_material_confirmed": "确认了材料",
    "transfer_material_document_attached": "上传了材料文件",
    "transfer_material_document_replaced": "更新了材料文件",
    "transfer_material_added": "添加了材料",
    "transfer_workflow_transitioned": "更新了办理阶段",
    "transfer_case_paused": "暂停了转学记录",
    "transfer_case_resumed": "恢复了转学记录",
    "transfer_case_cancelled": "取消了转学记录",
    "transfer_case_failed": "将转学记录标记为未完成",
    "transfer_case_archived": "归档了转学记录",
    "transfer_case_unarchived": "恢复了转学记录归档",
}


def format_case_audit_event(
    event: Any,
    actor_names: Mapping[int, str],
) -> dict[str, str]:
    """Return a safe, business-readable audit row without internal identifiers."""
    actor_user_id = getattr(event, "actor_user_id", None)
    actor_name = format_staff_name(
        actor_user_id,
        actor_names,
        none_is_system=True,
    )
    event_type = str(getattr(event, "event_type", "") or "")
    return {
        "actor_name": actor_name,
        "action": _AUDIT_ACTIONS.get(event_type, "更新了转学记录"),
        "occurred_at": str(getattr(event, "created_at", "") or ""),
    }
