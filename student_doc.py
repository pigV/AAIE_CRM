import io
import json
import re
from pathlib import Path

from docx import Document
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor
from PIL import Image, ImageDraw, ImageFont

from fields import FIELD_MAP, SECTIONS

BLUE = "1F5A75"
LIGHT_BLUE = "EAF2F6"
GRAY = "5E6B73"
STUDY_CONTRACT_COVER_ALT_PREFIX = "AAIE_PREMIUM_COVER_JSON:"


def _font(run, size=10.5, bold=False, color="1F2933"):
    run.font.name = "Arial"
    run._element.get_or_add_rPr().rFonts.set(qn("w:ascii"), "Arial")
    run._element.get_or_add_rPr().rFonts.set(qn("w:hAnsi"), "Arial")
    run._element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")
    run.font.size = Pt(size)
    run.bold = bold
    run.font.color.rgb = RGBColor.from_string(color)


def _shade(cell, fill):
    props = cell._tc.get_or_add_tcPr()
    shd = props.find(qn("w:shd"))
    if shd is None:
        shd = OxmlElement("w:shd")
        props.append(shd)
    shd.set(qn("w:fill"), fill)


def _cell_margins(cell, top=90, start=120, bottom=90, end=120):
    props = cell._tc.get_or_add_tcPr()
    margins = props.first_child_found_in("w:tcMar")
    if margins is None:
        margins = OxmlElement("w:tcMar")
        props.append(margins)
    for name, value in (("top", top), ("start", start), ("bottom", bottom), ("end", end)):
        node = margins.find(qn(f"w:{name}"))
        if node is None:
            node = OxmlElement(f"w:{name}")
            margins.append(node)
        node.set(qn("w:w"), str(value))
        node.set(qn("w:type"), "dxa")


def _set_width(cell, dxa):
    props = cell._tc.get_or_add_tcPr()
    width = props.find(qn("w:tcW"))
    if width is None:
        width = OxmlElement("w:tcW")
        props.append(width)
    width.set(qn("w:w"), str(dxa))
    width.set(qn("w:type"), "dxa")


def _value_text(value):
    if value is None or value == "":
        return "未填写"
    if isinstance(value, list):
        return "、".join(str(v) for v in value)
    return str(value)


def _has_meaningful_amount(value):
    text = str(value or "").strip()
    if not text:
        return False
    normalized = text.replace(",", "").replace("AUD", "").replace("澳币", "").replace("$", "").strip()
    try:
        return float(normalized) != 0
    except ValueError:
        return normalized not in {"0", "0.0", "0.00", "无", "没有", "none", "None"}


def _currency_amount(currency, amount):
    return " ".join(str(part or "").strip() for part in (currency, amount) if str(part or "").strip())


def _aud_amount(amount):
    text = str(amount or "").strip()
    if not text:
        return ""
    if "澳币" in text:
        return text
    return f"{text}澳币"


def _line_join(*values):
    return "\n".join(str(value).strip() for value in values if str(value or "").strip())


def contract_intake_term_display(value):
    text = str(value or "").strip()
    if not text:
        return ""
    if re.fullmatch(r"0?[1-9]|1[0-2]", text):
        return f"{int(text)}月"
    return text


def contract_intake_year_display(value):
    text = str(value or "").strip()
    if not text:
        return ""
    if re.fullmatch(r"\d{4}", text):
        return f"{text}年"
    return text


def contract_intake_year_term_display(intake_year, intake_term):
    year_text = str(intake_year or "").strip()
    year_display = contract_intake_year_display(year_text)
    term_text = contract_intake_term_display(intake_term)
    if year_display and term_text:
        if term_text.startswith(year_text) or term_text.startswith(year_display):
            return term_text
        return f"{year_display}{term_text}"
    if year_display:
        return year_display
    return term_text


def _contract_intake_term_display(value, intake_year=None):
    return contract_intake_year_term_display(intake_year, value)


def generate_student_info(student, destination):
    data = json.loads(student["form_data_json"])
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    doc = Document()
    section = doc.sections[0]
    section.page_width = Inches(8.5)
    section.page_height = Inches(11)
    section.top_margin = Inches(0.72)
    section.bottom_margin = Inches(0.72)
    section.left_margin = Inches(0.78)
    section.right_margin = Inches(0.78)
    section.header_distance = Inches(0.35)
    section.footer_distance = Inches(0.35)

    normal = doc.styles["Normal"]
    normal.font.name = "Arial"
    normal._element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")
    normal.font.size = Pt(10.5)
    normal.paragraph_format.space_after = Pt(5)
    normal.paragraph_format.line_spacing = 1.15

    header = section.header.paragraphs[0]
    header.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    run = header.add_run("AAIE CRM | Student Information")
    _font(run, 8.5, color=GRAY)

    title = doc.add_paragraph()
    title.paragraph_format.space_after = Pt(3)
    run = title.add_run("Student Information")
    _font(run, 23, True, BLUE)
    subtitle = doc.add_paragraph()
    subtitle.paragraph_format.space_after = Pt(14)
    run = subtitle.add_run(
        f"{student['passport_first_name']} {student['passport_last_name']}  |  Student ID {student['student_id']}"
    )
    _font(run, 11.5, True, GRAY)

    summary = doc.add_table(rows=2, cols=4)
    summary.alignment = WD_TABLE_ALIGNMENT.LEFT
    summary.autofit = False
    summary_values = [
        ("Student ID", student["student_id"]),
        ("Created Date", student["created_date"][:10]),
        ("DOB", student["date_of_birth"]),
        ("Status", student["current_status"]),
    ]
    for index, (label, value) in enumerate(summary_values):
        row = 0 if index < 2 else 1
        col = (index % 2) * 2
        for offset, width in ((0, 1350), (1, 3330)):
            _set_width(summary.cell(row, col + offset), width)
            _cell_margins(summary.cell(row, col + offset))
            summary.cell(row, col + offset).vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
        _shade(summary.cell(row, col), LIGHT_BLUE)
        p = summary.cell(row, col).paragraphs[0]
        _font(p.add_run(label), 9.5, True, BLUE)
        p = summary.cell(row, col + 1).paragraphs[0]
        _font(p.add_run(_value_text(value)), 10)

    for section_id, section_title, fields in SECTIONS:
        if section_id == "notes" and not data.get("general_notes"):
            continue
        heading = doc.add_paragraph()
        heading.paragraph_format.space_before = Pt(12)
        heading.paragraph_format.space_after = Pt(5)
        heading.paragraph_format.keep_with_next = True
        _font(heading.add_run(section_title), 13, True, BLUE)

        rows = []
        for name, label, _, _, _ in fields:
            if name == "account_password":
                continue
            value = data.get(name)
            if value not in (None, "", []):
                rows.append((label, _value_text(value)))
        if not rows:
            p = doc.add_paragraph("暂无信息")
            _font(p.runs[0], 10, color=GRAY)
            continue

        table = doc.add_table(rows=0, cols=2)
        table.alignment = WD_TABLE_ALIGNMENT.LEFT
        table.autofit = False
        for label, value in rows:
            cells = table.add_row().cells
            _set_width(cells[0], 2700)
            _set_width(cells[1], 6660)
            for cell in cells:
                _cell_margins(cell)
                cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            _shade(cells[0], "F3F6F8")
            _font(cells[0].paragraphs[0].add_run(label), 9.5, True, "334E5C")
            _font(cells[1].paragraphs[0].add_run(value), 10)

    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _font(footer.add_run("Confidential - AAIE internal use only"), 8, color=GRAY)
    doc.core_properties.title = f"Student Information - {student['student_id']}"
    doc.core_properties.subject = "AAIE CRM Student Information"
    doc.save(destination)
    return destination


