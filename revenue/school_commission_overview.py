"""Read-only student counts for the school contract register."""

from collections import defaultdict
from urllib.parse import urlencode

from revenue import vic_rcti
from revenue.institution_commission_draft import InstitutionCommissionDraft
from revenue.read_models import RevenueReadModels
from revenue.repository import RevenueRepository
from revenue.state_school_commission import school_commission_assignment


class SchoolCommissionOverview:
    def __init__(self, connection, intake, actor_id, *, can_view_claims, formal_candidates=None):
        self.connection = connection
        self.model = RevenueReadModels(connection)
        self.repository = RevenueRepository(connection)
        self.intake = intake
        self.actor_id = actor_id
        self.can_view_claims = can_view_claims
        self.formal_candidates = formal_candidates
        self.assignment = school_commission_assignment(connection)

    def _authority_school_ids(self, state):
        return [school_id for school_id, assignment in self.assignment.items()
                if assignment == ("public", state)]

    def _memberships(self, target, *, authority_school_id=None):
        kind, key = target
        visible = self.model.visibility.student_predicate("s")
        if kind == "provider":
            return self.connection.execute(
                f"""SELECT c.student_id, '已最终确认' AS source, a.intake,
                           NULL AS case_id, c.id AS cycle_id
                    FROM higher_education_applications a
                    JOIN application_cycles c ON c.id=a.application_cycle_id
                    JOIN students s ON s.student_id=c.student_id
                    WHERE a.provider_id=? AND a.is_current=1 AND c.education_stream='higher_education'
                      AND a.status NOT IN ('Closed','Withdrawn','Superseded')
                      AND EXISTS(SELECT 1 FROM higher_education_offers o
                                 WHERE o.application_id=a.id AND o.application_cycle_id=c.id AND o.status='Accepted')
                      AND {visible}
                    ORDER BY c.student_id,c.id DESC""", (key,),
            ).fetchall()
        school_ids = [key] if kind == "school" else self._authority_school_ids(key)
        if authority_school_id is not None:
            school_ids = [authority_school_id]
        if not school_ids:
            return []
        placeholders = ",".join("?" for _ in school_ids)
        return self.connection.execute(
            f"""WITH membership AS (
                    SELECT student_id, school_id, '当前在读' AS source, start_date AS intake,
                           NULL AS case_id, NULL AS cycle_id, 0 AS priority
                    FROM student_school_enrolments
                    WHERE school_id IN ({placeholders}) AND status='active' AND enrolment_role='current'
                    UNION ALL
                    SELECT a.student_id, a.school_id, '已最终确认', NULL, r.id, a.application_cycle_id, 1
                    FROM cycle_final_acceptances a
                    LEFT JOIN revenue_commercial_cases r ON r.application_cycle_id=a.application_cycle_id
                         AND r.student_id=a.student_id
                    WHERE a.school_id IN ({placeholders}) AND a.acceptance_status='active'
                )
                SELECT m.student_id, m.school_id, m.source, m.intake, m.case_id, m.cycle_id
                FROM membership m JOIN students s ON s.student_id=m.student_id
                WHERE {visible} ORDER BY m.student_id,m.priority,m.cycle_id DESC""",
            school_ids + school_ids,
        ).fetchall()

    def _candidates(self, target, school):
        if not self.can_view_claims:
            return "申领权限未开放", []
        if target[0] == "school":
            sector, state = self.assignment.get(target[1], ("", ""))
            if sector == "public" and state in self.intake.education_authorities:
                return "由州／领地教育局结算", []
        candidates = []
        available = False
        for uid in dict.fromkeys(uid for _, _, uid in school["confirmed"] if uid):
            workflow = InstitutionCommissionDraft(self.repository, self.intake, uid, self.actor_id)
            terms = workflow.terms()
            if not terms:
                continue
            if terms.get("payment_mode") == vic_rcti.PAYMENT:
                return "按 RCTI 结算", []
            available = True
            candidates.extend(workflow.candidates(terms))
        if target[0] == "school" and self.formal_candidates:
            formal = self.formal_candidates(target[1])
            if formal is not None:
                available = True
                for row in formal:
                    forecast = row["forecast"]
                    candidates.append({
                        **row, "reason": " ".join(row["reasons"]),
                        "uid": row["component_uid"] if not row["reasons"] and forecast else None,
                        "academic_period": forecast["tuition"].get("academic_period", "first_year") if forecast else None,
                        "gross_cents": forecast["gross_cents"] if forecast else None,
                    })
        return ("" if available else "暂未开放申领"), candidates

    def authority_schools(self, target, contract, *, include_empty=False):
        school_ids = self._authority_school_ids(target[1])
        if not school_ids:
            return []
        members = defaultdict(set)
        for row in self._memberships(target):
            members[row["school_id"]].add(row["student_id"])
        status, candidates = self._candidates(target, contract)
        eligible = defaultdict(set)
        for row in candidates:
            school_id = row.get("school_id")
            if row.get("uid") and row["student_id"] in members.get(school_id, ()):
                eligible[school_id].add(row["student_id"])
        placeholders = ",".join("?" for _ in school_ids)
        schools = self.connection.execute(
            f"SELECT id, school_name FROM schools WHERE id IN ({placeholders})", school_ids,
        ).fetchall()
        rows = [
            {"id": sid, "name": name, "students": len(members[sid]),
             "claimable": None if status else len(eligible[sid]), "status": status}
            for sid, name in schools if include_empty or sid in members
        ]
        return sorted(rows, key=lambda row: (not row["students"], row["name"].casefold(), row["id"]))

    def read(self, target, school, *, view=None, authority_school_id=None):
        if authority_school_id is not None and (
            target[0] != "authority" or self.assignment.get(authority_school_id) != ("public", target[1])
        ):
            raise LookupError("School is outside this education authority")
        members = defaultdict(list)
        for row in self._memberships(target, authority_school_id=authority_school_id):
            members[row["student_id"]].append(row)
        status, candidates = self._candidates(target, school)
        projects = defaultdict(dict)
        blocked = defaultdict(list)
        for row in candidates:
            sid = row["student_id"]
            if sid not in members or (
                authority_school_id is not None and row.get("school_id") != authority_school_id
            ):
                continue
            if row.get("uid"):
                period = row.get("academic_period")
                period = "first_year" if period == "year_1" else period
                key = row["component_uid"], period
                projects[sid].setdefault(key, {**row, "academic_period": period})
            elif row.get("reason") and row["reason"] not in blocked[sid]:
                blocked[sid].append(row["reason"])
        result = {"students": len(members), "claimable": len(projects) if not status else None, "status": status}
        if view is None:
            return result
        if authority_school_id is not None:
            result["school_name"] = self.connection.execute(
                "SELECT school_name FROM schools WHERE id=?", (authority_school_id,),
            ).fetchone()[0]
        rows = []
        for sid, memberships in members.items():
            if view == "claimable" and sid not in projects:
                continue
            sources = dict.fromkeys(row["source"] for row in memberships)
            intakes = {}
            for row in memberships:
                value = row["intake"]
                if not value and row["case_id"] is not None:
                    _, value, _ = self.model._case_authoritative_context(row["case_id"], row["cycle_id"])
                if value and value != "—":
                    intakes[value] = None
            rows.append({
                "id": sid, "name": self.model._student_display_name(sid),
                "href": "/revenue/cases?" + urlencode({"q": sid}),
                "sources": "、".join(sources), "intake": " · ".join(intakes),
                "projects": list(projects.get(sid, {}).values()), "reasons": blocked.get(sid, []),
            })
        return {**result, "rows": rows}
