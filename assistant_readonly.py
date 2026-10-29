from assistant_nlp import parse_assistant_query


def canonical_student_workflow(db_handle, student_id):
    if not hasattr(db_handle, "connect"):
        return None
    from student_workflow_projection import load_student_workflow_projection_map
    from workflow_cutover import is_workflow_v2_active

    connection = db_handle.connect()
    try:
        if not is_workflow_v2_active(connection):
            return None
        return load_student_workflow_projection_map(connection, (student_id,)).get(student_id)
    finally:
        release = getattr(db_handle, "_release_connection", None)
        if release is not None:
            release(connection)


READONLY_SOURCES = {
    "student_basic": [
        "CRM students", "CRM workflow_status", "Workflow V2 canonical projection",
    ],
    "application_files": ["CRM student_documents"],
    "offer_status": ["CRM offers", "CRM student_documents"],
    "coe_oshc_status": ["CRM coe_oshc", "CRM student_documents"],
    "contract_status": ["CRM contracts"],
    "follow_up_students": [
        "CRM reminders", "CRM students", "CRM workflow_status",
        "Workflow V2 canonical projection",
    ],
    "data_update_health": ["CRM data_sources", "CRM data_update_jobs", "CRM data_update_logs"],
    "missing_key_documents": ["CRM students", "CRM student_documents"],
    "workflow_application_status": [
        "CRM students", "CRM workflow_status", "Workflow V2 canonical projection",
        "CRM student_documents", "CRM offers", "CRM coe_oshc",
    ],
}

STUDENT_COLUMNS = """student_id,passport_first_name,passport_last_name,english_nickname,current_status,
                     current_year_level,preferred_intake_year,preferred_intake_term,applying_year_level,
                     visa_status,nas_status,created_date,updated_at,deleted_at"""

KEY_APPLICATION_DOCUMENTS = (
    "StudentPassport",
    "BirthCertificate",
    "Transcripts",
    "Immunization",
)


def rowdict(row):
    return dict(row) if row else None


def rowsdict(rows):
    return [dict(row) for row in rows]


def student_display_name(student):
    if not student:
        return ""
    return " ".join(
        part.strip()
        for part in (student.get("passport_first_name") or "", student.get("passport_last_name") or "")
        if part and part.strip()
    ).strip() or student.get("student_id", "Unknown student")


def find_student_matches(db_handle, filters, limit=6):
    student_id = filters.get("student_id")
    if student_id:
        row = rowdict(db_handle.one(
            f"""SELECT {STUDENT_COLUMNS}
                FROM students
                WHERE student_id=? AND deleted_at IS NULL""",
            (student_id,),
        ))
        return [row] if row else []
    student_name = filters.get("student_name")
    if student_name:
        name_tokens = [
            token.casefold()
            for token in str(student_name).replace(",", " ").split()
            if token.strip()
        ]
        if not name_tokens:
            return []
        like_params = [f"%{token}%" for token in name_tokens]
        conditions = []
        params = []
        for pattern in like_params:
            conditions.extend([
                "lower(COALESCE(passport_first_name,'')) LIKE ?",
                "lower(COALESCE(passport_last_name,'')) LIKE ?",
                "lower(COALESCE(english_nickname,'')) LIKE ?",
            ])
            params.extend([pattern, pattern, pattern])
        candidates = rowsdict(db_handle.all(
            f"""SELECT {STUDENT_COLUMNS}
                FROM students
                WHERE deleted_at IS NULL
                  AND ({' OR '.join(conditions)})
                ORDER BY updated_at DESC, student_id
                LIMIT 50""",
            tuple(params),
        ))
        scored = []
        requested = " ".join(name_tokens)
        requested_reversed = " ".join(reversed(name_tokens))
        for row in candidates:
            first = str(row.get("passport_first_name") or "").casefold()
            last = str(row.get("passport_last_name") or "").casefold()
            nickname = str(row.get("english_nickname") or "").casefold()
            first_last = " ".join(part for part in (first, last) if part)
            last_first = " ".join(part for part in (last, first) if part)
            searchable_tokens = {token for token in (first, last, nickname) if token}
            score = 0
            if requested in (first_last, last_first) or requested_reversed in (first_last, last_first):
                score = 100
            elif all(token in searchable_tokens for token in name_tokens):
                score = 90
            elif len(name_tokens) == 1 and name_tokens[0] in searchable_tokens:
                score = 80
            elif all(token in first_last or token in last_first or token in nickname for token in name_tokens):
                score = 60
            if score:
                scored.append((score, row))
        if not scored:
            return []
        best_score = max(score for score, _row in scored)
        best_rows = [row for score, row in scored if score == best_score]
        return best_rows[:limit]
    return []


