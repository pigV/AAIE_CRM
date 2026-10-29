import json
from dataclasses import dataclass
import urllib.error
import urllib.request
from urllib.parse import quote

from .models import ClientResult


@dataclass(frozen=True)
class ClientMediaResult:
    ok: bool
    status_code: int
    content_type: str = ""
    body: bytes = b""
    error_code: str = ""
    user_message: str = ""


class ContentExtractionClient:
    def __init__(self, base_url: str, token: str, timeout: float = 2.0,
                 max_response_bytes: int = 1024 * 1024,
                 max_media_bytes: int = 16 * 1024 * 1024):
        self.base_url = str(base_url or "").rstrip("/")
        self.token = str(token or "")
        self.timeout = float(timeout)
        self.max_response_bytes = int(max_response_bytes)
        self.max_media_bytes = int(max_media_bytes)

    def _request(self, method: str, path: str, payload=None):
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={
                "Content-Type": "application/json",
                "X-AAIE-Extraction-Token": self.token,
            },
        )
        try:
            response = urllib.request.urlopen(request, timeout=self.timeout)
        except urllib.error.HTTPError as error:
            response = error
        except (OSError, TimeoutError, urllib.error.URLError):
            return ClientResult.unavailable()
        try:
            raw = response.read(self.max_response_bytes + 1)
            if len(raw) > self.max_response_bytes:
                return ClientResult(False, response.status, {}, "INVALID_RESPONSE", "提取服务返回异常")
            parsed = json.loads(raw.decode("utf-8"))
            if not isinstance(parsed, dict):
                raise ValueError("response is not an object")
        except (UnicodeError, ValueError, json.JSONDecodeError):
            return ClientResult(False, response.status, {}, "INVALID_RESPONSE", "提取服务返回异常")
        ok = bool(parsed.get("ok")) and 200 <= response.status < 300
        return ClientResult(
            ok=ok,
            status_code=int(response.status),
            payload=parsed,
            error_code=str(parsed.get("error_code") or ""),
            user_message=str(parsed.get("user_message") or parsed.get("error") or ""),
        )

    def health(self):
        return self._request("GET", "/health")

    def create_run(self, crm_job_id: int, raw_input: str, upload_path: str = ""):
        return self._request(
            "POST",
            "/runs",
            {
                "crm_job_id": int(crm_job_id),
                "raw_input": str(raw_input or ""),
                "upload_path": str(upload_path or ""),
            },
        )

    def get_run(self, crm_job_id: int):
        return self._request("GET", f"/runs/{int(crm_job_id)}")

    def continue_upload(self, crm_job_id: int, upload_path: str):
        return self._request(
            "POST",
            f"/runs/{int(crm_job_id)}/upload",
            {"upload_path": str(upload_path or "")},
        )

    def retry_run(self, crm_job_id: int):
        return self._request("POST", f"/runs/{int(crm_job_id)}/retry", {})

    def get_accurate_transcript(self, crm_job_id: int):
        return self._request("GET", f"/runs/{int(crm_job_id)}/accurate-transcript")

    def get_review(self, crm_job_id: int):
        return self._request("GET", f"/runs/{int(crm_job_id)}/review")

    def submit_review(self, crm_job_id: int, review_id: str,
                      source_queue_hash: str, expected_revision: int,
                      action: str, selected_text: str,
                      candidate_references, reviewer_source: str):
        encoded_id = quote(str(review_id or ""), safe="")
        return self._request(
            "POST", f"/runs/{int(crm_job_id)}/review/{encoded_id}",
            {
                "source_queue_hash": str(source_queue_hash or ""),
                "expected_revision": int(expected_revision),
                "action": str(action or ""),
                "selected_text": str(selected_text or ""),
                "candidate_references": [str(value) for value in candidate_references or ()],
                "reviewer_source": str(reviewer_source or ""),
            },
        )

    def get_review_media(self, crm_job_id: int, clip_id: str):
        encoded_id = quote(str(clip_id or ""), safe="")
        request = urllib.request.Request(
            self.base_url + f"/runs/{int(crm_job_id)}/review/media/{encoded_id}",
            method="GET",
            headers={"X-AAIE-Extraction-Token": self.token},
        )
        try:
            response = urllib.request.urlopen(request, timeout=self.timeout)
        except urllib.error.HTTPError as error:
            try:
                payload = json.loads(error.read(self.max_response_bytes + 1).decode("utf-8"))
            except (UnicodeError, ValueError, json.JSONDecodeError):
                payload = {}
            return ClientMediaResult(
                False, int(error.code),
                error_code=str(payload.get("error_code") or "INVALID_MEDIA_RESPONSE"),
                user_message=str(payload.get("user_message") or "审核片段不可用"),
            )
        except (OSError, TimeoutError, urllib.error.URLError):
            return ClientMediaResult(
                False, 503, error_code="SERVICE_UNAVAILABLE",
                user_message="提取服务不可用",
            )
        content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        body = response.read(self.max_media_bytes + 1)
        if content_type not in {"video/mp4", "audio/wav"} or len(body) > self.max_media_bytes:
            return ClientMediaResult(
                False, int(response.status), error_code="INVALID_MEDIA_RESPONSE",
                user_message="审核片段返回异常",
            )
        return ClientMediaResult(True, int(response.status), content_type, body)