STUDY_CONTRACT_TITLE = "澳洲留学咨询及签证服务委托合同"
STUDY_CONTRACT_TEMPLATE_VERSION = "StudyAbroadConsultingVisaContract_Template_V1"
DEFAULT_PARTY_A_NAME = "澳亚教育AOYA/AAIE"
DEFAULT_PARTY_A_TRADING_NAME = "AOYA/AAIE"
DEFAULT_PARTY_A_CONTACT = "Sonny Zhang"
DEFAULT_PARTY_A_PHONE = "+61 3 8597 3046"
DEFAULT_PARTY_A_EMAIL = "info@aaie.com.au"
DEFAULT_PARTY_A_ADDRESS = "Suite1113， 401 Docklands Drive, Docklands VIC 3008"
STUDY_CONTRACT_STANDARD_TEXT_DEFAULTS = {
    "intro": "甲乙双方本着自愿、平等、诚实信用的原则，经友好协商，就乙方委托甲方提供澳洲留学咨询及签证相关服务事宜，订立本合同，以便共同遵守。",
    "service_scope_intro": "甲方根据乙方委托，向乙方提供以下一项或多项澳洲留学咨询及签证相关服务：",
    "fee_explanation": "乙方确认并理解，本合同项下服务费为甲方向乙方提供澳洲留学咨询、申请材料整理与流程指导、签证咨询与材料指导、入学前沟通协调、住宿、接机、保险及其他相关事项咨询协助服务的费用。",
    "third_party_fees": "本合同项下服务费不包括学校、教育机构、教育部门、保险机构、住宿机构、移民局或其他第三方收取的费用。包括但不限于学校申请费、学费、医疗保险费、住宿费、监护或福利安排费、接机费、体检费、签证申请费、翻译费、公证费、银行手续费及其他第三方费用。上述费用由乙方按照相关收费主体的要求另行承担。",
    "third_party_decisions": "甲方可根据乙方委托，在服务范围内协助乙方进行信息整理、材料准备、流程沟通及相关事项协调，但第三方是否受理、批准、录取、签发或退费，均以相关第三方的政策、标准及最终决定为准。",
    "party_a_obligations": "甲方有权按照本合同约定向乙方收取留学咨询及签证服务费，以及双方书面确认由甲方代收代付的其他费用。\n甲方应在本合同有效期间，向乙方介绍澳洲教育制度、学校或课程申请流程、入学要求、签证制度、签证材料要求及申请准备注意事项。\n甲方可根据乙方授权，为乙方利益与学校、教育机构、保险机构、住宿机构、移民局或其他相关第三方进行沟通，包括发送或回复信函、电子邮件及其他申请相关信息。涉及乙方个人重大决定的事项，甲方应及时通知乙方并取得乙方确认。\n如乙方需要住宿、接机、监护或福利安排等延伸协助，甲方可在服务范围内协助乙方联系、沟通及整理相关信息，具体费用和结果以第三方要求及决定为准。\n甲方对乙方提交的个人资料和申请材料承担保密义务；除为履行本合同、获得乙方同意或法律法规要求外，不向无关第三方披露。",
    "party_b_obligations": "乙方应保证向甲方提交的所有文件、材料、信息和陈述合法、真实、完整、有效；如有虚假、隐瞒、遗漏或迟延提交，相关后果由乙方自行承担。\n乙方应按甲方合理要求及时提供申请、签证、住宿、保险、监护或其他相关材料，并配合完成表格确认、签字、付款、面试及补充材料等事项。\n在服务过程中，如学生、家长、担保人、监护人或家庭成员的联系方式、地址、工作、收入、健康、学习、签证或其他重要情况发生变化，乙方应及时通知甲方。\n乙方应保证本合同载明的电话、邮箱和地址准确有效，并及时查阅及回复甲方通知。因乙方联系方式无效、未及时回复或未配合导致申请延误、失败或产生额外费用的，由乙方自行承担。\n未经甲方书面同意，乙方不得泄露、转交或挪用甲方在服务过程中制作或提供的申请方案、材料清单、表格、说明、流程文件、数据统计信息或其他具有保密意义的资料。",
    "refund_intro": "乙方确认，甲方根据本合同已提供澳洲留学咨询、规划、材料整理、签证指导、表格信息整理、流程沟通协调及其他相关咨询协助服务后，已实际提供的对应服务部分不予退还。",
    "third_party_refund": "乙方向学校、教育机构、教育部门、保险机构、住宿机构、移民局或其他第三方支付的费用，包括但不限于学校申请费、学费、医疗保险费、住宿费、监护或福利安排费、接机费、体检费、签证申请费、翻译费、公证费、银行手续费及其他第三方费用，由乙方按照相关第三方的政策及收费、退费规则自行承担。甲方可根据乙方要求协助沟通相关退费事宜，但不对第三方是否退费、退费金额、退费时间或退费结果作出保证。",
    "termination_no_refund": "如乙方在签约后因自身原因中途终止服务、未及时提交材料、未按要求支付第三方费用、未配合完成申请表签署或面试、或存在其他单方原因导致服务无法继续履行的，所缴纳服务费不予以退还。",
    "false_documents": "如乙方提供虚假文件或隐瞒重要事实，包括但不限于学习、健康、出勤、纪律、签证历史、家庭资金或其他会影响申请判断的信息，导致申请延误、拒绝、撤销或合同无法履行的，乙方应自行承担后果；给甲方造成损失的，甲方有权依法追偿。",
    "result_disclaimer": "甲方提供的是咨询、材料指导、流程协调及相关协助服务。学校录取、教育机构或教育部门审批、保险安排、住宿安排、签证结果及其他第三方事项，均由相关学校、教育机构、教育部门、保险机构、住宿机构、移民局或其他第三方依据其政策和标准独立决定。甲方不保证乙方一定获得录取、签证或其他第三方批准结果。",
    "force_majeure": "如因自然灾害、疫情、战争、骚乱、政策变化、系统故障、第三方审理延误、第三方要求变化或其他不能预见、不能避免且不能克服的客观情况导致服务延误、中止或无法按原计划完成，双方应友好协商解决，甲方不对超出自身合理控制范围的结果承担保证责任。",
    "contract_variation": "本合同的任何补充、变更或修改应采用书面形式，经双方确认后与本合同具有同等法律效力。",
    "minor_guardian": "如乙方或学生在签署本合同时未满十八周岁或不具备完全民事行为能力，应由其法定代理人或监护人签署或确认后生效。",
    "contract_effective": "本合同自双方签字或盖章之日起生效。本合同可采用纸质签署、扫描件、电子签名或双方认可的其他书面确认方式签署，具有同等效力。",
    "personal_expenses": "申请澳洲留学及签证相关服务属乙方自愿；服务过程中发生但本合同未列明的个人生活费、误工费、误学费或其他个人费用，由乙方自行承担。",
    "signature_instruction": "请乙方/监护人自行签名并填写乙方日期；甲方签名、公章及甲方日期由 AAIE 在最终合同中加盖。",
}