def find_student(db_handle, filters):
    matches = find_student_matches(db_handle, filters, limit=2)
    return matches[0] if len(matches) == 1 else None


def require_student(db_handle, parsed):
    matches = find_student_matches(db_handle, parsed["filters"], limit=6)
    if len(matches) == 1:
        return None, matches[0]
    if len(matches) > 1:
        return {
            "ok": False,
            "intent": parsed["intent"],
            "answer": "找到多个匹配学生，请提供 Student ID 或更完整的英文姓名后再查询。",
            "data": {
                "filters": parsed["filters"],
                "matches": [
                    {
                        "student_id": row.get("student_id"),
                        "name": student_display_name(row),
                        "current_status": row.get("current_status"),
                    }
                    for row in matches
                ],
            },
            "readonly": True,
            "sources": ["CRM students"],
        }, None
    return {
        "ok": False,
        "intent": parsed["intent"],
        "answer": "没有找到匹配的学生。请提供 Student ID 或更完整的英文姓名。",
        "data": {"filters": parsed["filters"]},
        "readonly": True,
        "sources": ["CRM students"],
    }, None


def student_basic(db_handle, parsed):
    missing, student = require_student(db_handle, parsed)
    if missing:
        return missing
    workflow = rowdict(db_handle.one(
        "SELECT current_stage,materials_checked FROM workflow_status WHERE student_id=?",
        (student["student_id"],),
    )) or {}
    canonical = canonical_student_workflow(db_handle, student["student_id"])
    if canonical is not None:
        workflow["current_stage"] = canonical.stage_label
        workflow["stage_key"] = canonical.stage_key
        workflow["projection_state"] = canonical.state
    answer = (
        f"{student_display_name(student)}（{student['student_id']}）当前状态为 "
        f"{student.get('current_status') or 'Unknown'}，流程阶段为 {workflow.get('current_stage') or '未记录'}。"
    )
    return {
        "ok": True,
        "intent": parsed["intent"],
        "answer": answer,
        "data": {"student": student, "workflow": workflow},
        "readonly": True,
        "sources": READONLY_SOURCES["student_basic"],
    }


def application_files(db_handle, parsed):
    missing, student = require_student(db_handle, parsed)
    if missing:
        return missing
    rows = rowsdict(db_handle.all(
        """SELECT document_type,document_status,COUNT(*) count,MAX(uploaded_at) latest_uploaded_at
           FROM student_documents
           WHERE student_id=? AND is_active=1 AND folder_category LIKE ?
           GROUP BY document_type,document_status
           ORDER BY document_type""",
        (student["student_id"], "%Application%"),
    ))
    total = sum(row["count"] for row in rows)
    answer = f"{student_display_name(student)} 的 Application 文件中心当前有 {total} 个有效文件记录。"
    return {
        "ok": True,
        "intent": parsed["intent"],
        "answer": answer,
        "data": {"student": student, "file_summary": rows, "total": total},
        "readonly": True,
        "sources": READONLY_SOURCES["application_files"],
    }


def offer_status(db_handle, parsed):
    missing, student = require_student(db_handle, parsed)
    if missing:
        return missing
    offers = rowsdict(db_handle.all(
        """SELECT id,school_name,document_type,offer_date,offer_expiry_date,offer_status,accepted_date,uploaded_at
           FROM offers
           WHERE student_id=?
           ORDER BY created_at DESC, id DESC""",
        (student["student_id"],),
    ))
    answer = f"{student_display_name(student)} 当前有 {len(offers)} 条 Offer 记录。"
    return {
        "ok": True,
        "intent": parsed["intent"],
        "answer": answer,
        "data": {"student": student, "offers": offers},
        "readonly": True,
        "sources": READONLY_SOURCES["offer_status"],
    }


def _latest_record(records, kinds):
    wanted = {kind.casefold() for kind in kinds}
    for row in records:
        if str(row.get("document_kind") or "").casefold() in wanted:
            return row
    return None


def _documents_for_type(documents, document_type):
    needle = document_type.casefold()
    return [
        row
        for row in documents
        if needle in str(row.get("document_type") or "").casefold()
    ]


