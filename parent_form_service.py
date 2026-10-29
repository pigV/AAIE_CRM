"""Business rules for secure, resumable parent forms."""

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import hmac
import json
import sqlite3
from typing import Any, Mapping

from fields import FIELD_MAP
from parent_form_repository import BindingIdentityConflict, LinkRegenerationConflict


PARENT_FORM_LINK_DAYS = 7
PARENT_FORM_INTAKE_TYPES = frozenset(
    {"ordinary_new_application", "new_student_transfer"}
)


class ParentFormError(RuntimeError):
    pass


class ParentFormNotFound(ParentFormError):
    pass


class ParentFormExpired(ParentFormError):
    pass


class ParentFormRevoked(ParentFormError):
    pass


class ParentFormSubmitted(ParentFormError):
    pass


class ParentFormAuthenticationRequired(ParentFormError):
    pass


class ParentFormIdentityMismatch(ParentFormError):
    pass


class ParentFormIntakeTypeInvalid(ParentFormError):
    pass


class ParentFormIntakeCorrectionBlocked(ParentFormError):
    pass


@dataclass(frozen=True)
class LinkRecord:
    token: str
    consultant_id: int | None
    student_hint: str
    expires_at: str
    intake_type: str | None


@dataclass(frozen=True)
class AccessContext:
    token: str
    openid: str
    expires_at: str
    binding: Mapping[str, Any]


@dataclass(frozen=True)
class DraftSnapshot:
    token: str
    values: dict[str, Any]
    revision: int
    save_source: str
    last_saved_at: str


def parent_form_cookie_name(token):
    suffix = hashlib.sha256(str(token).encode("utf-8")).hexdigest()[:20]
    return f"aaie_parent_form_{suffix}"


def parent_form_cookie_value(token, openid, secret):
    message = f"parent-form|{token}|{openid}".encode("utf-8")
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


