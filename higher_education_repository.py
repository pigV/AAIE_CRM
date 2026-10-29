"""Transactional domain operations for the higher-education module."""

import sqlite3

from revenue.access_policy import legacy_he_writes_closed


COURSE_TYPES = frozenset({"Foundation", "Diploma", "Bachelor", "Postgraduate", "Package"})
PACKAGE_PATHWAYS = frozenset({
    "Foundation + Bachelor",
    "Diploma + Bachelor",
    "Foundation + Diploma + Bachelor",
})
COURSE_LEVELS = frozenset({"Foundation", "Diploma", "Bachelor", "Master", "Postgraduate"})


def derive_parent_requested_pathway(course_type, package_combination=None):
    course_type = str(course_type or "").strip()
    if course_type == "Package":
        combination = str(package_combination or "").strip()
        if combination not in PACKAGE_PATHWAYS:
            raise HigherEducationValidationError("unsupported Package combination")
        return combination
    return {
        "Foundation": "Foundation",
        "Diploma": "Diploma",
        "Bachelor": "Direct Bachelor",
        "Postgraduate": "Direct Master",
    }.get(course_type, "")


class HigherEducationError(RuntimeError):
    pass


class HigherEducationValidationError(HigherEducationError):
    pass


class HigherEducationConflict(HigherEducationError):
    pass


def _required(value, field):
    cleaned = str(value or "").strip()
    if not cleaned:
        raise HigherEducationValidationError(f"{field} is required")
    return cleaned


