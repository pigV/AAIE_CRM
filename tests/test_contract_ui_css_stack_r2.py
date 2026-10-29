import unittest

from contract_template_editor import render_template_editor_index


class ContractTemplateEditorCssStackR2Tests(unittest.TestCase):
    def test_existing_dynamic_version_card_supports_future_published_version(self):
        html = render_template_editor_index(
            [
                {
                    "template_type": "NewInternationalStudentContract",
                    "display_name": "新留学生合同",
                    "current_published_version_id": 71,
                    "current_published_version_number": 7,
                }
            ],
            csrf="token",
            can_edit=True,
            can_publish=True,
            is_admin=True,
        )

        self.assertIn("V7 · #71", html)
        self.assertNotIn("V1 · #71", html)


if __name__ == "__main__":
    unittest.main()