def _document_summary(documents):
    return [
        {
            "document_type": row.get("document_type"),
            "original_name": row.get("original_name"),
            "uploaded_at": row.get("uploaded_at"),
            "document_status": row.get("document_status"),
        }
        for row in documents
    ]


def _coe_oshc_summary(records, documents):
    coe_record = _latest_record(records, ("COE",))
    oshc_record = _latest_record(records, ("OSHC",))
    caaw_record = _latest_record(records, ("CAAW",))
    coe_documents = _documents_for_type(documents, "COE")
    oshc_documents = _documents_for_type(documents, "OSHC")
    caaw_documents = _documents_for_type(documents, "CAAW")
    coe_file_path = coe_record.get("file_path") if coe_record else ""
    oshc_file_path = oshc_record.get("file_path") if oshc_record else ""
    caaw_file_path = caaw_record.get("file_path") if caaw_record else ""
    return {
        "coe": {
            "school": (
                (coe_record or {}).get("school_name")
                or (coe_record or {}).get("coe_provider")
                or ""
            ),
            "start_date": (coe_record or {}).get("coe_start_date") or "",
            "end_date": (coe_record or {}).get("coe_end_date") or "",
            "file_uploaded": bool(coe_file_path or coe_documents),
            "file_path": coe_file_path or "",
            "files": _document_summary(coe_documents),
            "latest_record": coe_record,
        },
        "oshc": {
            "provider": (
                (oshc_record or {}).get("oshc_provider")
                or (oshc_record or {}).get("oshc_company_name")
                or ""
            ),
            "policy_number": (oshc_record or {}).get("oshc_policy_number") or "",
            "start_date": (
                (oshc_record or {}).get("oshc_commencement_date")
                or (oshc_record or {}).get("oshc_start_date")
                or ""
            ),
            "end_date": (
                (oshc_record or {}).get("oshc_expiry_date")
                or (oshc_record or {}).get("oshc_end_date")
                or ""
            ),
            "file_uploaded": bool(oshc_file_path or oshc_documents),
            "file_path": oshc_file_path or "",
            "files": _document_summary(oshc_documents),
            "latest_record": oshc_record,
        },
        "caaw": {
            "file_uploaded": bool(caaw_file_path or caaw_documents),
            "file_path": caaw_file_path or "",
            "files": _document_summary(caaw_documents),
            "latest_record": caaw_record,
        },
    }


def _latest_update(records, documents):
    values = [
        row.get("created_at")
        for row in records
        if row.get("created_at")
    ] + [
        row.get("uploaded_at")
        for row in documents
        if row.get("uploaded_at")
    ]
    return max(values) if values else ""


def _first_file_name(files):
    for row in files:
        if row.get("original_name"):
            return row.get("original_name")
    return ""


