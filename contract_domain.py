"""Pure contract configuration, composition, signer, and snapshot rules.

This module deliberately has no database, HTTP, filesystem, Workflow, or
signing dependencies.  ``app.py`` is the compatibility adapter around it.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterable, Mapping


NEW_INTERNATIONAL_STUDENT_CONTRACT = "NewInternationalStudentContract"
TRANSFER_APPLICATION_CONTRACT = "TransferApplicationContract"
LOCAL_STUDENT_APPLICATION_CONTRACT = "LocalStudentApplicationContract"
DEFAULT_CONTRACT_TYPE = NEW_INTERNATIONAL_STUDENT_CONTRACT
SNAPSHOT_VERSION = 1


@dataclass(frozen=True)
class ServiceDefinition:
    service_id: str
    label: str


@dataclass(frozen=True)
class ContractTypeConfig:
    type_id: str
    label: str
    title: str
    allowed_services: tuple[str, ...]
    default_services: tuple[str, ...]
    clause_profile: str
    cover_title_lines: tuple[str, str]
    cover_service_summary: str
    cover_title_en: str
    service_fee_label: str
    intro: str
    service_scope_intro: str


_SERVICES = (
    ServiceDefinition("study_planning_consulting", "澳洲留学规划咨询"),
    ServiceDefinition("school_transfer_planning_and_application", "转学规划与转学申请协调"),
    ServiceDefinition("school_information_and_selection_advice", "学校信息介绍与选校建议"),
    ServiceDefinition("document_checklist_guidance_and_document_preparation", "申请材料清单指导与材料整理"),
    ServiceDefinition("application_process_guidance_and_coordination", "申请流程说明与沟通协调"),
    ServiceDefinition("offer_coe_caaw_oshc_process_assistance", "录取文件、COE、CAAW、OSHC 相关流程协助"),
    ServiceDefinition("offer_coe_oshc_process_assistance", "录取文件、COE、OSHC 相关流程协助"),
    ServiceDefinition("offer_and_enrolment_coordination", "录取与入学确认沟通协调"),
    ServiceDefinition("visa_document_checklist_guidance", "签证申请材料清单指导"),
    ServiceDefinition("visa_application_form_information_preparation_assistance", "签证申请表格信息整理协助"),
    ServiceDefinition("student_visa_lodgement", "学生签证递交"),
    ServiceDefinition("subclass_590_guardian_visa_lodgement", "590 陪读签证递交"),
    ServiceDefinition("gs_study_plan_consulting", "GS / 学习计划相关咨询"),
    ServiceDefinition("subclass_590_guardian_visa_consulting", "590 陪读签证相关咨询"),
    ServiceDefinition("accommodation_airport_pickup_insurance_prearrival_consulting", "住宿、接机、保险、入学前准备事项咨询"),
)
SERVICE_CATALOG = MappingProxyType({item.service_id: item for item in _SERVICES})

_INTERNATIONAL_ALLOWED = (
    "study_planning_consulting",
    "school_information_and_selection_advice",
    "document_checklist_guidance_and_document_preparation",
    "application_process_guidance_and_coordination",
    "offer_coe_caaw_oshc_process_assistance",
    "visa_document_checklist_guidance",
    "visa_application_form_information_preparation_assistance",
    "student_visa_lodgement",
    "subclass_590_guardian_visa_lodgement",
    "gs_study_plan_consulting",
    "subclass_590_guardian_visa_consulting",
    "accommodation_airport_pickup_insurance_prearrival_consulting",
)
_INTERNATIONAL_DEFAULTS = tuple(
    value
    for value in _INTERNATIONAL_ALLOWED
    if value
    not in {
        "subclass_590_guardian_visa_lodgement",
        "subclass_590_guardian_visa_consulting",
    }
)
_TRANSFER_DEFAULTS = (
    "school_transfer_planning_and_application",
    "school_information_and_selection_advice",
    "document_checklist_guidance_and_document_preparation",
    "application_process_guidance_and_coordination",
    "offer_coe_oshc_process_assistance",
)
_LOCAL_DEFAULTS = (
    "study_planning_consulting",
    "school_information_and_selection_advice",
    "document_checklist_guidance_and_document_preparation",
    "application_process_guidance_and_coordination",
    "offer_and_enrolment_coordination",
)

_CONFIGS = (
    ContractTypeConfig(
        NEW_INTERNATIONAL_STUDENT_CONTRACT,
        "新留学生合同",
        "澳洲留学咨询及签证服务委托合同",
        _INTERNATIONAL_ALLOWED,
        _INTERNATIONAL_DEFAULTS,
        "international",
        ("澳洲留学咨询及", "签证服务委托合同"),
        "留学咨询 / 学校申请 / 签证服务",
        "Formal education consulting and visa service agreement",
        "留学咨询及签证服务费",
        "甲乙双方本着自愿、平等、诚实信用的原则，经友好协商，就乙方委托甲方提供澳洲留学咨询及签证相关服务事宜，订立本合同，以便共同遵守。",
        "甲方根据乙方委托，向乙方提供以下一项或多项澳洲留学咨询及签证相关服务：",
    ),
    ContractTypeConfig(
        TRANSFER_APPLICATION_CONTRACT,
        "转学申请合同",
        "转学申请服务委托合同",
        _TRANSFER_DEFAULTS,
        _TRANSFER_DEFAULTS,
        "transfer",
        ("转学申请", "服务委托合同"),
        "转学规划 / 学校申请 / 入学协调",
        "School transfer application service agreement",
        "转学申请服务费",
        "甲乙双方本着自愿、平等、诚实信用的原则，经友好协商，就乙方委托甲方提供转学规划、学校申请及相关协调服务事宜，订立本合同，以便共同遵守。",
        "甲方根据乙方委托，提供以下一项或多项转学规划、学校申请及相关协调服务：",
    ),
    ContractTypeConfig(
        LOCAL_STUDENT_APPLICATION_CONTRACT,
        "本地生申请合同",
        "本地生申请服务委托合同",
        _LOCAL_DEFAULTS,
        _LOCAL_DEFAULTS,
        "local",
        ("本地生申请", "服务委托合同"),
        "升学规划 / 学校申请 / 入学协调",
        "Local student application service agreement",
        "本地生申请服务费",
        "甲乙双方本着自愿、平等、诚实信用的原则，经友好协商，就乙方委托甲方提供本地生升学规划、学校申请及相关协调服务事宜，订立本合同，以便共同遵守。",
        "甲方根据乙方委托，提供以下一项或多项本地生升学规划、学校申请及相关协调服务：",
    ),
)
CONTRACT_TYPE_CONFIGS = MappingProxyType({config.type_id: config for config in _CONFIGS})


_CLAUSE_PROFILES = MappingProxyType(
    {
        "international": MappingProxyType(
            {
                "三、服务费用与第三方费用": (
                    "乙方确认并理解，本合同项下服务费为甲方向乙方提供澳洲留学咨询、申请材料整理与流程指导、签证咨询与材料指导、入学前沟通协调、住宿、接机、保险及其他相关事项咨询协助服务的费用。\n"
                    "本合同项下服务费不包括学校、教育机构、教育部门、保险机构、住宿机构、移民局或其他第三方收取的费用。包括但不限于学校申请费、学费、医疗保险费、住宿费、监护或福利安排费、接机费、体检费、签证申请费、翻译费、公证费、银行手续费及其他第三方费用。上述费用由乙方按照相关收费主体的要求另行承担。\n"
                    "甲方可根据乙方委托，在服务范围内协助乙方进行信息整理、材料准备、流程沟通及相关事项协调，但第三方是否受理、批准、录取、签发或退费，均以相关第三方的政策、标准及最终决定为准。"
                ),
                "四、甲方权利义务": (
                    "甲方有权按照本合同约定向乙方收取留学咨询及签证服务费，以及双方书面确认由甲方代收代付的其他费用。\n"
                    "甲方应在本合同有效期间，向乙方介绍澳洲教育制度、学校或课程申请流程、入学要求、签证制度、签证材料要求及申请准备注意事项。\n"
                    "甲方可根据乙方授权，为乙方利益与学校、教育机构、保险机构、住宿机构、移民局或其他相关第三方进行沟通，包括发送或回复信函、电子邮件及其他申请相关信息。涉及乙方个人重大决定的事项，甲方应及时通知乙方并取得乙方确认。\n"
                    "如乙方需要住宿、接机、监护或福利安排等延伸协助，甲方可在服务范围内协助乙方联系、沟通及整理相关信息，具体费用和结果以第三方要求及决定为准。\n"
                    "甲方对乙方提交的个人资料和申请材料承担保密义务；除为履行本合同、获得乙方同意或法律法规要求外，不向无关第三方披露。"
                ),
                "五、乙方权利义务": (
                    "乙方应保证向甲方提交的所有文件、材料、信息和陈述合法、真实、完整、有效；如有虚假、隐瞒、遗漏或迟延提交，相关后果由乙方自行承担。\n"
                    "乙方应按甲方合理要求及时提供申请、签证、住宿、保险、监护或其他相关材料，并配合完成表格确认、签字、付款、面试及补充材料等事项。\n"
                    "在服务过程中，如学生、家长、担保人、监护人或家庭成员的联系方式、地址、工作、收入、健康、学习、签证或其他重要情况发生变化，乙方应及时通知甲方。\n"
                    "乙方应保证本合同载明的电话、邮箱和地址准确有效，并及时查阅及回复甲方通知。因乙方联系方式无效、未及时回复或未配合导致申请延误、失败或产生额外费用的，由乙方自行承担。\n"
                    "未经甲方书面同意，乙方不得泄露、转交或挪用甲方在服务过程中制作或提供的申请方案、材料清单、表格、说明、流程文件、数据统计信息或其他具有保密意义的资料。"
                ),
                "六、退费及违约责任": (
                    "乙方确认，甲方根据本合同已提供澳洲留学咨询、规划、材料整理、签证指导、表格信息整理、流程沟通协调及其他相关咨询协助服务后，已实际提供的对应服务部分不予退还。\n"
                    "乙方向学校、教育机构、教育部门、保险机构、住宿机构、移民局或其他第三方支付的费用，包括但不限于学校申请费、学费、医疗保险费、住宿费、监护或福利安排费、接机费、体检费、签证申请费、翻译费、公证费、银行手续费及其他第三方费用，由乙方按照相关第三方的政策及收费、退费规则自行承担。甲方可根据乙方要求协助沟通相关退费事宜，但不对第三方是否退费、退费金额、退费时间或退费结果作出保证。\n"
                    "如乙方签证申请被拒签，且乙方明确表示不再继续申请，甲方可从乙方已支付的留学咨询及签证服务费中扣除 AUD$ 900，并向乙方退还剩余部分。\n"
                    "如按照甲乙双方确认的申请学校方案进行申请，学生最终未被任何一所学校录取，则甲方已收取的服务费用将予以全额退还。\n"
                    "如乙方在签约后因自身原因中途终止服务、未及时提交材料、未按要求支付第三方费用、未配合完成申请表签署或面试、或存在其他单方原因导致服务无法继续履行的，所缴纳服务费不予以退还。\n"
                    "如乙方提供虚假文件或隐瞒重要事实，包括但不限于学习、健康、出勤、纪律、签证历史、家庭资金或其他会影响申请判断的信息，导致申请延误、拒绝、撤销或合同无法履行的，乙方应自行承担后果；给甲方造成损失的，甲方有权依法追偿。"
                ),
                "七、不可抗力、结果说明及免责": (
                    "甲方提供的是咨询、材料指导、流程协调及相关协助服务。学校录取、教育机构或教育部门审批、保险安排、住宿安排、签证结果及其他第三方事项，均由相关学校、教育机构、教育部门、保险机构、住宿机构、移民局或其他第三方依据其政策和标准独立决定。甲方不保证乙方一定获得录取、签证或其他第三方批准结果。\n"
                    "如因自然灾害、疫情、战争、骚乱、政策变化、系统故障、第三方审理延误、第三方要求变化或其他不能预见、不能避免且不能克服的客观情况导致服务延误、中止或无法按原计划完成，双方应友好协商解决，甲方不对超出自身合理控制范围的结果承担保证责任。"
                ),
                "九、合同补充、变更和生效": (
                    "本合同的任何补充、变更或修改应采用书面形式，经双方确认后与本合同具有同等法律效力。\n"
                    "如乙方或学生在签署本合同时未满十八周岁或不具备完全民事行为能力，应由其法定代理人或监护人签署或确认后生效。\n"
                    "本合同自双方签字或盖章之日起生效。本合同可采用纸质签署、扫描件、电子签名或双方认可的其他书面确认方式签署，具有同等效力。\n"
                    "申请澳洲留学及签证相关服务属乙方自愿；服务过程中发生但本合同未列明的个人生活费、误工费、误学费或其他个人费用，由乙方自行承担。"
                ),
            }
        ),
        "transfer": MappingProxyType(
            {
                "三、服务费用与第三方费用": (
                    "本合同服务费对应转学规划、选校、材料整理、学校申请、Offer、COE、OSHC及相关沟通协调。\n"
                    "学校、教育机构、保险机构或其他第三方收取的申请费、学费、OSHC及其他费用由乙方另行承担。\n"
                    "第三方是否受理、录取、签发文件或退费，以该第三方的政策、标准及最终决定为准。"
                ),
                "四、甲方权利义务": (
                    "甲方按照本合同服务范围提供转学规划、学校信息、材料准备、学校申请、Offer、COE、OSHC及相关流程的咨询与协调，并对乙方资料承担保密义务。"
                ),
                "五、乙方权利义务": (
                    "乙方应保证资料真实、完整、有效，及时配合转学和学校申请、付款、面试及补充材料，并及时通知联系方式或重要情况的变化。"
                ),
                "六、退费及违约责任": (
                    "乙方确认，甲方已实际提供的转学规划、选校、材料整理、学校申请及流程协调服务部分不予退还。\n"
                    "学校、教育机构、保险机构或其他第三方费用按相应第三方政策处理。\n"
                    "如乙方提供虚假材料、隐瞒重要事实或因自身原因终止服务，应承担由此产生的后果。"
                ),
                "七、不可抗力、结果说明及免责": (
                    "学校录取、教育机构审批、COE、OSHC及其他第三方事项由相关第三方独立决定，甲方不保证录取或其他批准结果。\n"
                    "因不可抗力、政策变化、系统故障或第三方处理延误导致服务变化的，双方应友好协商。"
                ),
                "九、合同补充、变更和生效": (
                    "本合同的补充、变更或修改应采用书面形式，经双方确认后与本合同具有同等法律效力。\n"
                    "未满十八周岁的学生应由法定代理人或监护人签署或确认。\n"
                    "本合同可采用纸质、扫描件或电子签名方式签署，自双方签字或盖章之日起生效。"
                ),
            }
        ),
        "local": MappingProxyType(
            {
                "三、服务费用与第三方费用": (
                    "本合同服务费对应选校、材料整理、学校申请、沟通协调、录取与入学确认支持。\n"
                    "学校、教育机构或其他第三方收取的申请费、学费及其他费用由乙方另行承担。\n"
                    "第三方是否受理、录取或退费，以该第三方的政策、标准及最终决定为准。"
                ),
                "四、甲方权利义务": (
                    "甲方按照本合同服务范围提供学校信息、选校建议、材料准备、学校申请、录取与入学确认流程的咨询与协调，并对乙方资料承担保密义务。"
                ),
                "五、乙方权利义务": (
                    "乙方应保证资料真实、完整、有效，及时配合学校申请、付款、面试及补充材料，并及时通知联系方式或重要情况的变化。"
                ),
                "六、退费及违约责任": (
                    "乙方确认，甲方已实际提供的选校、材料整理、学校申请、沟通协调及录取支持服务部分不予退还。\n"
                    "学校、教育机构或其他第三方费用按相应第三方政策处理。\n"
                    "如乙方提供虚假材料、隐瞒重要事实或因自身原因终止服务，应承担由此产生的后果。"
                ),
                "七、不可抗力、结果说明及免责": (
                    "学校录取、教育机构审批及其他第三方事项由相关第三方独立决定，甲方不保证录取或其他批准结果。\n"
                    "因不可抗力、政策变化、系统故障或第三方处理延误导致服务变化的，双方应友好协商。"
                ),
                "九、合同补充、变更和生效": (
                    "本合同的补充、变更或修改应采用书面形式，经双方确认后与本合同具有同等法律效力。\n"
                    "未满十八周岁的学生应由法定代理人或监护人签署或确认。\n"
                    "本合同可采用纸质、扫描件或电子签名方式签署，自双方签字或盖章之日起生效。"
                ),
            }
        ),
    }
)


class ContractDomainError(ValueError):
    pass


class ContractSignerValidationError(ContractDomainError):
    pass


def contract_type_config(type_id: str) -> ContractTypeConfig:
    normalized = normalize_contract_type(type_id)
    try:
        return CONTRACT_TYPE_CONFIGS[normalized]
    except KeyError as exc:
        raise ContractDomainError("合同类型无效。") from exc


def normalize_contract_type(type_id: object) -> str:
    value = str(type_id or DEFAULT_CONTRACT_TYPE).strip()
    legacy = {
        "": DEFAULT_CONTRACT_TYPE,
        "StudyAbroadContract": DEFAULT_CONTRACT_TYPE,
        "留学合同": DEFAULT_CONTRACT_TYPE,
        "新留学生合同": DEFAULT_CONTRACT_TYPE,
        "转学申请合同": TRANSFER_APPLICATION_CONTRACT,
        "本地生申请合同": LOCAL_STUDENT_APPLICATION_CONTRACT,
    }
    return legacy.get(value, value)


def contract_type_options() -> tuple[tuple[str, str], ...]:
    return tuple((config.type_id, config.label) for config in _CONFIGS)


def service_definitions() -> tuple[ServiceDefinition, ...]:
    return _SERVICES


def service_label(service_id: str) -> str:
    definition = SERVICE_CATALOG.get(str(service_id or ""))
    return definition.label if definition else str(service_id or "")


def allowed_service_ids(type_id: str) -> tuple[str, ...]:
    return contract_type_config(type_id).allowed_services


def default_service_ids(type_id: str) -> tuple[str, ...]:
    return contract_type_config(type_id).default_services


def normalize_selected_services(
    type_id: str,
    values: Iterable[str] | str | None,
    submitted: bool,
) -> tuple[str, ...]:
    if not submitted:
        return default_service_ids(type_id)
    raw_values = (values,) if isinstance(values, str) else tuple(values or ())
    allowed = set(allowed_service_ids(type_id))
    selected = []
    seen = set()
    for raw in raw_values:
        value = str(raw or "").strip()
        if value in allowed and value not in seen:
            selected.append(value)
            seen.add(value)
    return tuple(selected)


def service_scope_text(service_ids: Iterable[str], other_text: str = "") -> str:
    lines = [f"{index}. {service_label(value)}" for index, value in enumerate(service_ids, 1)]
    other = str(other_text or "").strip()
    if other:
        lines.append(f"其他服务范围：{other}")
    return "\n".join(lines)


def compose_contract_sections(type_id: str) -> dict[str, str]:
    config = contract_type_config(type_id)
    return dict(_CLAUSE_PROFILES[config.clause_profile])


def _normalized_parent_source(parent_type: object) -> str:
    value = str(parent_type or "").strip().lower()
    if value in {"father", "父亲", "父", "dad"}:
        return "father"
    if value in {"mother", "母亲", "母", "mum", "mom"}:
        return "mother"
    return ""


def _name(first: object, last: object) -> str:
    return " ".join(part for part in (str(first or "").strip(), str(last or "").strip()) if part)


def signer_candidate(
    source: str,
    student_data: Mapping[str, object] | None,
    parent_rows: Iterable[Mapping[str, object]] | None,
) -> dict[str, str]:
    """Return one parent's fields without borrowing from the other parent.

    Incomplete candidates are intentional so the shared contract form can ask
    the operator to fill only the missing snapshot values.
    """
    normalized_source = str(source or "").strip().lower()
    if normalized_source not in {"mother", "father"}:
        raise ContractSignerValidationError("请选择父亲或母亲作为乙方签署人。")
    student_data = student_data or {}
    parent = next(
        (row for row in (parent_rows or ()) if _normalized_parent_source(row.get("parent_type")) == normalized_source),
        {},
    )
    first = parent.get("passport_first_name") or student_data.get(f"{normalized_source}_passport_first_name")
    last = parent.get("passport_last_name") or student_data.get(f"{normalized_source}_passport_last_name")
    return {
        "source": normalized_source,
        "name": _name(first, last),
        "relationship": "母亲" if normalized_source == "mother" else "父亲",
        "phone": str(parent.get("mobile") or student_data.get(f"{normalized_source}_mobile") or "").strip(),
        "email": str(parent.get("email") or student_data.get(f"{normalized_source}_email") or "").strip(),
    }


def resolve_signer(
    source: str,
    student_data: Mapping[str, object] | None,
    parent_rows: Iterable[Mapping[str, object]] | None,
    overrides: Mapping[str, object] | None = None,
) -> dict[str, str]:
    normalized_source = str(source or "").strip().lower()
    if normalized_source not in {"mother", "father"}:
        raise ContractSignerValidationError("请选择父亲或母亲作为乙方签署人。")
    overrides = overrides or {}
    candidate = signer_candidate(normalized_source, student_data, parent_rows)
    name = str(overrides.get("name") or candidate["name"]).strip()
    phone = str(overrides.get("phone") or candidate["phone"]).strip()
    email = str(overrides.get("email") or candidate["email"]).strip()
    relationship = str(overrides.get("relationship") or candidate["relationship"]).strip()
    missing = []
    if not name:
        missing.append("姓名")
    if not phone:
        missing.append("电话")
    if not email:
        missing.append("邮箱")
    if missing:
        raise ContractSignerValidationError("所选签署人缺少" + "、".join(missing) + "，请在合同页补填。")
    return {
        "source": normalized_source,
        "name": name,
        "relationship": relationship,
        "phone": phone,
        "email": email,
    }


def build_contract_snapshot(payload: Mapping[str, object]) -> dict[str, object]:
    snapshot = copy.deepcopy(dict(payload))
    snapshot["snapshot_version"] = SNAPSHOT_VERSION
    contract_type_config(str(snapshot.get("contract_type") or DEFAULT_CONTRACT_TYPE))
    return snapshot


def snapshot_payload(raw_json: str | Mapping[str, object] | None) -> dict[str, object]:
    if not raw_json:
        return {}
    if isinstance(raw_json, Mapping):
        data = copy.deepcopy(dict(raw_json))
    else:
        try:
            data = json.loads(str(raw_json))
        except (TypeError, json.JSONDecodeError):
            return {}
    snapshot = data.get("contract_snapshot") if isinstance(data, dict) else None
    if isinstance(snapshot, dict):
        return copy.deepcopy(snapshot)
    legacy = data.get("contract_payload") if isinstance(data, dict) else None
    return copy.deepcopy(legacy) if isinstance(legacy, dict) else {}


def snapshot_storage_json(snapshot: Mapping[str, object], student_data: Mapping[str, object] | None = None) -> str:
    return json.dumps(
        {
            "student_form_data": copy.deepcopy(dict(student_data or {})),
            "contract_snapshot": build_contract_snapshot(snapshot),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
