"""Read explicit stage decisions tied to one cycle and its current evidence."""

import json


def stage_decisions(connection, action, cycle_ids):
    if not cycle_ids:
        return {}
    marks = ",".join("?" for _ in cycle_ids)
    found = {}
    for cycle_id, payload in connection.execute(
        f"""SELECT cycle.id,audit.new_value
              FROM audit_logs audit
              JOIN application_cycles cycle ON audit.record_id=CAST(cycle.id AS TEXT)
                   AND audit.student_id=cycle.student_id
             WHERE cycle.id IN ({marks}) AND audit.action_type=?
               AND audit.module_name='application_cycles' AND audit.status='success'
             ORDER BY audit.id DESC""",
        (*cycle_ids, action),
    ):
        if cycle_id not in found:
            try:
                decision = json.loads(payload)
                found[cycle_id] = decision if isinstance(decision, dict) else {}
            except (TypeError, ValueError):
                found[cycle_id] = {}
    return found


def pathway_offer_key(connection, cycle_id):
    rows = connection.execute(
        """SELECT component.id,component.canonical_offer_event_id
             FROM progression_pathway_acceptances pathway
             JOIN progression_pathway_components component ON component.pathway_id=pathway.id
            WHERE pathway.application_cycle_id=? AND pathway.status='accepted'
              AND component.active_final=1 AND component.component_role='required'
            ORDER BY component.id""",
        (cycle_id,),
    ).fetchall()
    return [list(row) for row in rows] if rows and all(row[1] for row in rows) else []