def _has_column(conn, table, column):
    return column in {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def normalize_course_name(value):
    return " ".join(_required(value, "course_name").casefold().split())


def course_type_for_pathway(pathway_code, segment_count, final_course_level):
    if int(segment_count or 0) > 1:
        return "Package"
    if final_course_level == "Master":
        return "Postgraduate"
    return final_course_level


class HigherEducationRepository:
    def __init__(self, connection):
        self.conn = connection

    def create_provider(self, name, provider_type, actor_user_id, occurred_at, *, source_url=None, evidence=None, review_status="Pending"):
        provider_name = _required(name, "provider_name")
        if provider_type not in {"University", "Pathway Provider"}:
            raise HigherEducationValidationError("unsupported provider_type")
        try:
            cursor = self.conn.execute(
                """INSERT INTO higher_education_providers(
                     provider_name,normalized_name,provider_type,review_status,active_status,
                     source_url,evidence,created_by_user_id,created_at,updated_by_user_id,updated_at
                   ) VALUES(?,?,?,?, 'Active',?,?,?,?,?,?)""",
                (provider_name, " ".join(provider_name.casefold().split()), provider_type, review_status,
                 source_url, evidence, actor_user_id, occurred_at, actor_user_id, occurred_at),
            )
        except sqlite3.IntegrityError as exc:
            raise HigherEducationConflict(str(exc)) from exc
        return int(cursor.lastrowid)

    def link_pathway(self, university_id, pathway_provider_id, relationship_type, actor_user_id, occurred_at, *, effective_from=None, effective_to=None, source_url=None, evidence=None):
        if relationship_type not in {"Direct", "Partner"}:
            raise HigherEducationValidationError("unsupported relationship_type")
        types = dict(self.conn.execute(
            "SELECT id,provider_type FROM higher_education_providers WHERE id IN (?,?)",
            (university_id, pathway_provider_id),
        ).fetchall())
        if types.get(university_id) != "University" or types.get(pathway_provider_id) != "Pathway Provider":
            raise HigherEducationValidationError("relationship endpoints have invalid provider types")
        try:
            cursor = self.conn.execute(
                """INSERT INTO higher_education_provider_relationships(
                     university_id,pathway_provider_id,relationship_type,effective_from,effective_to,
                     active_status,source_url,evidence,created_by_user_id,created_at,updated_by_user_id,updated_at
                   ) VALUES(?,?,?,?,?,'Active',?,?,?,?,?,?)""",
                (university_id, pathway_provider_id, relationship_type, effective_from, effective_to,
                 source_url, evidence, actor_user_id, occurred_at, actor_user_id, occurred_at),
            )
        except sqlite3.IntegrityError as exc:
            raise HigherEducationConflict(str(exc)) from exc
        return int(cursor.lastrowid)

    def review_provider(self, provider_id, decision, actor_user_id, occurred_at):
        if decision not in {"Approved", "Rejected"}:
            raise HigherEducationValidationError("unsupported provider review decision")
        changed = self.conn.execute(
            "UPDATE higher_education_providers SET review_status=?,active_status=?,updated_by_user_id=?,updated_at=? WHERE id=? AND review_status='Pending'",
            (decision, "Active" if decision == "Approved" else "Inactive", actor_user_id, occurred_at, provider_id),
        ).rowcount
        if not changed:
            raise HigherEducationConflict("provider is not pending review")

    def create_pathway_combination(self, university_id, pathway_code, display_name, final_course_level, actor_user_id, occurred_at, *, source_url=None, evidence=None):
        if final_course_level not in {"Foundation", "Diploma", "Bachelor", "Master"}:
            raise HigherEducationValidationError("unsupported final course level")
        display_name = _required(display_name, "display_name")
        if "→" in display_name:
            raise HigherEducationValidationError("pathway display must use +")
        provider = self.conn.execute(
            "SELECT provider_type,active_status,review_status FROM higher_education_providers WHERE id=?",
            (university_id,),
        ).fetchone()
        if provider is None or provider["provider_type"] != "University" or provider["active_status"] != "Active" or provider["review_status"] != "Approved":
            raise HigherEducationValidationError("approved active university is required")
        try:
            cursor = self.conn.execute(
                """INSERT INTO higher_education_pathway_combinations(
                     university_id,pathway_code,display_name,final_course_level,active_status,
                     source_url,evidence,created_by_user_id,created_at,updated_by_user_id,updated_at
                   ) VALUES(?,?,?,?, 'Active',?,?,?,?,?,?)""",
                (university_id, _required(pathway_code, "pathway_code"), display_name, final_course_level,
                 source_url, evidence, actor_user_id, occurred_at, actor_user_id, occurred_at),
            )
        except sqlite3.IntegrityError as exc:
            raise HigherEducationConflict(str(exc)) from exc
        return int(cursor.lastrowid)

    def add_pathway_segment(self, pathway_combination_id, segment_order, provider_id, course_level, actor_user_id, occurred_at, *, source_url=None, evidence=None):
        if course_level not in {"Foundation", "Diploma", "Bachelor", "Master"}:
            raise HigherEducationValidationError("unsupported pathway segment level")
        pathway = self.conn.execute(
            "SELECT university_id FROM higher_education_pathway_combinations WHERE id=? AND active_status='Active'",
            (pathway_combination_id,),
        ).fetchone()
        if pathway is None:
            raise HigherEducationValidationError("active pathway is required")
        provider_allowed = int(provider_id) == int(pathway["university_id"]) or self.conn.execute(
            """SELECT 1 FROM higher_education_provider_relationships
               WHERE university_id=? AND pathway_provider_id=? AND active_status='Active' LIMIT 1""",
            (pathway["university_id"], provider_id),
        ).fetchone()
        if not provider_allowed:
            raise HigherEducationValidationError("provider requires an active university relationship")
        try:
            cursor = self.conn.execute(
                """INSERT INTO higher_education_pathway_segments(
                     pathway_combination_id,segment_order,provider_id,course_level,target_university_id,
                     source_url,evidence,created_by_user_id,created_at,updated_by_user_id,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (pathway_combination_id, int(segment_order), provider_id, course_level, pathway["university_id"],
                 source_url, evidence, actor_user_id, occurred_at, actor_user_id, occurred_at),
            )
        except sqlite3.IntegrityError as exc:
            raise HigherEducationConflict(str(exc)) from exc
        return int(cursor.lastrowid)

    def list_supported_pathways(self, university_id, *, active_only=True):
        sql = "SELECT * FROM higher_education_pathway_combinations WHERE university_id=?"
        if active_only:
            sql += " AND active_status='Active'"
        return self.conn.execute(sql + " ORDER BY display_name,id", (university_id,)).fetchall()

    def create_or_get_course(self, university_id, course_name, course_level, actor_user_id, occurred_at, *, specialisation=None):
        if course_level not in COURSE_LEVELS:
            raise HigherEducationValidationError("unsupported course level")
        clean_name = _required(course_name, "course_name")
        normalized = normalize_course_name(clean_name)
        existing = self.conn.execute(
            "SELECT id,active_status FROM higher_education_courses WHERE university_id=? AND normalized_name=? AND course_level=?",
            (university_id, normalized, course_level),
        ).fetchone()
        if existing:
            if existing["active_status"] != "Active":
                raise HigherEducationConflict("an inactive identical course already exists; ask an administrator to reactivate or merge it")
            return int(existing["id"]), False
        try:
            cursor = self.conn.execute(
                """INSERT INTO higher_education_courses(
                     university_id,course_name,normalized_name,course_level,specialisation,active_status,
                     created_by_user_id,created_at,updated_by_user_id,updated_at
                   ) VALUES(?,?,?,?,?,'Active',?,?,?,?)""",
                (university_id, clean_name, normalized, course_level, str(specialisation or "").strip() or None,
                 actor_user_id, occurred_at, actor_user_id, occurred_at),
            )
        except sqlite3.IntegrityError as exc:
            existing = self.conn.execute(
                "SELECT id FROM higher_education_courses WHERE university_id=? AND normalized_name=? AND course_level=?",
                (university_id, normalized, course_level),
            ).fetchone()
            if existing:
                return int(existing["id"]), False
            raise HigherEducationConflict(str(exc)) from exc
        return int(cursor.lastrowid), True

    def search_courses(self, university_id, query="", *, limit=20):
        normalized_query = " ".join(str(query or "").casefold().split())
        return self.conn.execute(
            """SELECT * FROM higher_education_courses
               WHERE university_id=? AND active_status='Active' AND normalized_name LIKE ?
               ORDER BY CASE WHEN normalized_name LIKE ? THEN 0 ELSE 1 END,course_level,course_name,id
               LIMIT ?""",
            (university_id, f"%{normalized_query}%", f"{normalized_query}%", max(1, min(int(limit), 100))),
        ).fetchall()

    def merge_course(self, duplicate_course_id, canonical_course_id, actor_user_id, occurred_at):
        duplicate = self.conn.execute(
            "SELECT university_id,course_level FROM higher_education_courses WHERE id=?",
            (duplicate_course_id,),
        ).fetchone()
        canonical = self.conn.execute(
            "SELECT university_id,course_level,active_status FROM higher_education_courses WHERE id=?",
            (canonical_course_id,),
        ).fetchone()
        if duplicate is None or canonical is None or int(duplicate_course_id) == int(canonical_course_id):
            raise HigherEducationValidationError("two distinct courses are required")
        if (
            int(duplicate["university_id"]) != int(canonical["university_id"])
            or duplicate["course_level"] != canonical["course_level"]
            or canonical["active_status"] != "Active"
        ):
            raise HigherEducationValidationError("courses must share university and level")
        for table in (
            "higher_education_plan_items",
            "higher_education_applications",
            "higher_education_package_segments",
        ):
            if _has_column(self.conn, table, "course_id"):
                self.conn.execute(
                    f"UPDATE {table} SET course_id=? WHERE course_id=?",
                    (canonical_course_id, duplicate_course_id),
                )
        self.conn.execute(
            """UPDATE higher_education_courses
               SET active_status='Inactive',merged_into_course_id=?,updated_by_user_id=?,updated_at=?
               WHERE id=?""",
            (canonical_course_id, actor_user_id, occurred_at, duplicate_course_id),
        )

    def set_course_status(self, course_id, active_status, actor_user_id, occurred_at):
        if active_status not in {"Active", "Inactive"}:
            raise HigherEducationValidationError("unsupported course status")
        cursor = self.conn.execute(
            """UPDATE higher_education_courses
               SET active_status=?,updated_by_user_id=?,updated_at=? WHERE id=?""",
            (active_status, actor_user_id, occurred_at, course_id),
        )
        if cursor.rowcount != 1:
            raise HigherEducationValidationError("course does not exist")

    def create_plan(self, cycle_id, actor_user_id, occurred_at):
        v2_schema = _has_column(self.conn, "students", "parent_requested_pathway")
        pathway_select = "s.parent_requested_pathway" if v2_schema else "NULL"
        cycle = self.conn.execute(
            f"""SELECT {pathway_select} AS parent_requested_pathway
               FROM application_cycles c JOIN students s ON s.student_id=c.student_id
               WHERE c.id=? AND c.education_stream='higher_education'""",
            (cycle_id,),
        ).fetchone()
        if cycle is None:
            raise HigherEducationValidationError("higher education cycle is required")
        try:
            if v2_schema:
                cursor = self.conn.execute(
                    "INSERT INTO higher_education_plans(application_cycle_id,status,default_pathway_code,created_by_user_id,created_at,updated_by_user_id,updated_at) VALUES(?,'Draft',?,?,?,?,?)",
                    (cycle_id, cycle["parent_requested_pathway"], actor_user_id, occurred_at, actor_user_id, occurred_at),
                )
            else:
                cursor = self.conn.execute(
                    "INSERT INTO higher_education_plans(application_cycle_id,status,created_by_user_id,created_at,updated_by_user_id,updated_at) VALUES(?,'Draft',?,?,?,?)",
                    (cycle_id, actor_user_id, occurred_at, actor_user_id, occurred_at),
                )
        except sqlite3.IntegrityError as exc:
            raise HigherEducationValidationError(str(exc)) from exc
        return int(cursor.lastrowid)

    def add_plan_item(self, plan_id, provider_id, course_type, course_name, intake, actor_user_id, occurred_at, *, pathway_combination_id=None, course_id=None):
        if course_type not in COURSE_TYPES:
            raise HigherEducationValidationError("unsupported course_type")
        plan = self.conn.execute("SELECT status FROM higher_education_plans WHERE id=?", (plan_id,)).fetchone()
        if plan is None or plan["status"] != "Draft":
            raise HigherEducationValidationError("university applications can only be added to a Draft plan")
        order = self.conn.execute("SELECT coalesce(max(item_order),0)+1 FROM higher_education_plan_items WHERE plan_id=?", (plan_id,)).fetchone()[0]
        cleaned_course_name = _required(course_name, "course_name")
        if course_id is not None:
            course = self.conn.execute(
                "SELECT course_name FROM higher_education_courses WHERE id=? AND university_id=? AND active_status='Active'",
                (course_id, provider_id),
            ).fetchone()
            if course is None:
                raise HigherEducationValidationError("course is outside selected university")
            cleaned_course_name = course["course_name"]
        normalized_course_name = normalize_course_name(cleaned_course_name)
        self.conn.execute(
            """INSERT INTO higher_education_course_names(course_name,normalized_name,created_by_user_id,created_at,updated_at)
               VALUES(?,?,?,?,?) ON CONFLICT(normalized_name) DO UPDATE SET updated_at=excluded.updated_at""",
            (cleaned_course_name, normalized_course_name, actor_user_id, occurred_at, occurred_at),
        )
        course_name_id = self.conn.execute("SELECT id FROM higher_education_course_names WHERE normalized_name=?", (normalized_course_name,)).fetchone()[0]
        self.conn.execute(
            """INSERT INTO higher_education_provider_course_names(provider_id,course_name_id,usage_count,last_used_at)
               VALUES(?,?,1,?) ON CONFLICT(provider_id,course_name_id) DO UPDATE SET usage_count=usage_count+1,last_used_at=excluded.last_used_at""",
            (provider_id, course_name_id, occurred_at),
        )
        if _has_column(self.conn, "higher_education_plan_items", "pathway_combination_id"):
            cursor = self.conn.execute(
                """INSERT INTO higher_education_plan_items(plan_id,provider_id,course_type,course_name,intake,item_order,pathway_combination_id,course_id,created_by_user_id,created_at,updated_by_user_id,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (plan_id, provider_id, course_type, cleaned_course_name, str(intake or "").strip() or None, order,
                 pathway_combination_id, course_id, actor_user_id, occurred_at, actor_user_id, occurred_at),
            )
        else:
            cursor = self.conn.execute(
                """INSERT INTO higher_education_plan_items(plan_id,provider_id,course_type,course_name,intake,item_order,created_by_user_id,created_at,updated_by_user_id,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (plan_id, provider_id, course_type, cleaned_course_name, str(intake or "").strip() or None, order, actor_user_id, occurred_at, actor_user_id, occurred_at),
            )
        return int(cursor.lastrowid)

    def remove_draft_plan_item(self, item_id):
        row = self.conn.execute(
            """SELECT i.id,p.status
               FROM higher_education_plan_items i
               JOIN higher_education_plans p ON p.id=i.plan_id
               WHERE i.id=?""",
            (item_id,),
        ).fetchone()
        if row is None:
            raise HigherEducationValidationError("plan item does not exist")
        if row["status"] != "Draft":
            raise HigherEducationValidationError("only Draft university applications can be removed")
        if self.conn.execute(
            "SELECT 1 FROM higher_education_applications WHERE source_plan_item_id=? LIMIT 1",
            (item_id,),
        ).fetchone():
            raise HigherEducationValidationError("submitted university application cannot be removed")
        self.conn.execute("DELETE FROM higher_education_plan_items WHERE id=?", (item_id,))

    def confirm_plan(self, plan_id, actor_user_id, occurred_at):
        pathway_rows = self.conn.execute(
            """SELECT c.id,c.final_course_level,
                      (SELECT s.course_level FROM higher_education_pathway_segments s
                       WHERE s.pathway_combination_id=c.id ORDER BY s.segment_order DESC LIMIT 1) terminal_level,
                      (SELECT count(*) FROM higher_education_pathway_segments s
                       WHERE s.pathway_combination_id=c.id) segment_count
               FROM higher_education_plan_items i
               JOIN higher_education_pathway_combinations c ON c.id=i.pathway_combination_id
               WHERE i.plan_id=?""",
            (plan_id,),
        ).fetchall() if self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='higher_education_pathway_combinations'"
        ).fetchone() else ()
        for pathway in pathway_rows:
            if not pathway["segment_count"]:
                raise HigherEducationValidationError("selected pathway requires configured segments")
            if pathway["terminal_level"] != pathway["final_course_level"]:
                raise HigherEducationValidationError("pathway terminal segment must match final course level")
        changed = self.conn.execute(
            "UPDATE higher_education_plans SET status='Confirmed',confirmed_at=?,confirmed_by_user_id=?,updated_by_user_id=?,updated_at=? WHERE id=? AND status='Draft'",
            (occurred_at, actor_user_id, actor_user_id, occurred_at, plan_id),
        ).rowcount
        if not changed:
            raise HigherEducationConflict("only a Draft plan can be confirmed")

    def submit_plan_item(self, plan_item_id, channel, intake, actor_user_id, occurred_at, *, master_agent_id=None):
        row = self.conn.execute(
            """SELECT i.*,p.application_cycle_id,p.status plan_status
               FROM higher_education_plan_items i JOIN higher_education_plans p ON p.id=i.plan_id WHERE i.id=?""",
            (plan_item_id,),
        ).fetchone()
        if row is None or row["plan_status"] != "Confirmed":
            raise HigherEducationValidationError("plan must be confirmed before submission")
        return self.create_application(
            row["application_cycle_id"], row["provider_id"], row["course_type"], row["course_name"],
            intake or row["intake"], channel, actor_user_id, occurred_at,
            source_plan_item_id=plan_item_id, master_agent_id=master_agent_id,
            pathway_combination_id=row["pathway_combination_id"] if "pathway_combination_id" in row.keys() else None,
            course_id=row["course_id"] if "course_id" in row.keys() else None,
        )

    def create_application(self, cycle_id, provider_id, course_type, course_name, intake, channel, actor_user_id, occurred_at, *, source_plan_item_id=None, master_agent_id=None, pathway_combination_id=None, course_id=None):
        if course_type not in COURSE_TYPES:
            raise HigherEducationValidationError("unsupported course_type")
        intake = _required(intake, "intake")
        if channel not in {"Direct", "Master Agent"} or (channel == "Master Agent" and master_agent_id is None):
            raise HigherEducationValidationError("valid submission channel is required")
        try:
            if _has_column(self.conn, "higher_education_applications", "pathway_combination_id"):
                cursor = self.conn.execute(
                    """INSERT INTO higher_education_applications(
                         application_cycle_id,source_plan_item_id,provider_id,course_type,course_name,intake,
                         submission_channel,master_agent_id,status,is_current,submitted_at,
                         pathway_combination_id,course_id,created_by_user_id,created_at,updated_by_user_id,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,'Submitted',1,?,?,?,?,?,?,?)""",
                    (cycle_id, source_plan_item_id, provider_id, course_type, _required(course_name, "course_name"), intake,
                     channel, master_agent_id, occurred_at, pathway_combination_id, course_id,
                     actor_user_id, occurred_at, actor_user_id, occurred_at),
                )
            else:
                cursor = self.conn.execute(
                    """INSERT INTO higher_education_applications(
                         application_cycle_id,source_plan_item_id,provider_id,course_type,course_name,intake,
                         submission_channel,master_agent_id,status,is_current,submitted_at,
                         created_by_user_id,created_at,updated_by_user_id,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,'Submitted',1,?,?,?,?,?)""",
                    (cycle_id, source_plan_item_id, provider_id, course_type, _required(course_name, "course_name"), intake,
                     channel, master_agent_id, occurred_at, actor_user_id, occurred_at, actor_user_id, occurred_at),
                )
        except sqlite3.IntegrityError as exc:
            raise HigherEducationConflict(str(exc)) from exc
        application_id = int(cursor.lastrowid)
        if course_type == "Package" and pathway_combination_id is not None:
            segments = self.conn.execute(
                """SELECT * FROM higher_education_pathway_segments
                   WHERE pathway_combination_id=? ORDER BY segment_order""",
                (pathway_combination_id,),
            ).fetchall()
            if not segments:
                raise HigherEducationValidationError("Package pathway requires configured segments")
            final_order = max(int(segment["segment_order"]) for segment in segments)
            for segment in segments:
                level = "Postgraduate" if segment["course_level"] == "Master" else segment["course_level"]
                is_final = int(segment["segment_order"]) == final_order
                self.conn.execute(
                    """INSERT INTO higher_education_package_segments(
                         application_id,segment_order,teaching_provider_id,course_type,course_name,intake,
                         target_university_id,course_id,is_final_degree,
                         created_by_user_id,created_at,updated_by_user_id,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (application_id, segment["segment_order"], segment["provider_id"], level,
                     _required(course_name, "course_name") if is_final else segment["course_level"], intake,
                     segment["target_university_id"], course_id if is_final else None, 1 if is_final else 0,
                     actor_user_id, occurred_at, actor_user_id, occurred_at),
                )
        # This remains a cutover-aware, no-op bridge when Revenue is absent or
        # disabled.  It references the authoritative HE row and never copies it.
        from revenue.operational_integration import (
            process_revenue_higher_education_change,
        )
        process_revenue_higher_education_change(
            self.conn, application_id, actor_user_id, occurred_at
        )
        return application_id

    def add_package_segment(self, application_id, segment_order, teaching_provider_id, course_type, course_name, intake, actor_user_id, occurred_at):
        if _has_column(self.conn, "higher_education_package_segments", "target_university_id"):
            raise HigherEducationValidationError("Package Segments are managed by the approved pathway architecture")
        try:
            cursor = self.conn.execute(
                """INSERT INTO higher_education_package_segments(
                     application_id,segment_order,teaching_provider_id,course_type,course_name,intake,
                     created_by_user_id,created_at,updated_by_user_id,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (application_id, int(segment_order), teaching_provider_id, course_type,
                 _required(course_name, "course_name"), _required(intake, "intake"),
                 actor_user_id, occurred_at, actor_user_id, occurred_at),
            )
        except sqlite3.IntegrityError as exc:
            raise HigherEducationConflict(str(exc)) from exc
        return int(cursor.lastrowid)

    def record_offer(
        self,
        application_id,
        occurred_at,
        *,
        package_segment_id=None,
        actor_user_id=None,
        actor_name=None,
    ):
        application = self.conn.execute(
            """SELECT application.application_cycle_id,provider.provider_name
               FROM higher_education_applications AS application
               JOIN higher_education_providers AS provider
                 ON provider.id=application.provider_id
               WHERE application.id=?""",
            (application_id,),
        ).fetchone()
        if application is None:
            raise HigherEducationValidationError("application does not exist")
        try:
            cursor = self.conn.execute(
                "INSERT INTO higher_education_offers(application_id,application_cycle_id,package_segment_id,status,received_at,created_at,updated_at) VALUES(?,?,?,'Received',?,?,?)",
                (application_id, application["application_cycle_id"], package_segment_id, occurred_at, occurred_at, occurred_at),
            )
        except sqlite3.IntegrityError as exc:
            raise HigherEducationConflict(str(exc)) from exc
        offer_id = int(cursor.lastrowid)
        if _has_column(
            self.conn, "offer_received_events", "higher_education_offer_id"
        ):
            from workflow_offer_events import (
                WorkflowEventConflictError,
                record_offer_received,
            )

            try:
                record_offer_received(
                    self.conn,
                    int(application["application_cycle_id"]),
                    higher_education_offer_id=offer_id,
                    external_offer_key=f"he-offer:{offer_id}",
                    school_name=str(application["provider_name"]),
                    received_at=occurred_at,
                    actor_user_id=actor_user_id,
                    actor_name=actor_name,
                )
            except WorkflowEventConflictError as exc:
                raise HigherEducationConflict(str(exc)) from exc
        return offer_id

    def accept_offer(self, offer_id, actor_user_id, occurred_at):
        row = self.conn.execute(
            """SELECT o.status,a.application_cycle_id FROM higher_education_offers o
               JOIN higher_education_applications a ON a.id=o.application_id WHERE o.id=?""",
            (offer_id,),
        ).fetchone()
        if row is None or row["status"] != "Received":
            raise HigherEducationValidationError("offer is not available for acceptance")
        existing = self.conn.execute(
            """SELECT 1 FROM higher_education_offers o JOIN higher_education_applications a ON a.id=o.application_id
               WHERE a.application_cycle_id=? AND o.status='Accepted' LIMIT 1""",
            (row["application_cycle_id"],),
        ).fetchone()
        if existing:
            raise HigherEducationConflict("application cycle already has an accepted offer")
        self.conn.execute(
            "UPDATE higher_education_offers SET status='Accepted',accepted_at=?,accepted_by_user_id=?,updated_at=? WHERE id=?",
            (occurred_at, actor_user_id, occurred_at, offer_id),
        )
        if _has_column(
            self.conn, "offer_received_events", "higher_education_offer_id"
        ):
            from workflow_offer_events import (
                WorkflowEventConflictError,
                select_final_acceptance,
            )

            canonical_offer = self.conn.execute(
                """SELECT id FROM offer_received_events
                   WHERE higher_education_offer_id=?""",
                (int(offer_id),),
            ).fetchone()
            if canonical_offer is None:
                raise HigherEducationConflict(
                    "higher education Offer is missing its canonical received event"
                )
            try:
                select_final_acceptance(
                    self.conn,
                    int(row["application_cycle_id"]),
                    int(canonical_offer["id"]),
                    selected_at=occurred_at,
                    actor_user_id=actor_user_id,
                )
            except WorkflowEventConflictError as exc:
                raise HigherEducationConflict(str(exc)) from exc

    def propose_temporary_commission_share(self, application_id, share_percent, actor_user_id, occurred_at, *, package_segment_id=None, evidence=None):
        if legacy_he_writes_closed(self.conn):
            raise HigherEducationConflict("LEGACY_HE_COMMISSION_CLOSED")
        evidence = _required(evidence, "evidence")
        try:
            percent = float(share_percent)
        except (TypeError, ValueError) as exc:
            raise HigherEducationValidationError("share_percent must be numeric") from exc
        if not 0 < percent <= 100:
            raise HigherEducationValidationError("share_percent must be between 0 and 100")
        try:
            cursor = self.conn.execute(
                """INSERT INTO higher_education_temporary_commission_shares(
                     application_id,package_segment_id,share_percent,status,evidence,
                     created_by_user_id,created_at,updated_at
                   ) VALUES(?,?,?,'Pending',?,?,?,?)""",
                (application_id, package_segment_id, percent, evidence, actor_user_id, occurred_at, occurred_at),
            )
        except sqlite3.IntegrityError as exc:
            raise HigherEducationConflict(str(exc)) from exc
        return int(cursor.lastrowid)

    def review_temporary_commission_share(self, share_id, decision, actor_user_id, occurred_at, *, reason=None):
        if legacy_he_writes_closed(self.conn):
            raise HigherEducationConflict("LEGACY_HE_COMMISSION_CLOSED")
        if decision not in {"Approved", "Rejected"}:
            raise HigherEducationValidationError("unsupported temporary share review decision")
        changed = self.conn.execute(
            """UPDATE higher_education_temporary_commission_shares
               SET status=?,review_reason=?,reviewed_by_user_id=?,reviewed_at=?,updated_at=?
               WHERE id=? AND status='Pending'""",
            (decision, str(reason or "").strip() or None, actor_user_id, occurred_at, occurred_at, share_id),
        ).rowcount
        if not changed:
            raise HigherEducationConflict("temporary share is not pending review")

    def create_commission_line(self, application_id, teaching_provider_id, source_type, source_id, actor_user_id, occurred_at, *, package_segment_id=None):
        if legacy_he_writes_closed(self.conn):
            raise HigherEducationConflict("LEGACY_HE_COMMISSION_CLOSED")
        if source_type not in {"Agreement", "Temporary Share"}:
            raise HigherEducationValidationError("unsupported commission source")
        try:
            cursor = self.conn.execute(
                """INSERT INTO higher_education_commission_lines(
                     application_id,package_segment_id,teaching_provider_id,source_type,source_id,
                     status,created_by_user_id,created_at,updated_at
                   ) VALUES(?,?,?,?,?,'Draft',?,?,?)""",
                (application_id, package_segment_id, teaching_provider_id, source_type, int(source_id), actor_user_id, occurred_at, occurred_at),
            )
        except sqlite3.IntegrityError as exc:
            raise HigherEducationConflict(str(exc)) from exc
        return int(cursor.lastrowid)

    def link_coe(self, application_id, coe_oshc_id, actor_user_id, occurred_at, *, package_segment_id=None):
        scope = "Package Segment" if package_segment_id is not None else "Whole Application"
        try:
            cursor = self.conn.execute(
                """INSERT INTO higher_education_coe_links(
                     application_id,package_segment_id,coe_oshc_id,scope,created_by_user_id,created_at
                   ) VALUES(?,?,?,?,?,?)""",
                (application_id, package_segment_id, coe_oshc_id, scope, actor_user_id, occurred_at),
            )
        except sqlite3.IntegrityError as exc:
            raise HigherEducationConflict(str(exc)) from exc
        return int(cursor.lastrowid)
