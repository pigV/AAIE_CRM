import re


WRITE_INTENT_KEYWORDS = (
    "delete", "remove", "update", "modify", "edit", "create", "insert", "upload",
    "archive", "reset", "send", "submit", "confirm", "approve", "void", "restore",
    "generate", "mark", "execute sql", "run sql", "sql语句", "删除", "移除", "修改", "更新", "编辑", "创建", "新增", "上传",
    "归档", "重置", "发送", "提交", "确认", "批准", "作废", "恢复", "生成", "标记",
    "执行sql", "执行 sql",
)

UNSAFE_INPUT_PATTERNS = (
    r";",
    r"--",
    r"/\*",
    r"\*/",
    r"\b(drop|truncate|alter|union|pragma|attach|detach)\b",
)

INTENT_KEYWORDS = {
    "offer", "offers", "录取",
    "coe", "oshc", "caaw", "保险", "医保",
    "start", "date", "end", "commencement", "expiry", "course", "courses", "policy",
    "contract", "contracts", "合同", "签约", "签名", "签署",
    "document", "documents", "file", "files", "application", "applications",
    "材料", "文件", "文件中心", "上传", "缺少", "缺文件", "关键文件",
    "status", "summary", "profile", "workflow", "health",
    "状态", "查询", "查一下", "有没有", "怎么样", "摘要", "基本信息", "学生资料", "总结",
    "student", "students", "about", "for",
}

NAME_STOPWORDS = {
    "the", "a", "an", "of", "and", "or", "to", "in", "on",
    "student", "students", "about", "for", "query", "check", "show", "find",
}


def normalize_query(text):
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _strip_intent_words_from_name(value):
    tokens = re.findall(r"[A-Za-z][A-Za-z'-]*", str(value or ""))
    name_tokens = [
        token
        for token in tokens
        if token.casefold() not in INTENT_KEYWORDS
        and token.casefold() not in NAME_STOPWORDS
    ]
    return " ".join(name_tokens).strip()


def is_write_intent(text):
    lowered = normalize_query(text).casefold()
    if any(token in lowered for token in ("data update", "data-update", "数据更新")) and any(
        token in lowered for token in ("health", "健康", "status", "状态", "summary", "摘要", "查询")
    ):
        write_terms = tuple(
            keyword for keyword in WRITE_INTENT_KEYWORDS
            if keyword not in ("update", "更新")
        )
        return any(keyword in lowered for keyword in write_terms)
    return any(keyword in lowered for keyword in WRITE_INTENT_KEYWORDS)


def is_unsafe_input(text):
    lowered = normalize_query(text).casefold()
    return any(re.search(pattern, lowered) for pattern in UNSAFE_INPUT_PATTERNS)


def extract_student_filter(text):
    normalized = normalize_query(text)
    student_id_match = re.search(r"\b(?:20\d{6}|[A-Z]{1,6}\d{3,})\b", normalized, re.I)
    if student_id_match:
        return {"student_id": student_id_match.group(0).upper()}
    name_match = re.search(r"(?:学生|student|about|for)\s+([A-Za-z][A-Za-z\s'-]{1,80})", normalized, re.I)
    if name_match:
        explicit_name = _strip_intent_words_from_name(name_match.group(1))
        if explicit_name:
            return {"student_name": explicit_name}
    inferred_name = _strip_intent_words_from_name(normalized.replace("的", " "))
    if inferred_name:
        return {"student_name": inferred_name}
    return {}


def classify_intent(text):
    lowered = normalize_query(text).casefold()
    if any(token in lowered for token in ("缺少", "缺文件", "missing document", "missing file", "关键文件")):
        return "missing_key_documents"
    if any(token in lowered for token in ("workflow", "application status", "状态摘要", "流程状态", "申请状态")):
        return "workflow_application_status"
    if any(token in lowered for token in ("data update", "data-update", "数据更新", "health", "健康")):
        return "data_update_health"
    if any(token in lowered for token in ("跟进", "follow up", "follow-up", "reminder", "提醒")):
        return "follow_up_students"
    if any(token in lowered for token in ("application", "applications", "申请", "材料", "文件中心")):
        return "application_files"
    if "offer" in lowered or "录取" in lowered:
        return "offer_status"
    if any(token in lowered for token in ("coe", "oshc", "caaw", "保险", "医保", "课程开始", "课程结束")):
        return "coe_oshc_status"
    if any(token in lowered for token in ("contract", "合同", "签约", "签名", "签署")):
        return "contract_status"
    if any(token in lowered for token in ("student", "学生", "基本信息", "profile")):
        return "student_basic"
    return "unknown"


def extract_coe_oshc_detail(text):
    lowered = normalize_query(text).casefold()
    mentions_oshc = any(token in lowered for token in ("oshc", "医保", "保险"))
    mentions_coe = any(token in lowered for token in ("coe", "course", "课程", "commencement"))
    mentions_start = any(
        token in lowered
        for token in (
            "start date", "commencement date", "开始时间", "开始日期",
            "什么时候开始", "课程开始", "课程开始日期",
        )
    )
    mentions_end = any(
        token in lowered
        for token in (
            "end date", "expiry date", "结束时间", "结束日期",
            "到什么时候", "到期", "课程结束", "课程结束日期",
        )
    )
    mentions_summary = any(
        token in lowered
        for token in ("状态", "status", "准备好", "ready", "coe/oshc", "coe 和 oshc", "coe oshc")
    )
    if mentions_oshc and mentions_start:
        return "oshc_start"
    if mentions_oshc and mentions_end:
        return "oshc_end"
    if mentions_coe and mentions_start:
        return "coe_start"
    if mentions_coe and mentions_end:
        return "coe_end"
    if mentions_summary or (mentions_coe and mentions_oshc):
        return "coe_oshc_summary"
    if mentions_oshc:
        return "oshc_summary"
    if mentions_coe:
        return "coe_summary"
    return "coe_oshc_summary"


def parse_assistant_query(text):
    query = normalize_query(text)
    if not query:
        return {"intent": "unknown", "filters": {}, "query": query, "rejected": False}
    if is_write_intent(query):
        return {"intent": "rejected_write", "filters": {}, "query": query, "rejected": True}
    if is_unsafe_input(query):
        return {"intent": "rejected_unsafe_input", "filters": {}, "query": query, "rejected": True}
    intent = classify_intent(query)
    filters = extract_student_filter(query)
    if intent == "coe_oshc_status":
        filters["detail"] = extract_coe_oshc_detail(query)
    return {
        "intent": intent,
        "filters": filters,
        "query": query,
        "rejected": False,
    }