def _setup_contract_document(doc):
    section = doc.sections[0]
    section.page_width = Inches(8.5)
    section.page_height = Inches(11)
    section.top_margin = Inches(0.75)
    section.bottom_margin = Inches(0.75)
    section.left_margin = Inches(0.85)
    section.right_margin = Inches(0.85)
    normal = doc.styles["Normal"]
    normal.font.name = "Arial"
    normal._element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")
    normal.font.size = Pt(10.5)
    normal.paragraph_format.line_spacing = 1.18
    normal.paragraph_format.space_after = Pt(5)
    for style_name in ("Heading 1", "Heading 2"):
        style = doc.styles[style_name]
        style.font.name = "Arial"
        style._element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")
        style.font.color.rgb = RGBColor.from_string(BLUE)


def _add_contract_heading(doc, text):
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(10)
    p.paragraph_format.space_after = Pt(5)
    p.paragraph_format.keep_with_next = True
    _font(p.add_run(text), 13, True, BLUE)
    return p


def _add_contract_paragraph(doc, text):
    p = doc.add_paragraph()
    _font(p.add_run(text or ""), 10.5)
    return p


def _add_or_insert_paragraph(doc, anchor=None):
    if anchor is not None:
        return anchor.insert_paragraph_before()
    return doc.add_paragraph()


def _paragraph_shading(paragraph, fill):
    props = paragraph._p.get_or_add_pPr()
    shd = props.find(qn("w:shd"))
    if shd is None:
        shd = OxmlElement("w:shd")
        props.append(shd)
    shd.set(qn("w:fill"), fill)


def _cover_paragraph(doc, text="", size=10.5, bold=False, color="1F2933", align=WD_ALIGN_PARAGRAPH.LEFT, anchor=None, shade=None):
    p = _add_or_insert_paragraph(doc, anchor)
    p.alignment = align
    p.paragraph_format.space_after = Pt(6)
    if shade:
        _paragraph_shading(p, shade)
    _font(p.add_run(text or ""), size, bold, color)
    return p


def _prevent_row_split(row):
    props = row._tr.get_or_add_trPr()
    if props.find(qn("w:cantSplit")) is None:
        props.append(OxmlElement("w:cantSplit"))


def _keep_table_together(table):
    for row_index, row in enumerate(table.rows):
        _prevent_row_split(row)
        for cell in row.cells:
            for paragraph in cell.paragraphs:
                paragraph.paragraph_format.keep_together = True
                paragraph.paragraph_format.keep_with_next = row_index < len(table.rows) - 1
    return table


