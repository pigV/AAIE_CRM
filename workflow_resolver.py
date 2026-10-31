"""Pure authoritative resolver for the 11 active CRM workflow stages."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WorkflowStageDefinition:
    number: int
    key: str
    title: str


ACTIVE_WORKFLOW_STAGES = (
    WorkflowStageDefinition(1, "01_parent_form", "等待家长填写表单"),
    WorkflowStageDefinition(2, "02_contract_creation", "等待建立合同"),
    WorkflowStageDefinition(3, "03_contract_signature", "等待家长签合同"),
    WorkflowStageDefinition(4, "04_school_plan_creation", "等待制作学校方案"),
    WorkflowStageDefinition(5, "05_plan_confirmation", "等待家长确认方案"),
    WorkflowStageDefinition(6, "06_school_application_submission", "等待递交学校申请"),
    WorkflowStageDefinition(7, "07_school_response", "等待学校回复"),
    WorkflowStageDefinition(8, "08_offer_acceptance", "等待家长接受 Offer（Signed Offer + Payment）"),
    WorkflowStageDefinition(9, "09_waiting_coe", "等待 COE"),
    WorkflowStageDefinition(10, "10_visa_submission", "等待递交 Visa"),
    WorkflowStageDefinition(11, "11_visa_result", "等待 Visa 结果"),
)

_STAGE_BY_NUMBER = {stage.number: stage for stage in ACTIVE_WORKFLOW_STAGES}


@dataclass(frozen=True)
class WorkflowFacts:
    cycle_id: int
    cycle_uid: str
    student_id: str | None
    entity_kind: str
    cycle_status: str
    cycle_outcome: str | None
    parent_form_exists: bool
    parent_form_submitted: bool
    student_exists: bool
    contract_exists: bool
    contract_ready_for_signature: bool
    contract_signed: bool
    plan_exists: bool
    plan_sent: bool
    plan_confirmed: bool
    submitted_application_count: int
    actionable_offer_count: int
    final_acceptance_selected: bool
    signed_offer_completed: bool
    payment_completed: bool
    submitted_back_to_final_school: bool
    coe_exists: bool
    visa_submitted: bool
    visa_decision: str | None
    cycle_type: str = "initial"
    source_case_id: int | None = None
    contract_gate_status: str | None = None
    contract_execution_pending: bool = False


@dataclass(frozen=True)
class WorkflowStageResult:
    cycle_id: int
    key: str | None
    number: int | None
    title: str
    blocker: str | None
    pending_action: str | None
    is_closed: bool
    outcome: str | None
    issue_codes: tuple[str, ...]


def _issue_codes(facts: WorkflowFacts) -> tuple[str, ...]:
    issues: list[str] = []
    if facts.visa_decision and facts.cycle_status != "closed":
        issues.append("VISA_DECISION_ACTIVE_CYCLE")
    if facts.cycle_status == "closed" and not facts.cycle_outcome:
        issues.append("CLOSED_CYCLE_WITHOUT_OUTCOME")
    if facts.cycle_type != "progression" and facts.plan_confirmed and not facts.plan_sent:
        issues.append("PLAN_CONFIRMED_WITHOUT_SENT_PLAN")
    if (
        facts.cycle_type != "progression"
        and facts.submitted_application_count
        and not facts.plan_confirmed
    ):
        issues.append("APPLICATION_SUBMITTED_WITHOUT_CONFIRMED_PLAN")
    if facts.actionable_offer_count and not facts.submitted_application_count:
        issues.append("OFFER_WITHOUT_SUBMITTED_APPLICATION")
    if facts.final_acceptance_selected and not facts.actionable_offer_count:
        issues.append("FINAL_ACCEPTANCE_WITHOUT_OFFER")
    if facts.submitted_back_to_final_school and not (
        facts.final_acceptance_selected
        and facts.signed_offer_completed
        and facts.payment_completed
    ):
        issues.append("FINAL_SCHOOL_SUBMISSION_WITHOUT_ACCEPTANCE_EVIDENCE")
    if facts.coe_exists and not facts.submitted_back_to_final_school:
        issues.append("COE_WITHOUT_FINAL_SCHOOL_SUBMISSION")
    if facts.visa_submitted and not facts.coe_exists:
        issues.append("VISA_SUBMITTED_WITHOUT_COE")
    return tuple(issues)


def _active_result(
    facts: WorkflowFacts,
    number: int,
    blocker: str,
    issues: tuple[str, ...],
    *,
    pending_action: str | None = None,
    title: str | None = None,
) -> WorkflowStageResult:
    stage = _STAGE_BY_NUMBER[number]
    return WorkflowStageResult(
        cycle_id=facts.cycle_id,
        key=stage.key,
        number=stage.number,
        title=title or stage.title,
        blocker=blocker,
        pending_action=pending_action,
        is_closed=False,
        outcome=None,
        issue_codes=issues,
    )


def resolve_workflow_stage(facts: WorkflowFacts) -> WorkflowStageResult:
    """Resolve one cycle without reading or modifying any persistence."""

    issues = _issue_codes(facts)
    if facts.visa_decision or facts.cycle_status == "closed":
        outcome = facts.cycle_outcome
        if not outcome and facts.visa_decision:
            outcome = "granted" if facts.visa_decision == "grant" else "refused"
        return WorkflowStageResult(
            cycle_id=facts.cycle_id,
            key=None,
            number=None,
            title="Application Cycle Closed",
            blocker=None,
            pending_action=None,
            is_closed=True,
            outcome=outcome,
            issue_codes=issues,
        )
    if facts.cycle_type == "progression":
        if facts.contract_gate_status == "unclassified":
            return _active_result(
                facts,
                2,
                "必须先明确 Higher Education 或 K-12 / School Progression",
                (*issues, "PROGRESSION_BUSINESS_TYPE_UNCLASSIFIED"),
                title="请先明确升学业务类型",
            )
        if facts.contract_gate_status == "waiting_contract_creation":
            return _active_result(
                facts, 2, "等待建立对应升学合同", issues
            )
        if facts.contract_gate_status == "waiting_parent_signature":
            return _active_result(
                facts, 3, "等待家长完成升学合同签署", issues,
                title="等待家长签署",
            )
        if facts.contract_gate_status == "waiting_aaie_execution":
            return _active_result(
                facts, 3, "等待 AAIE 完成升学合同签章", issues,
                title="等待AAIE签章",
            )
        if facts.contract_gate_status not in {"not_required", "complete"}:
            return _active_result(
                facts,
                2,
                "Progression contract ownership 无效",
                (*issues, "PROGRESSION_CONTRACT_OWNERSHIP_INVALID"),
                title="升学合同归属不匹配",
            )
        if facts.visa_submitted:
            return _active_result(
                facts, 11, "等待有效 Visa Grant 或 Refusal Letter", issues
            )
        if facts.coe_exists and facts.submitted_back_to_final_school:
            return _active_result(facts, 10, "等待正式递交 Visa", issues)
        if facts.submitted_back_to_final_school:
            return _active_result(facts, 9, "等待最终升学方案出具 COE", issues)
        if facts.actionable_offer_count:
            pending = None
            blocker = "等待选择最终升学方案并完成 Signed Offer 与 Payment"
            if (
                facts.final_acceptance_selected
                and facts.signed_offer_completed
                and facts.payment_completed
            ):
                pending = "PENDING_FINAL_SCHOOL_SUBMISSION"
                blocker = "待递交 Signed Offer + Payment 给最终学校"
            return _active_result(
                facts, 8, blocker, issues, pending_action=pending
            )
        if facts.submitted_application_count:
            return _active_result(facts, 7, "等待目标学校回复", issues)
        return _active_result(
            facts,
            6,
            "准备材料、目标学校与升学路径后递交申请",
            issues,
            title="准备升学材料与目标申请",
        )
    if facts.visa_submitted:
        return _active_result(facts, 11, "等待有效 Visa Grant 或 Refusal Letter", issues)
    if facts.coe_exists and facts.submitted_back_to_final_school:
        return _active_result(facts, 10, "等待正式递交 Visa", issues)
    if facts.submitted_back_to_final_school:
        return _active_result(facts, 9, "等待最终学校出具 COE", issues)
    if facts.actionable_offer_count:
        pending = None
        blocker = "等待选择最终学校并完成 Signed Offer 与 Payment"
        if facts.final_acceptance_selected and facts.signed_offer_completed and facts.payment_completed:
            pending = "PENDING_FINAL_SCHOOL_SUBMISSION"
            blocker = "待递交 Signed Offer + Payment 给最终学校"
        return _active_result(facts, 8, blocker, issues, pending_action=pending)
    if facts.submitted_application_count:
        return _active_result(facts, 7, "等待 Public Group / Private Application 学校回复", issues)
    if facts.plan_confirmed:
        return _active_result(facts, 6, "等待正式递交学校申请", issues)
    if facts.plan_sent:
        return _active_result(facts, 5, "等待家长最终确认学校方案", issues)
    if facts.contract_execution_pending:
        return _active_result(
            facts, 3, "家长已签署，等待 AAIE 完成签章及完整合同归档", issues,
            title="等待 AAIE 完成签章／归档",
        )
    if facts.contract_signed:
        return _active_result(facts, 4, "等待制作并发送学校方案", issues)
    if facts.contract_ready_for_signature:
        return _active_result(facts, 3, "等待家长完成在线合同签署", issues)
    if facts.student_exists:
        return _active_result(facts, 2, "等待建立合同及签署链接", issues)
    return _active_result(facts, 1, "等待家长提交 Parent Form", issues)
