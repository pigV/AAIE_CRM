import unittest
from unittest import mock

import record_picker_and_uploader


class UploadReviewConfirmationCoordinatorTests(unittest.TestCase):
    def coordinator(self):
        try:
            import upload_review_confirmation_coordinator as coordinator
        except ModuleNotFoundError as exc:
            self.fail(f"confirmation coordinator is missing: {exc}")
        return coordinator

    def confirmed(self, coordinator, business_record_id=None):
        return coordinator.ConfirmedUpload(
            module="Applications",
            document_id=19,
            business_record_id=business_record_id,
        )

    def call(self, coordinator, confirmed, stored_form=None):
        return coordinator.coordinate_confirmed_upload(
            "/tmp/isolated.db",
            stored_form=stored_form or {},
            confirmed_upload=confirmed,
            actor_user_id=7,
            actor_permissions={"student_cases.edit"},
            flags=object(),
        )

    def test_ordinary_application_is_noop_without_business_record(self):
        coordinator = self.coordinator()
        with mock.patch.object(
            coordinator.record_picker_and_uploader,
            "upload_context_from_form",
            return_value=None,
        ), mock.patch.object(
            coordinator.record_picker_and_uploader,
            "complete_upload_and_link",
        ) as link:
            result = self.call(coordinator, self.confirmed(coordinator))
        self.assertEqual(result.context_kind, "none")
        self.assertFalse(result.linked)
        link.assert_not_called()

    def test_progression_uses_document_id_without_business_record(self):
        coordinator = self.coordinator()
        context = record_picker_and_uploader.ProgressionUploadContext(
            case_id=3,
            document_role="application_material",
        )
        with mock.patch.object(
            coordinator.record_picker_and_uploader,
            "upload_context_from_form",
            return_value=context,
        ), mock.patch.object(
            coordinator.record_picker_and_uploader,
            "complete_upload_and_link",
        ) as link:
            result = self.call(coordinator, self.confirmed(coordinator))
        self.assertEqual(result.context_kind, "progression")
        self.assertTrue(result.linked)
        self.assertIsNone(link.call_args.kwargs["canonical_record_id"])
        self.assertEqual(link.call_args.kwargs["canonical_document_id"], 19)

    def test_transfer_requires_business_record_id(self):
        coordinator = self.coordinator()
        context = record_picker_and_uploader.TransferUploadContext(
            case_id=4,
            record_type="offer",
            application_id=8,
        )
        with mock.patch.object(
            coordinator.record_picker_and_uploader,
            "upload_context_from_form",
            return_value=context,
        ):
            with self.assertRaisesRegex(
                ValueError,
                "Transfer上传未生成可关联的正式业务记录",
            ):
                self.call(coordinator, self.confirmed(coordinator))

    def test_transfer_delegates_positive_business_record_id(self):
        coordinator = self.coordinator()
        context = record_picker_and_uploader.TransferUploadContext(
            case_id=4,
            record_type="offer",
            application_id=8,
        )
        with mock.patch.object(
            coordinator.record_picker_and_uploader,
            "upload_context_from_form",
            return_value=context,
        ), mock.patch.object(
            coordinator.record_picker_and_uploader,
            "complete_upload_and_link",
        ) as link:
            result = self.call(
                coordinator,
                self.confirmed(coordinator, business_record_id=27),
            )
        self.assertEqual(result.context_kind, "transfer")
        self.assertTrue(result.linked)
        self.assertEqual(link.call_args.kwargs["canonical_record_id"], 27)
        self.assertEqual(link.call_args.kwargs["canonical_document_id"], 19)

    def test_existing_linker_allows_none_record_for_progression(self):
        context = record_picker_and_uploader.ProgressionUploadContext(
            case_id=3,
            document_role="application_material",
        )
        service = mock.Mock()
        service.link_progression_document.return_value = "linked"
        with mock.patch.object(
            record_picker_and_uploader,
            "upload_context_from_form",
            return_value=context,
        ), mock.patch.object(
            record_picker_and_uploader,
            "ProgressionCaseService",
            return_value=service,
        ):
            result = record_picker_and_uploader.complete_upload_and_link(
                "/tmp/isolated.db",
                stored_form={"progression_link_after_review": "1"},
                canonical_record_id=None,
                canonical_document_id=19,
                actor_user_id=7,
                actor_permissions={"student_cases.edit"},
                flags=object(),
            )
        self.assertEqual(result, "linked")
        self.assertEqual(
            service.link_progression_document.call_args.kwargs[
                "student_document_id"
            ],
            19,
        )

    def test_existing_linker_rejects_invalid_record_ids_for_transfer(self):
        context = record_picker_and_uploader.TransferUploadContext(
            case_id=4,
            record_type="offer",
            application_id=8,
        )
        with mock.patch.object(
            record_picker_and_uploader,
            "upload_context_from_form",
            return_value=context,
        ), mock.patch.object(
            record_picker_and_uploader,
            "select_existing_record",
        ) as select_existing_record:
            for canonical_record_id in (None, 0, -1, True, "27"):
                with self.subTest(canonical_record_id=canonical_record_id):
                    with self.assertRaisesRegex(
                        ValueError,
                        "^Transfer上传缺少正式业务记录编号$",
                    ):
                        record_picker_and_uploader.complete_upload_and_link(
                            "/tmp/isolated.db",
                            stored_form={"transfer_link_after_review": "1"},
                            canonical_record_id=canonical_record_id,
                            canonical_document_id=19,
                            actor_user_id=7,
                            actor_permissions={"student_cases.edit"},
                            flags=object(),
                        )
        select_existing_record.assert_not_called()