def _add_key_value_table(doc, rows):
    table = doc.add_table(rows=0, cols=2)
    table.alignment = WD_TABLE_ALIGNMENT.LEFT
    table.autofit = False
    for label, value in rows:
        cells = table.add_row().cells
        _set_width(cells[0], 2500)
        _set_width(cells[1], 6860)
        for cell in cells:
            _cell_margins(cell, top=95, bottom=95)
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
        _shade(cells[0], "F3F6F8")
        _font(cells[0].paragraphs[0].add_run(label), 9.5, True, "334E5C")
        _font(cells[1].paragraphs[0].add_run(_value_text(value)), 10)
    return _keep_table_together(table)


def _contract_value(data, key, fallback=""):
    value = data.get(key)
    if value not in (None, ""):
        return value
    return fallback


def _cover_date_text(value):
    text = str(value or "").strip()
    if not text:
        return "未填写"
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", text)
    if match:
        return f"{match.group(3)}/{match.group(2)}/{match.group(1)}"
    return text


def study_contract_cover_info(data):
    title_lines = data.get("contract_cover_title_lines") or ("澳洲留学咨询及", "签证服务委托合同")
    if not isinstance(title_lines, (list, tuple)) or len(title_lines) < 2:
        title_lines = ("澳洲留学咨询及", "签证服务委托合同")
    return {
        "contract_uid": _value_text(data.get("contract_uid")),
        "contract_date": _cover_date_text(data.get("contract_date")),
        "service_type": _value_text(data.get("service_type")),
        "student_full_name": _value_text(data.get("student_full_name")),
        "contract_cover_title_lines": [_value_text(title_lines[0]), _value_text(title_lines[1])],
        "contract_cover_service_summary": _value_text(data.get("contract_cover_service_summary") or "留学咨询 / 学校申请 / 签证服务"),
        "contract_cover_title_en": _value_text(data.get("contract_cover_title_en") or "Formal education consulting and visa service agreement"),
        "slogan_cn": "专业藏在细节里，信任留给时间",
        "slogan_en": "AAIE | Expertise in Every Detail. Trust Built Over Time.",
        "summary_cn": "12年专注澳洲低龄留学，代理全澳公校与私校。",
        "summary_en": "12 years dedicated to Australian K-12 education, representing public and private schools across Australia.",
    }


def normalize_study_contract_cover_info(info):
    return study_contract_cover_info({
        "contract_uid": (info or {}).get("contract_uid"),
        "contract_date": (info or {}).get("contract_date"),
        "service_type": (info or {}).get("service_type"),
        "student_full_name": (info or {}).get("student_full_name"),
        "contract_cover_title_lines": (info or {}).get("contract_cover_title_lines"),
        "contract_cover_service_summary": (info or {}).get("contract_cover_service_summary"),
        "contract_cover_title_en": (info or {}).get("contract_cover_title_en"),
    })


def study_contract_cover_accessibility_lines(info):
    cover = normalize_study_contract_cover_info(info)
    return [
        "AAIE",
        cover["contract_cover_title_lines"][0],
        cover["contract_cover_title_lines"][1],
        cover["contract_cover_service_summary"],
        cover["contract_cover_title_en"],
        "prepared for the family",
        "合同编号",
        cover["contract_uid"],
        "签订日期",
        cover["contract_date"],
        "服务类型",
        cover["service_type"],
        "学生姓名",
        cover["student_full_name"],
        cover["slogan_cn"],
        cover["summary_cn"],
        cover["slogan_en"],
        cover["summary_en"],
    ]


def study_contract_cover_alt_text(info):
    payload = normalize_study_contract_cover_info(info)
    payload["visible_text"] = study_contract_cover_accessibility_lines(payload)
    return STUDY_CONTRACT_COVER_ALT_PREFIX + json.dumps(payload, ensure_ascii=False, sort_keys=True)


def parse_study_contract_cover_alt_text(text):
    raw = str(text or "")
    if STUDY_CONTRACT_COVER_ALT_PREFIX not in raw:
        return None
    payload = raw.split(STUDY_CONTRACT_COVER_ALT_PREFIX, 1)[1].strip()
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return None
    return normalize_study_contract_cover_info(data)


def _cover_font(size, bold=False):
    font_root = Path(__file__).resolve().parent / "assets" / "fonts" / "noto-sans-cjk-sc"
    candidates = [
        font_root / ("NotoSansSC-Bold.ttf" if bold else "NotoSansSC-Regular.ttf"),
        font_root / ("NotoSansCJKsc-Bold.otf" if bold else "NotoSansCJKsc-Regular.otf"),
        Path("/System/Library/Fonts/PingFang.ttc"),
        Path("/System/Library/Fonts/STHeiti Medium.ttc"),
        Path("/Library/Fonts/NotoSansSC-Regular.ttf"),
    ]
    for candidate in candidates:
        try:
            if candidate.is_file():
                return ImageFont.truetype(str(candidate), size=size)
        except Exception:
            continue
    return ImageFont.load_default()


def _cover_px(value, total, base):
    return int(value * total / base)


def _cover_text(draw, xy, text, font, fill, max_width=None):
    raw = str(text or "")
    if not max_width:
        draw.text(xy, raw, font=font, fill=fill)
        return
    text = raw
    while text and draw.textlength(text, font=font) > max_width:
        text = text[:-1]
    if text != raw and len(text) > 1:
        text = text[:-1] + "…"
    draw.text(xy, text or raw[:1], font=font, fill=fill)


