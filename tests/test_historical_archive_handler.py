"""Handler must fail closed when a confirmed archive no longer validates."""
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch
import app


class HistoricalArchiveHandlerTests(unittest.TestCase):
    def test_invalid_registered_final_archive_never_falls_back_to_draft(self):
        handler=object.__new__(app.CRMHandler)
        records=iter([{'id':2},{'metadata_json':'{"confirmation_id":2,"final_document_id":999}'},{'student_id':'S'}])
        database=SimpleNamespace(one=lambda *args:next(records))
        with patch.object(app,'db',database), \
             patch('business_changes.snapshots.read_scope',return_value=nullcontext(None)), \
             patch('historical_archive_access.school_archive',return_value=None):
            with self.assertRaisesRegex(ValueError,'归档记录不可读取'):
                handler.school_plan_confirmed_archive_target({'id':1,'student_id':'S'})

    def test_report_without_confirmation_can_still_use_original_draft(self):
        handler=object.__new__(app.CRMHandler)
        with patch.object(app,'db',SimpleNamespace(one=lambda *args:None)):
            self.assertIsNone(handler.school_plan_confirmed_archive_target({'id':1,'student_id':'S'}))
