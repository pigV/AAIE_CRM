import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from tests import test_system_business_new_student_journey as journey


class BusyReadScenarioTests(unittest.TestCase):
    def test_only_explicit_busy_is_retried_before_comparing_business_facts(self):
        helper=journey.NewStudentGrantJourney()
        answers=[(503,{},b'{"code":"snapshot_busy"}'),(503,{},b'{"code":"snapshot_busy"}'),(200,{},b'{"data":{"count":3}}')]
        helper.http=SimpleNamespace(request_static=lambda path:answers.pop(0))
        with patch.object(journey.time,'sleep'):
            self.assertEqual({'data':{'count':3}},helper.packet('dashboard_overview'))
        self.assertEqual([],answers)

    def test_schema_errors_are_not_hidden_as_busy_retries(self):
        helper=journey.NewStudentGrantJourney()
        helper.http=SimpleNamespace(request_static=lambda path:(503,{},b'{"code":"sync_schema_unavailable"}'))
        with self.assertRaises(AssertionError):helper.packet('dashboard_overview')
