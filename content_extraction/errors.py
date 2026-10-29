from enum import Enum


class ExtractionErrorCode(str, Enum):
    LINK_NOT_FOUND = "LINK_NOT_FOUND"
    LINK_INVALID = "LINK_INVALID"
    LINK_AMBIGUOUS = "LINK_AMBIGUOUS"
    PLATFORM_UNSUPPORTED = "PLATFORM_UNSUPPORTED"
    REDIRECT_FAILED = "REDIRECT_FAILED"
    LOGIN_EXPIRED = "LOGIN_EXPIRED"
    VIDEO_RESOLVE_FAILED = "VIDEO_RESOLVE_FAILED"
    VIDEO_DOWNLOAD_FAILED = "VIDEO_DOWNLOAD_FAILED"
    VIDEO_UNAVAILABLE = "VIDEO_UNAVAILABLE"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"


class ExtractionError(Exception):
    def __init__(
        self,
        code: ExtractionErrorCode,
        stage: str,
        user_message: str,
        internal_detail: str = "",
    ):
        super().__init__(user_message)
        self.code = code
        self.stage = str(stage or "")
        self.user_message = str(user_message or "")
        self.internal_detail = str(internal_detail or "")

    def __str__(self):
        return self.user_message