def study_contract_cover_image(info, pixel_width=1654):
    cover = normalize_study_contract_cover_info(info)
    width = int(pixel_width)
    height = int(round(width * 297 / 210))
    image = Image.new("RGB", (width, height), "#008FB2")
    draw = ImageDraw.Draw(image, "RGBA")

    for y in range(height):
        ratio = y / max(1, height - 1)
        red = int(0 + ratio * 6)
        green = int(145 - ratio * 61)
        blue = int(178 + ratio * 24)
        draw.line((0, y, width, y), fill=(red, green, blue, 255))

    accent = [
        (width, height),
        (width, int(height * 0.43)),
        (int(width * 0.56), height),
    ]
    draw.polygon(accent, fill=(4, 185, 199, 36))

    mm_x = lambda value: _cover_px(value, width, 210)
    mm_y = lambda value: _cover_px(value, height, 297)
    left = mm_x(25)
    page_h = height

    font_brand = _cover_font(mm_y(30), True)
    font_title = _cover_font(mm_y(12), True)
    font_heading = _cover_font(mm_y(10), True)
    font_body = _cover_font(mm_y(4), False)
    font_body_bold = _cover_font(mm_y(4), True)
    font_card_label = _cover_font(mm_y(3.4), True)
    font_card_value = _cover_font(mm_y(3.8), True)
    font_footer_brand = _cover_font(mm_y(8.8), True)
    font_footer_title = _cover_font(mm_y(5.2), True)
    font_footer_body = _cover_font(mm_y(3.8), False)
    font_footer_body_bold = _cover_font(mm_y(3.8), True)

    draw.text((mm_x(28), page_h - mm_y(50)), "AAIE", font=font_brand, fill=(44, 99, 164, 255))

    for offset in range(0, mm_x(42), mm_x(7)):
        base_x = width - mm_x(82) + offset
        base_y = page_h - mm_y(104)
        draw.line((base_x, base_y, base_x + mm_x(38), base_y - mm_y(34)), fill=(255, 255, 255, 24), width=max(1, mm_x(0.4)))
    for offset in range(9):
        y = mm_y(43) + offset * mm_y(4.2)
        draw.line((width - mm_x(94), y, width - mm_x(15), y - mm_y(19)), fill=(255, 255, 255, 28), width=max(1, mm_x(0.2)))

    draw.text((left, page_h - mm_y(91)), cover["contract_cover_title_lines"][1], font=font_title, fill=(255, 255, 255, 255))
    draw.text((left, page_h - mm_y(109)), cover["contract_cover_title_lines"][0], font=font_title, fill=(255, 255, 255, 255))
    draw.rectangle((left, page_h - mm_y(121), left + mm_x(21), page_h - mm_y(119.7)), fill=(59, 196, 239, 255))
    draw.text((left, page_h - mm_y(138)), cover["contract_cover_service_summary"], font=font_heading, fill=(255, 255, 255, 255))
    draw.text((left, page_h - mm_y(151)), cover["contract_cover_title_en"], font=font_body, fill=(255, 255, 255, 255))
    draw.text((left, page_h - mm_y(158)), "prepared for the family", font=font_body, fill=(255, 255, 255, 255))

    cards = [
        ("合同编号", cover["contract_uid"]),
        ("签订日期", cover["contract_date"]),
        ("服务类型", cover["service_type"]),
        ("学生姓名", cover["student_full_name"]),
    ]
    card_w = mm_x(76)
    card_h = mm_y(31)
    gap_x = mm_x(8)
    gap_y = mm_y(8)
    start_y = page_h - mm_y(192)
    radius = mm_x(4)
    for index, (label, value) in enumerate(cards):
        col = index % 2
        row = index // 2
        x = left + col * (card_w + gap_x)
        y = start_y - row * (card_h + gap_y)
        draw.rounded_rectangle((x + mm_x(0.8), y + mm_y(0.8), x + card_w + mm_x(0.8), y + card_h + mm_y(0.8)), radius=radius, fill=(7, 59, 115, 15))
        draw.rounded_rectangle((x, y, x + card_w, y + card_h), radius=radius, fill=(255, 255, 255, 255))
        text_x = x + mm_x(14)
        _cover_text(draw, (text_x, y + mm_y(8)), label, font_card_label, (82, 96, 109, 255), card_w - mm_x(22))
        _cover_text(draw, (text_x, y + mm_y(17)), value, font_card_value, (7, 27, 61, 255), card_w - mm_x(22))

    draw.line((left, page_h - mm_y(64), width - mm_x(25), page_h - mm_y(64)), fill=(255, 255, 255, 89), width=max(1, mm_y(0.25)))
    draw.text((left, page_h - mm_y(49)), "AAIE", font=font_footer_brand, fill=(255, 255, 255, 255))
    draw.text((left + mm_x(34), page_h - mm_y(48)), cover["slogan_cn"], font=font_footer_title, fill=(255, 255, 255, 255))
    draw.text((left, page_h - mm_y(36)), cover["summary_cn"], font=font_footer_body, fill=(255, 255, 255, 255))
    draw.rectangle((left, page_h - mm_y(28), left + mm_x(13), page_h - mm_y(26.8)), fill=(59, 196, 239, 255))
    draw.text((left, page_h - mm_y(21)), cover["slogan_en"], font=font_footer_body_bold, fill=(255, 255, 255, 255))
    draw.text((left, page_h - mm_y(13)), "12 years dedicated to Australian K-12 education,", font=font_footer_body, fill=(255, 255, 255, 255))
    draw.text((left, page_h - mm_y(7)), "representing public and private schools across Australia.", font=font_footer_body, fill=(255, 255, 255, 255))
    return image


def study_contract_cover_png_bytes(info, pixel_width=1654):
    output = io.BytesIO()
    study_contract_cover_image(info, pixel_width=pixel_width).save(output, format="PNG", optimize=True)
    return output.getvalue()


def _company_name(data):
    return _contract_value(data, "company_legal_name", DEFAULT_PARTY_A_NAME)


def _company_authorised_person(data):
    return _contract_value(data, "company_authorised_person", DEFAULT_PARTY_A_CONTACT)


def _set_cell_border(cell, color="B7CFEA", size="8"):
    props = cell._tc.get_or_add_tcPr()
    borders = props.first_child_found_in("w:tcBorders")
    if borders is None:
        borders = OxmlElement("w:tcBorders")
        props.append(borders)
    for edge in ("top", "left", "bottom", "right"):
        node = borders.find(qn(f"w:{edge}"))
        if node is None:
            node = OxmlElement(f"w:{edge}")
            borders.append(node)
        node.set(qn("w:val"), "single")
        node.set(qn("w:sz"), size)
        node.set(qn("w:space"), "0")
        node.set(qn("w:color"), color)


