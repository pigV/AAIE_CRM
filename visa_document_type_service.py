from dataclasses import dataclass


class InvalidVisaDocumentType(ValueError):
    pass


class VisaDocumentNotFound(LookupError):
    pass


@dataclass(frozen=True)
class VisaDocumentTypeDefinition:
    form_value: str
    label: str
    document_type: str


VISA_DOCUMENT_TYPE_DEFINITIONS = (
    VisaDocumentTypeDefinition("other", "Other / 其他", "VisaOther"),
    VisaDocumentTypeDefinition("grant_letter", "Grant Letter / 下签信", "VisaGrantLetter"),
    VisaDocumentTypeDefinition("refusal_letter", "Refusal Letter / 拒签信", "VisaRefusalLetter"),
    VisaDocumentTypeDefinition("s56_request", "S56 Request / S56 补料信", "VisaS56Request"),
    VisaDocumentTypeDefinition("s56_response", "S56 Response / S56 补料回复", "VisaS56Response"),
    VisaDocumentTypeDefinition("lodgement_receipt", "Lodgement Receipt / 签证递交收据", "VisaLodgementReceipt"),
    VisaDocumentTypeDefinition(
        "immi_acknowledgment",
        "IMMI Acknowledgment Letter / IMMI 递交确认信",
        "VisaImmiAcknowledgmentLetter",
    ),
    VisaDocumentTypeDefinition("bridging_visa", "Bridging Visa / 过桥签证", "VisaBridgingVisa"),
    VisaDocumentTypeDefinition("health_examination", "Health Examination / 体检材料", "VisaHealthExamination"),
    VisaDocumentTypeDefinition("hap_id", "HAP ID / 体检编号", "VisaHapId"),
    VisaDocumentTypeDefinition(
        "withdrawal_confirmation",
        "Withdrawal Confirmation / 撤签确认",
        "VisaWithdrawalConfirmation",
    ),
)

_BY_FORM_VALUE = {item.form_value: item for item in VISA_DOCUMENT_TYPE_DEFINITIONS}
_BY_DOCUMENT_TYPE = {item.document_type: item for item in VISA_DOCUMENT_TYPE_DEFINITIONS}


def visa_document_type_definition(value: object, *, strict: bool = False) -> VisaDocumentTypeDefinition:
    normalized = str(value or "").strip()
    definition = _BY_FORM_VALUE.get(normalized) or _BY_DOCUMENT_TYPE.get(normalized)
    if definition:
        return definition
    if normalized == "VisaFile":
        return _BY_FORM_VALUE["other"]
    if strict:
        raise InvalidVisaDocumentType("请选择有效的 VISA 文件类型。")
    return _BY_FORM_VALUE["other"]


@dataclass(frozen=True)
class VisaDocumentTypeUpdate:
    document_id: int
    old_document_type: str
    new_document_type: str
    changed: bool


def update_existing_visa_document_type(
    conn,
    *,
    student_id: str,
    file_path: str,
    requested_value: object,
) -> VisaDocumentTypeUpdate:
    definition = visa_document_type_definition(requested_value, strict=True)
    row = conn.execute(
        """SELECT id,document_type FROM student_documents
           WHERE student_id=? AND file_path=?
           ORDER BY uploaded_at DESC,id DESC LIMIT 1""",
        (student_id, file_path),
    ).fetchone()
    if row is None:
        raise VisaDocumentNotFound("找不到该 VISA 记录关联的文件，文件类型未更新。")

    old_document_type = str(row["document_type"] or "")
    changed = old_document_type != definition.document_type
    if changed:
        cursor = conn.execute(
            """UPDATE student_documents SET document_type=?
               WHERE id=? AND student_id=? AND file_path=?""",
            (definition.document_type, row["id"], student_id, file_path),
        )
        if cursor.rowcount != 1:
            raise VisaDocumentNotFound(
                "找不到该 VISA 记录关联的文件，文件类型未更新。"
            )
    return VisaDocumentTypeUpdate(
        document_id=int(row["id"]),
        old_document_type=old_document_type,
        new_document_type=definition.document_type,
        changed=changed,
    )
