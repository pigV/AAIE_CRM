import unittest
from dataclasses import asdict

from google_drive_connection_service import GoogleDriveConnectionService


class GoogleDriveConnectionServiceTests(unittest.TestCase):
    def make_service(self, overrides=None, *, expired=False, missing_scopes=(), event=None):
        values = {
            "GOOGLE_DRIVE_CLIENT_ID": "synthetic-client-id",
            "GOOGLE_DRIVE_CLIENT_SECRET": "synthetic-client-secret",
            "GOOGLE_DRIVE_REFRESH_TOKEN": "synthetic-refresh-token",
            "GOOGLE_DRIVE_ACCESS_TOKEN": "synthetic-access-token",
            "GOOGLE_DRIVE_TOKEN_EXPIRY": "2099-08-10T04:08:06+10:00",
            "GOOGLE_DRIVE_SCOPE": "drive.readonly userinfo.email openid",
            "GOOGLE_DRIVE_CONNECTION_STATUS": "Connected",
            "CONTENT_GOOGLE_DRIVE_VIDEO_FOLDER_ID": "synthetic-folder-id",
        }
        values.update(overrides or {})
        return GoogleDriveConnectionService(
            setting=lambda key, default="": values.get(key, default),
            token_is_expired=lambda _expiry: expired,
            missing_scopes=lambda _scope: list(missing_scopes),
            latest_connection_event=lambda: event or {
                "status": "success",
                "created_at": "2026-08-10T03:10:17+10:00",
                "error_code": "",
            },
        )

    def test_connected_reports_safe_status_without_credential_values(self):
        status = self.make_service().status()

        self.assertEqual(status.code, "connected")
        self.assertEqual(status.label, "已连接")
        self.assertTrue(status.oauth_ready)
        self.assertEqual(status.folder_id, "synthetic-folder-id")
        encoded = repr(asdict(status))
        for secret in (
            "synthetic-client-id",
            "synthetic-client-secret",
            "synthetic-refresh-token",
            "synthetic-access-token",
        ):
            self.assertNotIn(secret, encoded)

    def test_expired_access_without_refresh_requires_authorization(self):
        status = self.make_service(
            {"GOOGLE_DRIVE_REFRESH_TOKEN": ""},
            expired=True,
        ).status()

        self.assertEqual(status.code, "authorization_expired")
        self.assertEqual(status.label, "授权失效")
        self.assertFalse(status.oauth_ready)

    def test_reconnect_required_or_missing_scope_requires_authorization(self):
        reconnect = self.make_service(
            {"GOOGLE_DRIVE_CONNECTION_STATUS": "Reconnect Required"}
        ).status()
        insufficient = self.make_service(missing_scopes=("drive.readonly",)).status()

        self.assertEqual(reconnect.code, "authorization_expired")
        self.assertEqual(insufficient.code, "authorization_expired")
        self.assertIn("权限", insufficient.detail)

    def test_latest_failed_connection_is_connection_error_with_bounded_code(self):
        status = self.make_service(
            event={
                "status": "failed",
                "created_at": "2026-08-09T22:01:00+10:00",
                "error_code": "drive_timeout",
                "error_message": "Bearer secret must never be returned",
            }
        ).status()

        self.assertEqual(status.code, "connection_error")
        self.assertEqual(status.label, "连接异常")
        self.assertIn("drive_timeout", status.detail)
        self.assertNotIn("Bearer secret", status.detail)

    def test_missing_client_or_folder_is_not_configured(self):
        missing_client = self.make_service({"GOOGLE_DRIVE_CLIENT_ID": ""}).status()
        missing_folder = self.make_service({"CONTENT_GOOGLE_DRIVE_VIDEO_FOLDER_ID": ""}).status()

        self.assertEqual(missing_client.code, "not_configured")
        self.assertEqual(missing_folder.code, "not_configured")
        self.assertEqual(missing_folder.label, "未配置")

    def test_expired_access_with_refresh_remains_connected_for_ephemeral_refresh(self):
        status = self.make_service(expired=True).status()

        self.assertEqual(status.code, "connected")
        self.assertEqual(status.label, "已连接")
        self.assertTrue(status.oauth_ready)
        self.assertIn("临时刷新", status.detail)

    def test_missing_access_with_refresh_remains_connected_for_ephemeral_refresh(self):
        status = self.make_service({"GOOGLE_DRIVE_ACCESS_TOKEN": ""}).status()

        self.assertEqual(status.code, "connected")
        self.assertTrue(status.oauth_ready)


if __name__ == "__main__":
    unittest.main()
