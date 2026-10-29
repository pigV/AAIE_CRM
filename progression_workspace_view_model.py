"""Read-only presentation model for the Progression Case workspace."""

from __future__ import annotations

from progression_case_service import ProgressionCaseService


def build_progression_workspace(database_path, case_id):
    service = ProgressionCaseService(database_path)
    data = service.get_case_data(case_id)
    readiness = service.get_completion_readiness(case_id)
    applications = []
    for row in data["target_applications"]:
        application = dict(row)
        application["is_final"] = bool(application["is_final"])
        application["documents"] = tuple(
            data["documents_by_target"].get(application["id"], ())
        )
        application["offers"] = tuple(
            offer for offer in data["offers"]
            if (
                offer.get("target_application_id") == application["id"]
                or (
                    offer.get("target_application_id") is None
                    and offer.get("institution_name_snapshot")
                    == application.get("institution_name_snapshot")
                )
            )
        )
        applications.append(application)
    case_documents = tuple(
        item for item in data["document_links"]
        if item["target_application_id"] is None
    )
    current_transcript = next(
        (
            item for item in case_documents
            if item["document_role"] == "latest_transcript"
            and item["is_current"] == 1
        ),
        None,
    )
    blocker_keys = readiness.blocker_keys
    if readiness.ready:
        business_step = "完成"
    elif (
        current_transcript is None
        or current_transcript["status"] not in {"verified", "confirmed"}
        or "required_materials" in blocker_keys
    ):
        business_step = "准备材料"
    elif "final_application" in blocker_keys:
        business_step = "申请学校并选定最终学校"
    elif "accepted_offer" in blocker_keys:
        business_step = "收到并接受最终 Offer"
    elif "payment_receipt" in blocker_keys:
        business_step = "支付学费或押金并确认付款收据"
    elif "coe" in blocker_keys:
        business_step = "更新 COE"
    elif "oshc" in blocker_keys:
        business_step = "更新 OSHC"
    elif "visa" in blocker_keys:
        business_step = "更新签证"
    else:
        business_step = "更新 COE"
    return {
        "case": data["case"],
        "applications": tuple(applications),
        "materials": tuple(data["materials"]),
        "case_documents": case_documents,
        "arrangements": tuple(data["arrangements"]),
        "events": tuple(data["events"]),
        "readiness": readiness,
        "readiness_title": (
            "完成条件已满足" if readiness.ready else "完成条件尚未满足"
        ),
        "current_business_step": business_step,
    }
