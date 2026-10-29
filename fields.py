"""Parent form field definitions shared by rendering and persistence."""

import re
from datetime import date, datetime

YEAR_LEVELS = [f"Year {n}" for n in range(1, 13)] + ["其他"]
APPLYING_YEAR_LEVELS = [f"Year {n}" for n in range(1, 13)] + ["预科+本科", "本科", "研究生", "不确定"]
APPLICATION_STREAMS = ["school_education|中小学申请", "higher_education|高等教育申请"]
HIGHER_EDUCATION_COURSE_TYPES = [
    "Foundation|预科", "Diploma", "Bachelor|本科", "Postgraduate|研究生", "Package|打包课程",
]
HIGHER_EDUCATION_PACKAGE_COMBINATIONS = [
    "Foundation + Bachelor",
    "Diploma + Bachelor",
    "Foundation + Diploma + Bachelor",
]

SECTIONS = [
    (
        "basic",
        "学生基本信息",
        [
            ("passport_last_name", "护照姓（拼音）", "text", True, None),
            ("passport_first_name", "护照名（拼音）", "text", True, None),
            ("english_nickname", "英文昵称", "text", False, None),
            ("gender", "性别", "radio", True, ["男", "女"]),
            ("date_of_birth", "出生日期", "date", True, None),
            ("current_year_level", "在读年级", "select", True, YEAR_LEVELS),
            ("application_stream", "申请类型", "select", True, APPLICATION_STREAMS),
            ("preferred_intake_year", "计划入读年份", "select", True, ["2026", "2027", "2028", "2029", "不确定"]),
            ("preferred_intake_term", "计划入澳学期", "select", True, ["02|2月入学", "04|4月入学", "07|7月入学", "10|10月入学", "unsure|不确定"]),
            ("applying_year_level", "申请年级", "select", False, APPLYING_YEAR_LEVELS),
            ("higher_education_course_type", "高等教育课程类型", "select", False, HIGHER_EDUCATION_COURSE_TYPES),
            ("higher_education_package_combination", "Package 组合", "select", False, HIGHER_EDUCATION_PACKAGE_COMBINATIONS),
            ("preferred_cities", "申请意向城市", "checkbox", True, [
                "melbourne|墨尔本", "sydney|悉尼", "brisbane|布里斯班", "adelaide|阿德莱德",
                "perth|珀斯", "canberra|堪培拉", "tasmania|塔斯马尼亚", "unsure|不确定",
            ]),
            ("visa_status", "身份状态", "select", True, ["中国护照", "澳洲PR", "澳洲公民", "其他", "不确定"]),
            ("previous_name", "曾用名", "text", False, None),
        ],
    ),
    (
        "contact",
        "联系方式与地址",
        [
            ("home_address", "家庭住址", "text", True, None),
            ("home_postcode", "家庭住址邮编", "text", True, None),
            ("australia_address", "澳洲住址", "text", False, None),
            ("australia_postcode", "澳洲住址邮编", "text", False, None),
            ("mobile_phone", "学生手机号", "tel", True, None),
            ("student_email", "学生Email", "email", False, None),
        ],
    ),
    (
        "siblings",
        "兄弟姐妹信息",
        [
            ("has_sibling_under_6", "家庭中是否有6岁以下直系兄弟姐妹", "radio", True, ["是", "否"]),
            ("sibling_1_name", "兄弟姐妹1姓名", "text", False, None),
            ("sibling_1_gender", "兄弟姐妹1性别", "radio", False, ["男", "女"]),
            ("sibling_1_dob", "兄弟姐妹1出生日期", "date", False, None),
            ("sibling_1_school_company", "兄弟姐妹1学校/公司", "text", False, None),
            ("sibling_1_year_level", "兄弟姐妹1年级", "select", False, YEAR_LEVELS),
            ("sibling_2_name", "兄弟姐妹2姓名", "text", False, None),
            ("sibling_2_gender", "兄弟姐妹2性别", "radio", False, ["男", "女"]),
            ("sibling_2_dob", "兄弟姐妹2出生日期", "date", False, None),
            ("sibling_2_school_company", "兄弟姐妹2学校/公司", "text", False, None),
            ("sibling_2_year_level", "兄弟姐妹2年级", "select", False, YEAR_LEVELS),
        ],
    ),
    (
        "accommodation",
        "陪读与住宿偏好",
        [
            ("requires_parent_accompaniment", "是否需要陪读", "radio", True, ["是", "否", "不确定"]),
            ("accompanying_person_relationship", "与学生关系", "select", False, ["父亲", "母亲", "祖父", "祖母", "外祖父", "外祖母", "其他", "不确定"]),
            ("accommodation_preference", "申请住宿方式", "checkbox", True, ["住校", "寄宿家庭", "不确定"]),
            ("accept_homestay_with_young_children", "接受住家有8岁以下小孩吗", "radio", False, ["接受", "不接受", "不确定"]),
            ("homestay_style_preference", "偏向于中式或西式住家", "radio", False, ["中式", "西式", "都可以", "不确定"]),
            ("smoke_allergy", "对烟过敏吗", "radio", True, ["是", "否", "不确定"]),
            ("food_allergy", "对某些食物过敏吗", "radio", True, ["是", "否", "不确定"]),
            ("food_allergy_notes", "食物过敏补充说明", "text", False, None),
            ("pet_allergy", "对宠物过敏吗", "radio", True, ["是", "否", "不确定"]),
            ("pet_allergy_notes", "宠物过敏补充说明", "text", False, None),
            ("medicine_allergy", "对某些药物过敏吗", "radio", True, ["是", "否", "不确定"]),
            ("medicine_allergy_notes", "药物过敏补充说明", "text", False, None),
            ("accommodation_notes", "其他需要补充的", "textarea", False, None),
        ],
    ),
    (
        "health",
        "健康、安全与学校声明",
        [
            ("photo_permission", "是否同意学校为学生照相作宣传用途", "radio", True, ["同意", "不同意", "需要进一步确认"]),
            ("can_swim", "学生是否会游泳", "radio", True, ["会", "不会", "一般"]),
            ("has_medical_or_psychological_history", "有无残疾或心理/身体疾病史", "radio", True, ["有", "无", "不确定"]),
            ("medical_history_notes", "疾病史补充说明", "textarea", False, None),
            ("requires_long_term_medication", "有无需要长期/持续性使用药物治疗", "radio", True, ["有", "无", "不确定"]),
            ("medication_notes", "药物治疗补充说明", "textarea", False, None),
            ("has_violent_behavior_history", "过去有无暴力行为史", "radio", True, ["有", "无", "不确定"]),
            ("has_suspension_or_expulsion_history", "曾经有无被学校停学或开除", "radio", True, ["有", "无", "不确定"]),
        ],
    ),
    (
        "visa",
        "签证风险信息",
        [
            ("student_australia_visa_refusal", "学生有无澳洲拒签史", "radio", True, ["有", "无", "不确定"]),
            ("student_visa_refusal_notes", "拒签史补充说明", "textarea", False, None),
            ("parents_australia_visa_refusal", "父母有无澳洲拒签史", "radio", True, ["有", "无", "不确定"]),
            ("parents_australia_visa_refusal_person", "父母澳洲拒签史对象", "radio", False, ["父亲", "母亲", "父母都有"]),
            ("father_migration_application_status", "父亲是否已经或者正在申请移民", "radio", True, ["是", "否", "不确定"]),
            ("father_migration_notes", "父亲移民补充信息", "textarea", False, None),
            ("mother_migration_application_status", "母亲是否已经或者正在申请移民", "radio", True, ["是", "否", "不确定"]),
            ("mother_migration_notes", "母亲移民补充信息", "textarea", False, None),
        ],
    ),
    (
        "study",
        "学习经历与语言测试",
        [
            ("current_school_name", "现在读学校名称", "text", True, None),
            ("current_school_year_level", "现在就读年级", "select", True, YEAR_LEVELS),
            ("study_history_notes", "学习经历备注", "textarea", False, None),
            ("has_aeas_or_idat", "是否考过 AEAS 或 IDAT", "radio", True, ["是", "否", "准备考", "不确定"]),
            ("aeas_idat_score", "AEAS/IDAT 分数", "text", False, None),
            ("has_ielts_or_toefl", "是否考过雅思或托福", "radio", True, ["是", "否", "准备考", "不确定"]),
            ("language_test_notes", "任何语言测试", "textarea", False, None),
            ("account_username", "账号用户名", "text", False, None),
            ("account_password", "账号密码", "text", False, None),
        ],
    ),
    (
        "father",
        "父亲信息",
        [
            ("father_passport_last_name", "父亲护照姓", "text", True, None),
            ("father_passport_first_name", "父亲护照名", "text", True, None),
            ("father_date_of_birth", "父亲出生日期", "date", True, None),
            ("father_mobile", "父亲手机号", "tel", True, None),
            ("father_email", "父亲常用 Email", "email", True, None),
            ("father_employer", "父亲工作单位", "text", False, None),
            ("father_work_address", "父亲单位地址", "text", False, None),
            ("father_work_postcode", "父亲单位邮编", "text", False, None),
            ("father_work_phone", "父亲单位电话", "tel", False, None),
            ("father_position", "父亲工作职务", "text", False, None),
            ("father_marital_status", "父亲婚姻状况", "select", False, ["已婚", "离异", "单身", "丧偶", "其他"]),
            ("father_religion", "父亲宗教信仰", "select", False, ["无", "佛教", "基督教", "天主教", "伊斯兰教", "其他", "不方便透露"]),
            ("father_highest_school", "父亲最高学历毕业院校", "text", False, None),
            ("father_highest_qualification", "父亲所获学历", "select", False, ["高中", "大专", "本科", "硕士", "博士", "其他"]),
        ],
    ),
    (
        "mother",
        "母亲信息",
        [
            ("mother_passport_last_name", "母亲护照姓", "text", True, None),
            ("mother_passport_first_name", "母亲护照名", "text", True, None),
            ("mother_date_of_birth", "母亲出生日期", "date", True, None),
            ("mother_mobile", "母亲手机号", "tel", True, None),
            ("mother_email", "母亲常用 Email", "email", True, None),
            ("mother_employer", "母亲工作单位", "text", False, None),
            ("mother_work_address", "母亲单位地址", "text", False, None),
            ("mother_work_postcode", "母亲单位邮编", "text", False, None),
            ("mother_work_phone", "母亲单位电话", "tel", False, None),
            ("mother_position", "母亲工作职务", "text", False, None),
            ("mother_marital_status", "母亲婚姻状况", "select", False, ["已婚", "离异", "单身", "丧偶", "其他"]),
            ("mother_religion", "母亲宗教信仰", "select", False, ["无", "佛教", "基督教", "天主教", "伊斯兰教", "其他", "不方便透露"]),
            ("mother_highest_school", "母亲最高学历毕业院校", "text", False, None),
            ("mother_highest_qualification", "母亲所获学历", "select", False, ["高中", "大专", "本科", "硕士", "博士", "其他"]),
        ],
    ),
    (
        "emergency",
        "紧急联系人",
        [
            ("emergency_contact_name", "紧急联系人姓名", "text", False, None),
            ("emergency_contact_gender", "紧急联系人性别", "radio", False, ["男", "女"]),
            ("emergency_contact_relationship", "紧急联系人与学生的关系", "select", False, ["父亲", "母亲", "亲属", "朋友", "其他"]),
            ("emergency_contact_mobile", "紧急联系人手机号", "tel", False, None),
            ("emergency_contact_email", "紧急联系人 Email", "email", False, None),
            ("emergency_contact_address", "紧急联系人住址及邮编", "textarea", False, None),
        ],
    ),
    (
        "notes",
        "备注",
        [("general_notes", "备注", "textarea", False, None)],
    ),
]