def _cover_cell_run(cell, text, size=10.5, bold=False, color="FFFFFF", alignment=WD_ALIGN_PARAGRAPH.LEFT):
    paragraph = cell.paragraphs[0]
    paragraph.alignment = alignment
    paragraph.paragraph_format.space_after = Pt(4)
    run = paragraph.add_run(text)
    _font(run, size, bold, color)
    return run


def _cover_cell_paragraph(cell, text, size=10.5, bold=False, color="FFFFFF", alignment=WD_ALIGN_PARAGRAPH.LEFT, space_after=4):
    paragraph = cell.add_paragraph()
    paragraph.alignment = alignment
    paragraph.paragraph_format.space_after = Pt(space_after)
    _font(paragraph.add_run(text), size, bold, color)
    return paragraph


def _add_cover_spacer(doc, height=6, fill=None):
    paragraph = doc.add_paragraph()
    paragraph.paragraph_format.space_after = Pt(height)
    if fill:
        _paragraph_shading(paragraph, fill)
    return paragraph


def _add_premium_contract_cover_image_page(doc, data):
    cover = study_contract_cover_info(data)
    paragraph = doc.add_paragraph()
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    paragraph.paragraph_format.space_before = Pt(0)
    paragraph.paragraph_format.space_after = Pt(0)
    paragraph.paragraph_format.keep_together = True
    run = paragraph.add_run()
    image_stream = io.BytesIO(study_contract_cover_png_bytes(cover))
    picture = run.add_picture(image_stream, width=Inches(6.65))
    picture._inline.docPr.set("title", "AAIE Premium Study Contract Cover")
    picture._inline.docPr.set("descr", study_contract_cover_alt_text(cover))
    # Keep the break attached to the cover image.  A standalone break
    # paragraph can be pushed onto the next page by Word/LibreOffice when the
    # image fills the printable height, producing an unintended blank page.
    run.add_break(WD_BREAK.PAGE)


def _build_blue_contract_cover_page(doc, data):
    _add_premium_contract_cover_image_page(doc, data)


def add_study_contract_cover_page(doc, data):
    body = doc._body._element
    section_tag = qn("w:sectPr")
    existing_content_count = sum(1 for child in body if child.tag != section_tag)
    _build_blue_contract_cover_page(doc, data)
    content_children = [child for child in list(body) if child.tag != section_tag]
    cover_children = content_children[existing_content_count:]
    for child in cover_children:
        body.remove(child)
    for insert_index, child in enumerate(cover_children):
        body.insert(insert_index, child)
    return doc


def _remove_paragraph(paragraph):
    element = paragraph._element
    parent = element.getparent()
    if parent is not None:
        parent.remove(element)


def remove_study_contract_duplicate_title_intro(doc, data):
    title_text = str(data.get("contract_title") or STUDY_CONTRACT_TITLE).strip()
    candidates = []
    found_body_start = False
    for paragraph in list(doc.paragraphs[:8]):
        text = paragraph.text.strip()
        if not text:
            continue
        if text == title_text:
            candidates.append(paragraph)
            continue
        if text.startswith("合同编号") and ("合同日期" in text or _value_text(data.get("contract_uid")) in text):
            candidates.append(paragraph)
            continue
        if text.startswith("一、合同双方"):
            found_body_start = True
            break
        break
    if found_body_start and candidates:
        for paragraph in candidates:
            _remove_paragraph(paragraph)
    return doc


def _add_signature_table(doc, data):
    table = doc.add_table(rows=5, cols=2)
    table.alignment = WD_TABLE_ALIGNMENT.LEFT
    table.autofit = False
    left_lines = (
        "AAIE / 甲方（签字 / 盖章）",
        f"代理方（甲方盖章）：{_company_name(data)}",
        f"授权代表：{_company_authorised_person(data)}",
        "甲方签署日期：",
        "（由 AAIE 在最终合同中加盖签名、公章并填写日期）",
    )
    right_lines = (
        "委托方（乙方签字）",
        "委托方（乙方签字）：",
        "法定代理人或者监护人（名字）：",
        "家长/乙方签署日期：____年____月____日",
        "（乙方/监护人自行签署并填写日期）",
    )
    for col, lines in enumerate((left_lines, right_lines)):
        for row in range(5):
            cell = table.cell(row, col)
            _set_width(cell, 4680)
            _cell_margins(cell, top=140, bottom=140)
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
        _shade(table.cell(0, col), "F3F6F8")
        for row, line in enumerate(lines):
            _font(
                table.cell(row, col).paragraphs[0].add_run(line),
                10 if row == 0 else 9.5,
                row == 0,
                "334E5C" if row == 0 else "1F2933",
            )
    return _keep_table_together(table)


def render_refund_text(template, values):
    text = template or ""
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", str(value or ""))
    return text


def _standard_clause(data, key):
    clauses = data.get("standard_clause_texts") or {}
    return clauses.get(key) or STUDY_CONTRACT_STANDARD_TEXT_DEFAULTS.get(key, "")


def _add_standard_clause(doc, data, key):
    text = _standard_clause(data, key)
    for line in str(text or "").splitlines():
        if line.strip():
            _add_contract_paragraph(doc, line.strip())


def _docx_contains_any(path, needles):
    try:
        doc = Document(path)
    except Exception:
        return True
    text = "\n".join(p.text for p in doc.paragraphs)
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                text += "\n" + "\n".join(p.text for p in cell.paragraphs)
    return any(needle in text for needle in needles)


