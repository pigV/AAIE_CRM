"""Independent content extraction service domain package."""

from .errors import ExtractionError, ExtractionErrorCode
from .models import LinkCandidate, NormalizedLink, Platform, RedirectHop


LIFECYCLE_STATUS = "EXPERIMENTAL / PRODUCTION_REJECTED"
PRODUCTION_ACCEPTED = False

__all__ = [
    "ExtractionError",
    "ExtractionErrorCode",
    "LIFECYCLE_STATUS",
    "LinkCandidate",
    "NormalizedLink",
    "Platform",
    "PRODUCTION_ACCEPTED",
    "RedirectHop",
]
