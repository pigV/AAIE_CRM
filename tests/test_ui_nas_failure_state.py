import unittest

from ui import page


USER = {
    "display_name": "Template Manager",
    "role_name": "Manager",
    "username": "manager",
    "module_permissions_json": "[]",
}


class NasFailureUiTests(unittest.TestCase):
    def render(self, error_code):
        return page(
            "Dashboard",
            "<p>body</p>",
            user=USER,
            nas={
                "mode": "test",
                "ready": False,
                "access_error_code": error_code,
            },
        )

    def test_mount_loss_keeps_not_connected_copy_and_file_lock(self):
        html = self.render("mount_unavailable")

        self.assertIn("NAS 未连接", html)
        self.assertIn("文件操作已锁定", html)
        self.assertNotIn("NAS 访问待恢复", html)

    def test_process_local_access_failure_uses_recovery_required_copy(self):
        html = self.render("read_denied")

        self.assertIn("NAS 访问待恢复", html)
        self.assertIn("NAS 已挂载，但当前服务需要恢复访问", html)
        self.assertIn("文件操作已锁定", html)
        self.assertNotIn("学生文件存储未连接", html)

    def test_write_denied_is_also_access_failure_not_mount_loss(self):
        html = self.render("write_denied")

        self.assertIn("NAS 访问待恢复", html)
        self.assertNotIn("NAS 未连接", html)


if __name__ == "__main__":
    unittest.main()
