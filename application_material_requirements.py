"""Application document requirement policy keyed by applying year level."""


DEFAULT_REQUIRED_APPLICATION_DOCUMENT_TYPES = frozenset(
    {"StudentPassport", "BirthCertificate", "Transcripts", "Vaccination"}
)
BIRTH_CERTIFICATE_OPTIONAL_APPLICATION_LEVELS = frozenset(
    {"预科+本科", "本科", "研究生"}
)


def _normalized_level(applying_year_level):
    return str(applying_year_level or "").strip()


def required_application_document_types(applying_year_level):
    required = DEFAULT_REQUIRED_APPLICATION_DOCUMENT_TYPES
    if _normalized_level(applying_year_level) in BIRTH_CERTIFICATE_OPTIONAL_APPLICATION_LEVELS:
        return required - {"BirthCertificate"}
    return required


def missing_required_application_document_types(applying_year_level, material_statuses):
    required = required_application_document_types(applying_year_level)
    return tuple(
        document_type
        for document_type, count in material_statuses
        if document_type in required and not count
    )
