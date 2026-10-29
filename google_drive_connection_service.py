from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class ConnectionStatus:
    code: str
    label: str
    detail: str
    last_verified_at: str
    folder_id: str
    oauth_ready: bool


class GoogleDriveConnectionService:
    def __init__(
        self,
        *,
        setting: Callable[[str, str], object],
        token_is_expired: Callable[[str], bool],
        missing_scopes: Callable[[str], list[str]],
        latest_connection_event: Callable[[], dict],
    ):
        self.setting = setting
        self.token_is_expired = token_is_expired
        self.missing_scopes = missing_scopes
        self.latest_connection_event = latest_connection_event

    def _value(self, key: str) -> str:
        return str(self.setting(key, "") or "").strip()

    @staticmethod
    def _safe_error_code(value: object) -> str:
        text = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or ""))[:80]
        return text or "connection_failed"

    def status(self) -> ConnectionStatus:
        client_id = self._value("GOOGLE_DRIVE_CLIENT_ID")
        client_secret = self._value("GOOGLE_DRIVE_CLIENT_SECRET")
        refresh_token = self._value("GOOGLE_DRIVE_REFRESH_TOKEN")
        access_token = self._value("GOOGLE_DRIVE_ACCESS_TOKEN")
        expiry = self._value("GOOGLE_DRIVE_TOKEN_EXPIRY")
        scope = self._value("GOOGLE_DRIVE_SCOPE")
        configured_status = self._value("GOOGLE_DRIVE_CONNECTION_STATUS")
        folder_id = self._value("CONTENT_GOOGLE_DRIVE_VIDEO_FOLDER_ID")
        oauth_ready = bool(client_id and client_secret and refresh_token)
        event = self.latest_connection_event() or {}
        last_verified_at = str(event.get("created_at") or "")[:40]

        if not (client_id and client_secret and folder_id):
            return ConnectionStatus(
                "not_configured",
                "未配置",
                "Google Drive OAuth 客户端或目标文件夹尚未完整配置。",
                last_verified_at,
                folder_id,
                oauth_ready,
            )

        normalized_status = configured_status.lower().replace("_", " ")
        missing = self.missing_scopes(scope)
        expired = bool(expiry and self.token_is_expired(expiry))
        if "reconnect" in normalized_status or "expired" in normalized_status or "invalid" in normalized_status:
            return ConnectionStatus(
                "authorization_expired",
                "授权失效",
                "Google Drive 授权需要重新连接。",
                last_verified_at,
                folder_id,
                oauth_ready,
            )
        if missing:
            return ConnectionStatus(
                "authorization_expired",
                "授权失效",
                "Google Drive 权限范围不足，需要重新授权。",
                last_verified_at,
                folder_id,
                oauth_ready,
            )
        if not access_token and not oauth_ready:
            return ConnectionStatus(
                "authorization_expired",
                "授权失效",
                "Google Drive 没有当前可用的访问授权，请重新连接。",
                last_verified_at,
                folder_id,
                oauth_ready,
            )
        if expired and not oauth_ready:
            return ConnectionStatus(
                "authorization_expired",
                "授权失效",
                "Google Drive 访问授权已过期，且没有可用的刷新授权，请重新连接。",
                last_verified_at,
                folder_id,
                oauth_ready,
            )
        if str(event.get("status") or "").lower() == "failed":
            code = self._safe_error_code(event.get("error_code"))
            return ConnectionStatus(
                "connection_error",
                "连接异常",
                f"最近连接检查失败：{code}",
                last_verified_at,
                folder_id,
                oauth_ready,
            )

        if not access_token or expired:
            detail = "Google Drive 已连接；操作时将使用现有刷新授权临时刷新访问权限，不覆盖现有 refresh token。"
        else:
            detail = "Google Drive 授权配置和最近连接状态正常。"
        return ConnectionStatus(
            "connected",
            "已连接",
            detail,
            last_verified_at,
            folder_id,
            oauth_ready,
        )