FIELD_MAP = {
    field[0]: {"label": field[1], "type": field[2], "required": field[3], "options": field[4], "section": section_id}
    for section_id, _, fields in SECTIONS
    for field in fields
}

REQUIRED_FIELDS = {name for name, meta in FIELD_MAP.items() if meta["required"]}
DATE_FIELDS = {name for name, meta in FIELD_MAP.items() if meta["type"] == "date"}
DATE_VALUE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y")


def parseAustralianDate(rawText, context=None):
    raw = str(rawText or "").strip()
    result = {
        "raw_text": raw,
        "normalized_date": "",
        "display_date_au": "",
        "date_format_detected": "",
        "ambiguous_date": False,
    }
    if not raw:
        return result
    text = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", raw, flags=re.I)
    text = text.replace(",", " ")
    text = re.sub(r"\s*([/.-])\s*", r"\1", text)
    text = re.sub(r"/{2,}", "/", text)
    text = re.sub(r"\s+", " ", text).strip(" .;:")

    numeric = re.fullmatch(r"(\d{1,4})([/.-])(\d{1,2})\2(\d{2,4})", text)
    if numeric:
        first, separator, second, year = numeric.groups()
        if len(first) == 4:
            formats = [(f"%Y{separator}%m{separator}%d", f"YYYY{separator}MM{separator}DD", False)]
        else:
            day = int(first)
            month = int(second)
            year_label = "YYYY" if len(year) == 4 else "YY"
            formats = [(f"%d{separator}%m{separator}%{'Y' if len(year) == 4 else 'y'}", f"DD{separator}MM{separator}{year_label}", day <= 12 and month <= 12)]
        for fmt, detected, ambiguous in formats:
            try:
                parsed = datetime.strptime(text, fmt).date()
            except ValueError:
                continue
            result.update({
                "normalized_date": parsed.isoformat(),
                "display_date_au": parsed.strftime("%d/%m/%Y"),
                "date_format_detected": detected.replace("-", "/").replace(".", "/") if detected.startswith("DD") else detected,
                "ambiguous_date": bool(ambiguous),
            })
            return result

    formats = [
        ("%d %B %Y", "D Month YYYY"),
        ("%d %b %Y", "D Mon YYYY"),
        ("%d-%B-%Y", "D-Month-YYYY"),
        ("%d-%b-%Y", "D-Mon-YYYY"),
        ("%B %d %Y", "Month D YYYY"),
        ("%b %d %Y", "Mon D YYYY"),
    ]
    for fmt, detected in formats:
        try:
            parsed = datetime.strptime(text, fmt).date()
        except ValueError:
            continue
        result.update({
            "normalized_date": parsed.isoformat(),
            "display_date_au": parsed.strftime("%d/%m/%Y"),
            "date_format_detected": detected,
            "ambiguous_date": False,
        })
        return result
    return result


def formatDateAU(value):
    parsed = parseAustralianDate(value)
    return parsed["display_date_au"] or str(value or "").strip()


def normalize_date_value(value):
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if not text:
        return ""
    parsed = parseAustralianDate(text)
    if parsed["normalized_date"]:
        return parsed["normalized_date"]
    return text


def is_valid_date_value(value):
    if value is None:
        return True
    if isinstance(value, (datetime, date)):
        return True
    text = str(value).strip()
    if not text:
        return True
    return bool(parseAustralianDate(text)["normalized_date"])


def display_date_value(value):
    return formatDateAU(value)
