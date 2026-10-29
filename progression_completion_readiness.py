"""Pure completion-readiness rules for Progression Cases."""

from __future__ import annotations

from dataclasses import dataclass


BLOCKER_LABELS = {
    "final_application": "尚未选定最终学校",
    "accepted_offer": "最终学校的 Offer 尚未接受",
    "signed_offer": "最终升学方案的已签署 Offer 尚未确认",
    "payment_receipt": "付款收据尚未确认",
    "coe": "所有最终学校的新 COE 尚未确认",
    "oshc": "新 OSHC 尚未确认",
    "visa": "签证更新尚未确认",
    "latest_transcript": "最新成绩单尚未在准备材料阶段确认",
    "required_materials": "仍有必需申请材料未确认",
    "current_enrolment": "当前学校权威记录尚未确认",
    "upcoming_enrolment": "待入读学校尚未确认",
    "effective_date": "学校转换生效日期尚未确认",
    "transition": "学校转换尚未建立或已失效",
}


@dataclass(frozen=True)
class ProgressionCompletionSnapshot:
    has_final_application: bool
    final_offer_accepted: bool
    signed_offer_status: str | None
    payment_status: str | None
    payment_not_applicable_reason: str | None
    coe_status: str | None
    oshc_status: str | None
    oshc_not_applicable_reason: str | None
    oshc_decision_audited: bool
    oshc_case_cycle_owned: bool
    oshc_existing_validated: bool
    visa_status: str | None
    latest_transcript_status: str | None
    unresolved_required_materials: int
    current_enrolment_ready: bool
    upcoming_enrolment_ready: bool
    effective_date_ready: bool
    transition_ready: bool


@dataclass(frozen=True)
class ProgressionCompletionReadiness:
    ready: bool
    blocker_keys: tuple[str, ...]
    blocker_messages: tuple[str, ...]


def evaluate_progression_completion(
    snapshot: ProgressionCompletionSnapshot,
) -> ProgressionCompletionReadiness:
    blockers: list[str] = []
    if not snapshot.has_final_application:
        blockers.append("final_application")
    if not snapshot.final_offer_accepted:
        blockers.append("accepted_offer")
    if snapshot.signed_offer_status != "confirmed":
        blockers.append("signed_offer")
    payment_ready = snapshot.payment_status == "confirmed" or (
        snapshot.payment_status == "not_applicable"
        and bool((snapshot.payment_not_applicable_reason or "").strip())
    )
    if not payment_ready:
        blockers.append("payment_receipt")
    if snapshot.coe_status != "confirmed":
        blockers.append("coe")
    oshc_ready = (
        snapshot.oshc_status == "confirmed"
        or snapshot.oshc_existing_validated
    )
    if not oshc_ready:
        blockers.append("oshc")
    if snapshot.latest_transcript_status not in {"verified", "confirmed"}:
        blockers.append("latest_transcript")
    if int(snapshot.unresolved_required_materials) > 0:
        blockers.append("required_materials")
    if not snapshot.current_enrolment_ready:
        blockers.append("current_enrolment")
    if not snapshot.upcoming_enrolment_ready:
        blockers.append("upcoming_enrolment")
    if not snapshot.effective_date_ready:
        blockers.append("effective_date")
    if not snapshot.transition_ready:
        blockers.append("transition")
    keys = tuple(blockers)
    return ProgressionCompletionReadiness(
        ready=not keys,
        blocker_keys=keys,
        blocker_messages=tuple(BLOCKER_LABELS[key] for key in keys),
    )
