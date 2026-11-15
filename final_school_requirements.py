"""Audited requirement decisions for one final Offer acceptance round."""

STEPS = {"signed": "签署", "payment": "付款", "return": "回传"}


def load_optional_steps(connection, acceptance_ids):
    if not acceptance_ids or not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='audit_logs'"
    ).fetchone():
        return {}
    ids = tuple(str(value) for value in acceptance_ids)
    marks = ",".join("?" for _ in ids)
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='view' AND name='final_school_optional_steps'"
    ).fetchone():
        result = {}
        for acceptance_id, step in connection.execute(
            f"SELECT acceptance_id,step FROM final_school_optional_steps WHERE acceptance_id IN ({marks})",
            ids,
        ):
            result.setdefault(acceptance_id, set()).add(step)
        return result
    latest = {}
    for acceptance_id, step, decision in connection.execute(
        f"""SELECT record_id,old_value,json_extract(new_value,'$.decision') FROM audit_logs
             WHERE module_name='offers' AND action_type='final_school_step_requirement'
               AND status='success' AND json_valid(new_value) AND record_id IN ({marks}) ORDER BY id DESC""",
        ids,
    ):
        if step in STEPS:
            latest.setdefault((int(acceptance_id), step), decision)
    result = {}
    for (acceptance_id, step), decision in latest.items():
        if decision == "optional":
            result.setdefault(acceptance_id, set()).add(step)
    return result


def outstanding_steps(signed, paid, returned, optional):
    return tuple(
        label for (step, label), completed in zip(STEPS.items(), (signed, paid, returned))
        if not completed and step not in optional
    )