def ensure_study_contract_template(destination):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    old_markers = (
        "Template Version",
        "合同模板版本",
        "如乙方签证申请被拒签，且乙方明确表示不再继续申请，双方可按照本合同约定的退款规则处理",
        "即 AUD$ {{refusal_refund_amount}}",
        "甲方（签字 / 盖章）：____________________________\n日期：____年____月____日",
        "乙方（签字）：____________________________",
    )
    if destination.exists() and not _docx_contains_any(destination, old_markers):
        return destination
    doc = Document()
    _setup_contract_document(doc)
    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _font(title.add_run("{{contract_title}}"), 18, True, BLUE)
    for heading, body in (
        ("一、合同双方", "合同编号：{{contract_uid}}\n甲方法律主体名称：{{company_legal_name}}\nTrading Name / 品牌名：{{company_trading_name}}\n甲方 ABN / ACN：{{company_abn_acn}}\n甲方地址：{{company_address}}\n甲方电话：{{company_phone}}\n甲方邮箱：{{company_email}}\n甲方授权代表 / 顾问：{{company_authorised_person}}\n乙方签署人：{{parent_signer_name}}\n与学生关系：{{parent_signer_relationship}}\n乙方电话：{{parent_phone}}\n乙方邮箱：{{parent_email}}\n乙方地址：{{parent_address}}\n学生姓名：{{student_full_name}}\n学生出生日期：{{student_date_of_birth}}\n学生护照号 / 学生 ID：{{student_id_or_passport_number}}\n当前年级：{{student_current_year_level}}\n申请年级：{{study_level}}\n入学年份 / 学期：{{intake_term}}"),
        ("二、服务项目及服务范围", "{{service_scope_text}}\n{{other_service_scope_text}}"),
        ("三、服务费用与第三方费用", "留学咨询及签证服务费：{{consulting_and_visa_service_fee}}澳币；支付方式：{{service_fee_currency}}；最终费用：{{final_fee_amount}}澳币。\n如系统录入有效费用减免，生成正式合同时可显示费用减免项目；无减免时不显示该项目。"),
        ("四、甲方权利义务", "甲方按本合同约定提供澳洲留学咨询、材料指导、流程协调及签证相关咨询服务，并对乙方资料承担保密义务。"),
        ("五、乙方权利义务", "乙方应保证提交资料真实、完整、有效，并按要求及时配合材料、申请、面试、付款和信息变更通知。"),
        ("六、退费及违约责任", "{{refund_policy_text}}"),
        ("七、不可抗力及免责条款", "因不可抗力、政策变化、第三方决定或乙方原因导致延误、拒绝、停止或损失的，按本合同约定及适用法律处理。"),
        ("八、适用法律及争议解决", "{{governing_law}}\n{{dispute_resolution}}"),
        ("九、合同补充、变更和生效", "本合同的补充、变更、修改应采用书面形式，经双方确认后与本合同具有同等法律效力。"),
        ("十、特别条款", "{{special_terms}}"),
        ("十一、签署", "甲方（签字 / 盖章）：\n代理方（甲方盖章）：{{company_legal_name}}\n授权代表：{{company_authorised_person}}\n甲方签署日期：\n\n委托方（乙方签字）：\n法定代理人或者监护人（名字）：\n家长/乙方签署日期：____年____月____日"),
    ):
        _add_contract_heading(doc, heading)
        _add_contract_paragraph(doc, body)
    doc.core_properties.title = STUDY_CONTRACT_TITLE
    doc.core_properties.subject = "AAIE CRM Contract Template"
    doc.save(destination)
    return destination