class ParentFormService:
    def __init__(self, repository, *, clock, token_factory, encrypt, decrypt):
        self.repository = repository
        self.clock = clock
        self.token_factory = token_factory
        self.encrypt = encrypt
        self.decrypt = decrypt

    @staticmethod
    def _validated_intake_type(value, *, allow_none=False):
        normalized = str(value or "").strip()
        if not normalized and allow_none:
            return None
        if normalized not in PARENT_FORM_INTAKE_TYPES:
            raise ParentFormIntakeTypeInvalid(
                "parent form intake type must be selected explicitly"
            )
        return normalized

    def create_link(
        self,
        consultant_id,
        student_hint,
        created_by,
        *,
        student_id=None,
        intake_type=None,
    ):
        intake_type = self._validated_intake_type(intake_type)
        now = self.clock()
        expires_at = (now + timedelta(days=PARENT_FORM_LINK_DAYS)).isoformat(
            timespec="seconds"
        )
        args = (
            self.token_factory(),
            consultant_id,
            student_hint,
            expires_at,
            created_by,
            now.isoformat(timespec="seconds"),
        )
        row = (
            self.repository.create_link(
                *args, student_id=student_id, intake_type=intake_type
            )
            if student_id is not None
            else self.repository.create_link(*args, intake_type=intake_type)
        )
        return self._link_record(row)

    def regenerate_link(self, old_token, created_by, *, intake_type=None):
        old_link = self.repository.get_link(old_token)
        if not old_link:
            raise ParentFormNotFound("parent form link not found")
        old_type = old_link["intake_type"] if "intake_type" in old_link.keys() else None
        selected_type = self._validated_intake_type(
            intake_type if intake_type is not None else old_type
        )
        now = self.clock()
        expires_at = (now + timedelta(days=PARENT_FORM_LINK_DAYS)).isoformat(
            timespec="seconds"
        )
        try:
            row = self.repository.regenerate_link(
                old_token,
                self.token_factory(),
                expires_at,
                created_by,
                now.isoformat(timespec="seconds"),
                intake_type=selected_type,
            )
        except LinkRegenerationConflict as exc:
            if exc.reason == "missing":
                raise ParentFormNotFound("parent form link not found") from exc
            raise ParentFormSubmitted(
                "the original link can no longer be regenerated"
            ) from exc
        return self._link_record(row)

    def correct_intake_type(self, token, new_intake_type, *, changed_by):
        row = self.repository.get_link(token)
        if not row:
            raise ParentFormNotFound("parent form link not found")
        previous_type = row["intake_type"] if "intake_type" in row.keys() else None
        if previous_type is None:
            raise ParentFormIntakeTypeInvalid(
                "historical untyped links cannot be corrected in place"
            )
        new_type = self._validated_intake_type(new_intake_type)
        if previous_type == new_type:
            return self._link_record(row)
        try:
            corrected = self.repository.correct_intake_type(
                token,
                previous_type,
                new_type,
                changed_by,
                self.clock().isoformat(timespec="seconds"),
            )
        except sqlite3.IntegrityError as exc:
            raise ParentFormIntakeCorrectionBlocked(
                "parent form intake type is locked because formal business has started"
            ) from exc
        return self._link_record(corrected)

    def bind_wechat(self, token, identity):
        self._ensure_writable(token)
        openid = str(identity.get("openid") or "").strip()
        if not openid:
            raise ParentFormAuthenticationRequired("WeChat identity is required")
        try:
            return self.repository.bind_identity(
                token,
                identity,
                self.clock().isoformat(timespec="seconds"),
            )
        except BindingIdentityConflict as exc:
            raise ParentFormIdentityMismatch(
                "this parent form is bound to another WeChat identity"
            ) from exc

    def record_open(self, token):
        if not self.repository.get_link(token):
            return False
        self.repository.record_access_event(
            token,
            "opened",
            self.clock().isoformat(timespec="seconds"),
        )
        return True

    def authorize(self, token, openid):
        link = self._ensure_writable(token)
        if not openid:
            raise ParentFormAuthenticationRequired("WeChat login is required")
        binding = self.repository.get_binding(token)
        if not binding:
            raise ParentFormAuthenticationRequired("WeChat binding is required")
        if not hmac.compare_digest(
            str(binding["bound_wechat_openid"]), str(openid)
        ):
            raise ParentFormIdentityMismatch(
                "this parent form is bound to another WeChat identity"
            )
        return AccessContext(
            token=token,
            openid=str(openid),
            expires_at=link["expires_at"],
            binding=binding,
        )

    def load_draft(self, token, openid):
        self.authorize(token, openid)
        draft = self.repository.load_draft(token)
        if not draft:
            return DraftSnapshot(
                token=token,
                values=self._clean_values({}),
                revision=0,
                save_source="",
                last_saved_at="",
            )
        plaintext = self.decrypt(draft.payload_encrypted.encode("utf-8"))
        values = json.loads(plaintext.decode("utf-8"))
        return DraftSnapshot(
            token=token,
            values=self._clean_values(values),
            revision=draft.revision,
            save_source=draft.save_source,
            last_saved_at=draft.last_saved_at,
        )

    def save_draft(
        self,
        token,
        openid,
        values,
        expected_revision,
        source,
    ):
        self.authorize(token, openid)
        clean = self._clean_values(values)
        payload = json.dumps(
            clean,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        encrypted = self.encrypt(payload)
        if isinstance(encrypted, bytes):
            encrypted = encrypted.decode("utf-8")
        stored = self.repository.save_draft(
            token,
            encrypted,
            expected_revision=expected_revision,
            source=source,
            stamp=self.clock().isoformat(timespec="seconds"),
        )
        return DraftSnapshot(
            token=token,
            values=clean,
            revision=stored.revision,
            save_source=stored.save_source,
            last_saved_at=stored.last_saved_at,
        )

    def status(self, token):
        link = self.repository.get_link(token)
        if not link:
            raise ParentFormNotFound("parent form link not found")
        if link["submitted_at"]:
            return "submitted"
        if link["invalidated_at"]:
            return "revoked"
        if self._parse_timestamp(link["expires_at"]) <= self.clock():
            return "expired"
        return "pending"

    def _ensure_writable(self, token):
        link = self.repository.get_link(token)
        if not link:
            raise ParentFormNotFound("parent form link not found")
        status = self.status(token)
        if status == "submitted":
            raise ParentFormSubmitted("parent form has already been submitted")
        if status == "revoked":
            raise ParentFormRevoked("parent form link has been revoked")
        if status == "expired":
            raise ParentFormExpired("parent form link has expired")
        return link

    def _parse_timestamp(self, value):
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        now = self.clock()
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=now.tzinfo)
        return parsed

    @staticmethod
    def _link_record(row):
        return LinkRecord(
            token=row["token"],
            consultant_id=row["consultant_id"],
            student_hint=row["student_hint"] or "",
            expires_at=row["expires_at"],
            intake_type=(
                row["intake_type"] if "intake_type" in row.keys() else None
            ),
        )

    @staticmethod
    def _clean_values(values):
        clean = {}
        for key, meta in FIELD_MAP.items():
            default = [] if meta["type"] == "checkbox" else ""
            value = values.get(key, default)
            if meta["type"] == "checkbox":
                if isinstance(value, (tuple, set)):
                    value = list(value)
                elif not isinstance(value, list):
                    value = [value] if value else []
            clean[key] = value
        return clean
