"""Current Revenue visibility derived from the authoritative CRM Student record.

This adapter is query-only.  It never changes Student or Revenue lifecycle state and
must not be used to delete or suppress immutable financial history.
"""

from __future__ import annotations

import sqlite3


class RevenueOperationalVisibility:
    """Build one authoritative predicate for current Revenue work surfaces."""

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self._student_columns = {
            str(row[1])
            for row in connection.execute('PRAGMA table_info("students")').fetchall()
        }

    def student_predicate(self, alias: str) -> str:
        clauses = [f"{alias}.student_id IS NOT NULL"]
        if "deleted_at" in self._student_columns:
            clauses.append(f"{alias}.deleted_at IS NULL")
        if "form_data_json" in self._student_columns:
            clauses.append(
                "NOT (CASE "
                f"WHEN json_valid(COALESCE({alias}.form_data_json,'')) "
                f"THEN COALESCE(json_extract({alias}.form_data_json,'$.synthetic_acceptance'),0) "
                "ELSE 0 END = 1)"
            )
        return " AND ".join(clauses)

    def case_predicate(self, case_alias: str = "c") -> str:
        return (
            "EXISTS(SELECT 1 FROM students revenue_visible_student "
            f"WHERE revenue_visible_student.student_id={case_alias}.student_id "
            f"AND {self.student_predicate('revenue_visible_student')})"
        )

    def is_student_visible(self, student_id: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM students revenue_visible_student "
            "WHERE revenue_visible_student.student_id=? AND "
            + self.student_predicate("revenue_visible_student"),
            (str(student_id),),
        ).fetchone()
        return row is not None

    def operational_exception_predicate(self, exception_alias: str = "e") -> str:
        case_visible = self.case_predicate("revenue_exception_case")
        component_visible = self.case_predicate("revenue_component_case")
        return f"""(
            ({exception_alias}.aggregate_type='commercial_case' AND EXISTS(
                SELECT 1 FROM revenue_commercial_cases revenue_exception_case
                 WHERE revenue_exception_case.uid={exception_alias}.aggregate_uid
                   AND {case_visible}
            ))
            OR ({exception_alias}.aggregate_type='enrolment_component' AND EXISTS(
                SELECT 1 FROM revenue_enrolment_components revenue_exception_component
                JOIN revenue_commercial_cases revenue_component_case
                  ON revenue_component_case.id=revenue_exception_component.commercial_case_id
                 WHERE revenue_exception_component.uid={exception_alias}.aggregate_uid
                   AND {component_visible}
            ))
            OR {exception_alias}.aggregate_type NOT IN ('commercial_case','enrolment_component')
        )"""

    def operational_suggestion_predicate(self, suggestion_alias: str = "s") -> str:
        """Keep non-case suggestions and hide commission targets for non-current Students."""
        visible_case = self.case_predicate("revenue_suggestion_case")
        return f"""(
            {suggestion_alias}.suggested_target_kind<>'commission_line'
            OR EXISTS(
                SELECT 1 FROM revenue_commission_lines revenue_suggestion_line
                JOIN revenue_commercial_cases revenue_suggestion_case
                  ON revenue_suggestion_case.id=revenue_suggestion_line.commercial_case_id
                 WHERE revenue_suggestion_line.uid={suggestion_alias}.suggested_target_uid
                   AND {visible_case}
            )
        )"""