def _detail_answer(student, detail, summary, records, documents):
    name = student_display_name(student)
    identity = f"{name}（{student['student_id']}）"
    latest_update = _latest_update(records, documents)
    if detail in ("coe_start", "coe_end"):
        label = "COE 开始日期" if detail == "coe_start" else "COE 结束日期"
        value_key = "start_date" if detail == "coe_start" else "end_date"
        value = summary["coe"].get(value_key)
        if value:
            school = summary["coe"].get("school") or "未记录学校"
            file_status = "已上传" if summary["coe"].get("file_uploaded") else "未找到上传文件"
            answer = f"{identity} 的 {label} 是 {value}。COE 学校：{school}。COE 文件：{file_status}。"
            if latest_update:
                answer += f" 最后更新时间：{latest_update}。"
            return answer
        file_name = _first_file_name(summary["coe"].get("files", []))
        if summary["coe"].get("file_uploaded"):
            uploaded_at = (summary["coe"].get("files") or [{}])[0].get("uploaded_at") or latest_update or "未记录"
            return (
                f"{identity} 当前系统没有结构化记录 {label}；已找到 COE 文件"
                f"{f'：{file_name}' if file_name else ''}，上传时间：{uploaded_at}。请人工打开文件确认日期。"
            )
        return f"没有找到该学生的 COE / OSHC 记录。建议检查 COE+OSHC 模块或文件中心是否已上传。"
    if detail in ("oshc_start", "oshc_end"):
        label = "OSHC 开始日期" if detail == "oshc_start" else "OSHC 结束日期"
        value_key = "start_date" if detail == "oshc_start" else "end_date"
        value = summary["oshc"].get(value_key)
        if value:
            provider = summary["oshc"].get("provider") or "未记录 provider"
            policy = summary["oshc"].get("policy_number") or "未记录保单号"
            file_status = "已上传" if summary["oshc"].get("file_uploaded") else "未找到上传文件"
            answer = f"{identity} 的 {label} 是 {value}。OSHC provider：{provider}，保单号：{policy}。OSHC 文件：{file_status}。"
            if latest_update:
                answer += f" 最后更新时间：{latest_update}。"
            return answer
        file_name = _first_file_name(summary["oshc"].get("files", []))
        if summary["oshc"].get("file_uploaded"):
            uploaded_at = (summary["oshc"].get("files") or [{}])[0].get("uploaded_at") or latest_update or "未记录"
            return (
                f"{identity} 当前系统没有结构化记录 {label}；已找到 OSHC 文件"
                f"{f'：{file_name}' if file_name else ''}，上传时间：{uploaded_at}。请人工打开文件确认日期。"
            )
        return f"没有找到该学生的 COE / OSHC 记录。建议检查 COE+OSHC 模块或文件中心是否已上传。"
    coe = summary["coe"]
    oshc = summary["oshc"]
    caaw = summary["caaw"]
    parts = [
        f"{identity} 当前 COE / OSHC 状态",
        f"COE 学校：{coe.get('school') or '未记录'}",
        f"COE 开始日期：{coe.get('start_date') or '未记录'}",
        f"COE 结束日期：{coe.get('end_date') or '未记录'}",
        f"COE 文件：{'已上传' if coe.get('file_uploaded') else '未找到'}",
        f"OSHC provider：{oshc.get('provider') or '未记录'}",
        f"OSHC 保单号：{oshc.get('policy_number') or '未记录'}",
        f"OSHC 开始日期：{oshc.get('start_date') or '未记录'}",
        f"OSHC 结束日期：{oshc.get('end_date') or '未记录'}",
        f"OSHC 文件：{'已上传' if oshc.get('file_uploaded') else '未找到'}",
        f"CAAW 文件：{'已上传' if caaw.get('file_uploaded') else '未找到'}",
    ]
    if latest_update:
        parts.append(f"最后更新时间：{latest_update}")
    return "；".join(parts) + "。"


def coe_oshc_status(db_handle, parsed):
    missing, student = require_student(db_handle, parsed)
    if missing:
        return missing
    records = rowsdict(db_handle.all(
        """SELECT id,document_kind,school_name,coe_provider,coe_course,coe_start_date,coe_end_date,
                  oshc_provider,oshc_company_name,oshc_start_date,oshc_end_date,
                  oshc_commencement_date,oshc_expiry_date,oshc_policy_number,file_path,created_at
           FROM coe_oshc
           WHERE student_id=?
           ORDER BY created_at DESC, id DESC""",
        (student["student_id"],),
    ))
    documents = rowsdict(db_handle.all(
        """SELECT id,document_type,original_name,file_path,uploaded_at,document_status
           FROM student_documents
           WHERE student_id=?
             AND is_active=1
             AND folder_category=?
           ORDER BY uploaded_at DESC, id DESC""",
        (student["student_id"], "COE + OSHC"),
    ))
    detail = parsed.get("filters", {}).get("detail") or "coe_oshc_summary"
    summary = _coe_oshc_summary(records, documents)
    if not records and not documents:
        answer = "没有找到该学生的 COE / OSHC 记录。建议检查 COE+OSHC 模块或文件中心是否已上传。"
        return {
            "ok": False,
            "intent": parsed["intent"],
            "answer": answer,
            "data": {
                "student": student,
                "detail": detail,
                "coe_oshc": records,
                "documents": documents,
                "summary": summary,
            },
            "readonly": True,
            "sources": READONLY_SOURCES["coe_oshc_status"],
        }
    kinds = ", ".join(sorted({row["document_kind"] for row in records if row.get("document_kind")})) or "暂无"
    answer = _detail_answer(student, detail, summary, records, documents)
    if detail in ("coe_summary", "oshc_summary"):
        answer = f"{answer} 当前结构化记录类型：{kinds}。"
    return {
        "ok": True,
        "intent": parsed["intent"],
        "answer": answer,
        "data": {
            "student": student,
            "detail": detail,
            "coe_oshc": records,
            "documents": documents,
            "summary": summary,
            "data_source": READONLY_SOURCES["coe_oshc_status"],
            "latest_updated_at": _latest_update(records, documents),
        },
        "readonly": True,
        "sources": READONLY_SOURCES["coe_oshc_status"],
    }


