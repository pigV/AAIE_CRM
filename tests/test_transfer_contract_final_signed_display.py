import unittest
from unittest import mock

import app as app_module


class _CompletionDatabase:
    def __init__(self, contract):
        self.contract = contract
        self.connection = object()
        self.released = []

    def one(self, _sql, _params=()):
        return self.contract

    def connect(self):
        return self.connection

    def _release_connection(self, connection):
        self.released.append(connection)


class TransferContractFinalSignedDisplayTests(unittest.TestCase):
    def completion_contract(self, *, contract_type, application_cycle_id):
        return {
            "id": 199,
            "student_id": "S-TRANSFER",
            "contract_uid": "CONTRACT-199",
            "contract_type": contract_type,
            "application_cycle_id": application_cycle_id,
        }

    def invoke_completion(self, contract):
        completion_db = _CompletionDatabase(contract)
        event = {"id": 501, "event_type": "contract.completed"}
        with (
            mock.patch.object(app_module, "db", completion_db),
            mock.patch.object(
                app_module,
                "emit_contract_business_event",
                return_value=event,
            ),
            mock.patch.object(app_module, "is_workflow_v2_active", return_value=True),
            mock.patch.object(app_module, "process_revenue_contract_completion") as revenue,
        ):
            result = app_module.record_contract_completion_fact(
                contract["id"],
                "2026-09-05T12:00:00+10:00",
                1,
                {"signature_id": 103},
            )
        return result, revenue, completion_db

    def test_pre_cycle_transfer_completion_does_not_invoke_revenue_observer(self):
        contract = self.completion_contract(
            contract_type="TransferApplicationContract",
            application_cycle_id=None,
        )

        result, revenue, completion_db = self.invoke_completion(contract)

        self.assertEqual("contract.completed", result["event_type"])
        revenue.assert_not_called()
        self.assertEqual([], completion_db.released)

    def test_transfer_completion_with_cycle_still_invokes_revenue_observer(self):
        contract = self.completion_contract(
            contract_type="TransferApplicationContract",
            application_cycle_id=149,
        )

        _result, revenue, completion_db = self.invoke_completion(contract)

        revenue.assert_called_once_with(
            completion_db.connection,
            contract["id"],
            1,
            "2026-09-05T12:00:00+10:00",
        )
        self.assertEqual([completion_db.connection], completion_db.released)

    def test_ordinary_contract_without_cycle_still_invokes_revenue_observer(self):
        contract = self.completion_contract(
            contract_type="NewInternationalStudentContract",
            application_cycle_id=None,
        )

        _result, revenue, completion_db = self.invoke_completion(contract)

        revenue.assert_called_once_with(
            completion_db.connection,
            contract["id"],
            1,
            "2026-09-05T12:00:00+10:00",
        )
        self.assertEqual([completion_db.connection], completion_db.released)


if __name__ == "__main__":
    unittest.main()