def build_study_abroad_contract_document(data):
    """Build the retained AAIE contract master without touching the filesystem.

    Page geometry, headings, tables and the two-column signature region remain
    renderer-owned.  A Published master snapshot may replace only the bodies
    of sections three through ten through ``contract_sections``.
    """
    doc = Document()
    _setup_contract_document(doc)
    for style_name in ("Normal", "Heading 2", "Header", "Footer"):
        style = doc.styles[style_name]
        style.font.name = "Noto Sans SC"
        for attribute in ("ascii", "hAnsi", "eastAsia", "cs"):
            style.element.get_or_add_rPr().rFonts.set(qn(f"w:{attribute}"), "Noto Sans SC")
    add_study_contract_cover_page(doc, data)
    contract_sections = dict(data.get("contract_sections") or {})
    contract_section_titles = dict(data.get("contract_section_titles") or {})

    def add_content_heading(key, fallback):
        _add_contract_heading(doc, contract_section_titles.get(key) or fallback)

    def add_section_body(key, fallback):
        body = contract_sections.get(key)
        if body is None:
            fallback()
            return
        for paragraph in str(body or "").splitlines():
            if paragraph.strip():
                _add_contract_paragraph(doc, paragraph.strip())

    _add_contract_heading(doc, "一、合同双方")
    identity_rows = [
        ("合同编号", data.get("contract_uid")),
        ("甲方法律主体名称", _company_name(data)),
        ("Trading Name / 品牌名", _contract_value(data, "company_trading_name", DEFAULT_PARTY_A_TRADING_NAME)),
        ("甲方 ABN / ACN", data.get("company_abn_acn")),
        ("甲方地址", _contract_value(data, "company_address", DEFAULT_PARTY_A_ADDRESS)),
        ("甲方电话", _contract_value(data, "company_phone", DEFAULT_PARTY_A_PHONE)),
        ("甲方邮箱", _contract_value(data, "company_email", DEFAULT_PARTY_A_EMAIL)),
        ("甲方授权代表 / 顾问", _company_authorised_person(data)),
        ("乙方签署人", data.get("parent_signer_name")),
        ("与学生关系", data.get("parent_signer_relationship")),
        ("乙方电话", data.get("parent_phone")),
        ("乙方邮箱", data.get("parent_email")),
        ("乙方地址", data.get("parent_address")),
        ("学生姓名", data.get("student_full_name")),
        ("学生出生日期", data.get("student_date_of_birth")),
        ("学生护照号 / 学生 ID", data.get("student_id_or_passport_number")),
        ("当前年级", data.get("student_current_year_level")),
        ("申请年级", data.get("study_level")),
    ]
    if data.get("canonical_display_binding_schema_version"):
        identity_rows.append((
            "计划入读年份",
            contract_intake_year_display(
                data.get("planned_entry_year") or data.get("intake_year")
            ),
        ))
        if str(data.get("intake_term") or "").strip():
            identity_rows.append((
                "计划入读学期", contract_intake_term_display(data.get("intake_term"))
            ))
    else:
        identity_rows.append((
            "入学年份 / 学期",
            _contract_intake_term_display(
                data.get("intake_term"),
                data.get("intake_year") or data.get("preferred_intake_year"),
            ),
        ))
    _add_key_value_table(doc, identity_rows)
    _add_standard_clause(doc, data, "intro")

    _add_contract_heading(doc, "二、服务项目及服务范围")
    _add_standard_clause(doc, data, "service_scope_intro")
    if data.get("service_scope_override_text"):
        _add_contract_paragraph(doc, data.get("service_scope_override_text"))
    else:
        scopes = data.get("service_scope_items") or []
        if scopes:
            for index, item in enumerate(scopes, 1):
                _add_contract_paragraph(doc, f"{index}. {item}；" if index < len(scopes) else f"{index}. {item}。")
        else:
            _add_contract_paragraph(doc, "以双方书面确认的服务范围为准。")
    if not data.get("service_scope_override_text") and data.get("other_service_scope_text"):
        _add_contract_paragraph(doc, f"其他服务范围：{data.get('other_service_scope_text')}")

    add_content_heading("profile_3", "三、服务费用与第三方费用")
    add_section_body("profile_3", lambda: (
        _add_standard_clause(doc, data, "fee_explanation"),
        _add_standard_clause(doc, data, "third_party_fees"),
        _add_standard_clause(doc, data, "third_party_decisions"),
    ))
    fee_rows = list(data.get("pricing_rows") or [
        ("费用名称", data.get("service_fee_label") or "留学咨询及签证服务费"),
        ("服务类型", data.get("service_type")),
        ("支付方式", data.get("service_fee_currency")),
        ("服务费", _aud_amount(data.get("consulting_and_visa_service_fee"))),
        ("最终费用", _aud_amount(data.get("final_fee_amount"))),
    ])
    _add_key_value_table(doc, fee_rows)

    add_content_heading("profile_4", "四、甲方权利义务")
    add_section_body("profile_4", lambda: _add_standard_clause(doc, data, "party_a_obligations"))

    add_content_heading("profile_5", "五、乙方权利义务")
    add_section_body("profile_5", lambda: _add_standard_clause(doc, data, "party_b_obligations"))

    add_content_heading("profile_6", "六、退费及违约责任")
    def add_refund_body():
        _add_standard_clause(doc, data, "refund_intro")
        _add_standard_clause(doc, data, "third_party_refund")
        refund_text = render_refund_text(data.get("refund_policy_text"), data)
        if refund_text:
            _add_contract_paragraph(doc, refund_text)
        if data.get("refund_notes"):
            _add_contract_paragraph(doc, f"退款备注：{data.get('refund_notes')}")
        _add_standard_clause(doc, data, "termination_no_refund")
        _add_standard_clause(doc, data, "false_documents")
    add_section_body("profile_6", add_refund_body)

    add_content_heading("profile_7", "七、不可抗力、结果说明及免责")
    add_section_body("profile_7", lambda: (
        _add_standard_clause(doc, data, "result_disclaimer"),
        _add_standard_clause(doc, data, "force_majeure"),
    ))

    add_content_heading("legal_dispute", "八、适用法律及争议解决")
    add_section_body("legal_dispute", lambda: (
        _add_contract_paragraph(doc, data.get("governing_law") or "澳大利亚维多利亚州法律"),
        _add_contract_paragraph(doc, data.get("dispute_resolution") or "双方应先友好协商；协商不成的，按适用法律及相关管辖规则处理。"),
    ))

    add_content_heading("profile_9", "九、合同补充、变更和生效")
    add_section_body("profile_9", lambda: (
        _add_standard_clause(doc, data, "contract_variation"),
        _add_standard_clause(doc, data, "minor_guardian"),
        _add_standard_clause(doc, data, "contract_effective"),
        _add_standard_clause(doc, data, "personal_expenses"),
    ))

    add_content_heading("special_terms", "十、特别条款")
    _add_contract_paragraph(
        doc,
        data.get("special_terms") or contract_sections.get("special_terms") or "无。",
    )

    doc.add_page_break()
    _add_contract_heading(doc, "十一、签署")
    _add_key_value_table(doc, (
        ("甲方法律主体名称", _company_name(data)),
        ("甲方 ABN / ACN", data.get("company_abn_acn")),
        ("甲方授权代表", _company_authorised_person(data)),
        ("甲方电话", _contract_value(data, "company_phone", DEFAULT_PARTY_A_PHONE)),
        ("甲方联系邮箱", _contract_value(data, "company_email", DEFAULT_PARTY_A_EMAIL)),
        ("甲方地址", _contract_value(data, "company_address", DEFAULT_PARTY_A_ADDRESS)),
        ("乙方签署人", data.get("parent_signer_name")),
        ("与学生关系", data.get("parent_signer_relationship")),
        ("乙方电话", data.get("parent_phone")),
        ("乙方邮箱", data.get("parent_email")),
        ("乙方地址", data.get("parent_address")),
    ))
    _add_standard_clause(doc, data, "signature_instruction")
    _add_signature_table(doc, data)

    doc.core_properties.title = data.get("contract_title") or STUDY_CONTRACT_TITLE
    doc.core_properties.subject = "AAIE CRM Study Abroad Contract"
    return doc


def generate_study_abroad_contract(data, destination):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    doc = build_study_abroad_contract_document(data)
    doc.save(destination)
    return destination