def contract_status(db_handle, parsed):
    missing, student = require_student(db_handle, parsed)
    if missing:
        return missing
    contracts = rowsdict(db_handle.all(
        """SELECT id,contract_uid,contract_title,contract_type,contract_status,
                  contract_date,generated_at,signed_at,fully_executed_at,updated_at
           FROM contracts
           WHERE student_id=?
             AND deleted_at IS NULL
             AND COALESCE(contract_status,'') NOT IN ('Deleted')
           ORDER BY updated_at DESC, id DESC""",
        (student["student_id"],),
    ))
    answer = f"{student_display_name(student)} 当前有 {len(contracts)} 条合同记录。"
    return {
        "ok": True,
        "intent": parsed["intent"],
        "answer": answer,
        "data": {"student": student, "contracts": contracts},
        "readonly": True,
        "sources": READONLY_SOURCES["contract_status"],
    }


def follow_up_students(db_handle, parsed):
    student = None
    params = ()
    student_filter = ""
    if parsed.get("filters"):
        missing, student = require_student(db_handle, parsed)
        if missing:
            return missing
        student_filter = " AND r.student_id=?"
        params = (student["student_id"],)
    rows = rowsdict(db_handle.all(
        f"""SELECT r.id,r.student_id,r.title,r.priority,r.due_date,r.status,
                   s.passport_first_name,s.passport_last_name,w.current_stage
            FROM reminders r
            LEFT JOIN students s ON s.student_id=r.student_id
            LEFT JOIN workflow_status w ON w.student_id=r.student_id
            WHERE COALESCE(r.status,'Pending') NOT IN ('Completed','Dismissed','Cancelled','Canceled')
            {student_filter}
            ORDER BY COALESCE(r.due_date,''), r.priority DESC, r.created_at DESC
            LIMIT 8""",
        params,
    ))
    for row in rows:
        if row.get("student_id"):
            canonical = canonical_student_workflow(db_handle, str(row["student_id"]))
            if canonical is not None:
                row["current_stage"] = canonical.stage_label
    if student:
        answer = f"{student_display_name(student)} 最近需要跟进的提醒共有 {len(rows)} 条（最多显示 8 条）。"
    else:
        answer = f"最近需要跟进的提醒共有 {len(rows)} 条（最多显示 8 条）。"
    return {
        "ok": True,
        "intent": parsed["intent"],
        "answer": answer,
        "data": {"items": rows},
        "readonly": True,
        "sources": READONLY_SOURCES["follow_up_students"],
    }


def missing_key_documents(db_handle, parsed):
    missing, student = require_student(db_handle, parsed)
    if missing:
        return missing
    rows = rowsdict(db_handle.all(
        """SELECT document_type,document_status,MAX(uploaded_at) latest_uploaded_at,COUNT(*) count
           FROM student_documents
           WHERE student_id=? AND is_active=1 AND folder_category LIKE ?
           GROUP BY document_type,document_status
           ORDER BY document_type""",
        (student["student_id"], "%Application%"),
    ))
    present = {str(row.get("document_type") or "").casefold(): row for row in rows}
    missing_types = [
        document_type
        for document_type in KEY_APPLICATION_DOCUMENTS
        if document_type.casefold() not in present
    ]
    if missing_types:
        answer = f"{student_display_name(student)} 仍缺少关键文件：{', '.join(missing_types)}。"
    else:
        answer = f"{student_display_name(student)} 的关键 Application 文件目前都有有效记录。"
    return {
        "ok": True,
        "intent": parsed["intent"],
        "answer": answer,
        "data": {
            "student": student,
            "required_document_types": list(KEY_APPLICATION_DOCUMENTS),
            "present_documents": rows,
            "missing_document_types": missing_types,
        },
        "readonly": True,
        "sources": READONLY_SOURCES["missing_key_documents"],
    }


def workflow_application_status(db_handle, parsed):
    missing, student = require_student(db_handle, parsed)
    if missing:
        return missing
    workflow = rowdict(db_handle.one(
        """SELECT current_stage,materials_checked,materials_checked_at,contract_signed_at,
                  visa_lodged_at,visa_finalized_at,completed_at
           FROM workflow_status
           WHERE student_id=?""",
        (student["student_id"],),
    )) or {}
    canonical = canonical_student_workflow(db_handle, student["student_id"])
    if canonical is not None:
        workflow["current_stage"] = canonical.stage_label
        workflow["stage_key"] = canonical.stage_key
        workflow["projection_state"] = canonical.state
    file_rows = rowsdict(db_handle.all(
        """SELECT folder_category,document_status,COUNT(*) count,MAX(uploaded_at) latest_uploaded_at
           FROM student_documents
           WHERE student_id=? AND is_active=1
           GROUP BY folder_category,document_status
           ORDER BY folder_category,document_status""",
        (student["student_id"],),
    ))
    total_files = sum(row["count"] for row in file_rows)
    offer_count = db_handle.one("SELECT COUNT(*) count FROM offers WHERE student_id=?", (student["student_id"],))["count"]
    coe_oshc_count = db_handle.one("SELECT COUNT(*) count FROM coe_oshc WHERE student_id=?", (student["student_id"],))["count"]
    answer = (
        f"{student_display_name(student)} 当前流程阶段为 {workflow.get('current_stage') or '未记录'}，"
        f"有效文件 {total_files} 个，Offer {offer_count} 条，COE/OSHC {coe_oshc_count} 条。"
    )
    return {
        "ok": True,
        "intent": parsed["intent"],
        "answer": answer,
        "data": {
            "student": student,
            "workflow": workflow,
            "application_files": {"total": total_files, "by_folder": file_rows},
            "offer_count": offer_count,
            "coe_oshc_count": coe_oshc_count,
        },
        "readonly": True,
        "sources": READONLY_SOURCES["workflow_application_status"],
    }


def data_update_health(db_handle, parsed):
    sources = rowsdict(db_handle.all(
        """SELECT source_name,source_type,state,update_frequency,auto_check_enabled,
                  requires_admin_confirmation,last_checked_at,last_successful_update_at,
                  latest_period,status
           FROM data_sources
           ORDER BY source_name,state"""
    ))
    jobs = rowsdict(db_handle.all(
        """SELECT job_type,state,status,source_period,records_found,records_failed,
                  needs_manual_confirmation_count,created_at,updated_at
           FROM data_update_jobs
           ORDER BY created_at DESC, id DESC
           LIMIT 8"""
    ))
    answer = f"Data Update Centre 当前登记 {len(sources)} 个数据源，最近更新任务显示 {len(jobs)} 条。"
    return {
        "ok": True,
        "intent": parsed["intent"],
        "answer": answer,
        "data": {"sources": sources, "recent_jobs": jobs},
        "readonly": True,
        "sources": READONLY_SOURCES["data_update_health"],
    }


HANDLERS = {
    "student_basic": student_basic,
    "application_files": application_files,
    "offer_status": offer_status,
    "coe_oshc_status": coe_oshc_status,
    "contract_status": contract_status,
    "follow_up_students": follow_up_students,
    "data_update_health": data_update_health,
    "missing_key_documents": missing_key_documents,
    "workflow_application_status": workflow_application_status,
}


def rejected_response(parsed):
    if parsed["intent"] == "rejected_write":
        answer = "这是只读小助手，不能删除、修改、创建、上传、确认、提交或生成任何数据。"
    else:
        answer = "输入中包含不安全的查询片段。请用自然语言描述要查看的信息，不要输入数据库语句。"
    return {
        "ok": False,
        "intent": parsed["intent"],
        "answer": answer,
        "data": {"filters": parsed.get("filters", {})},
        "readonly": True,
        "sources": [],
    }


def query_assistant(db_handle, question):
    parsed = parse_assistant_query(question)
    if parsed.get("rejected"):
        return rejected_response(parsed)
    handler = HANDLERS.get(parsed["intent"])
    if not handler:
        return {
            "ok": False,
            "intent": "unknown",
            "answer": "暂时只支持学生基础信息、文件状态、Offer、COE/OSHC、跟进提醒和 Data Update 健康状态的只读查询。",
            "data": {"filters": parsed.get("filters", {})},
            "readonly": True,
            "sources": [],
        }
    try:
        return handler(db_handle, parsed)
    except Exception:
        return {
            "ok": False,
            "intent": parsed["intent"],
            "answer": "只读查询暂时不可用，请稍后再试或换一个更具体的问题。",
            "data": {"filters": parsed.get("filters", {})},
            "readonly": True,
            "sources": READONLY_SOURCES.get(parsed["intent"], []),
        }
