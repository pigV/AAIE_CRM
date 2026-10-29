"""Transfer fee authority must remain explicit before and after activation."""
from tests.test_transfer_source_authority import SourceAuthorityTests

class TransferFeeAuthorityTests(SourceAuthorityTests):
    def setUp(self):
        super().setUp()
        self.assertTrue(callable(getattr(self.svc, "resolve_for_contract", None)), "Contract source authority resolver required")
        self.assertTrue(callable(getattr(self.svc, "resolve_for_cycle", None)), "Cycle source authority resolver required")
    def test_contract_fee_requires_explicit_source(self):
        with self.assertRaises(self.svc.SourceAuthorityError):
            self.svc.resolve_for_contract(self.db, 'new', 100)
        self.save()
        self.assertEqual(self.svc.resolve_for_contract(self.db, 'new', 100)['source_classification'], 'new_transfer')

    def test_existing_pending_contract_resolves_without_generic_revenue_existing(self):
        self.existing()
        self.assertEqual(self.svc.resolve_for_contract(self.db, 'existing', 200)['source_classification'], 'aaie_existing')

    def test_consumed_cycle_retains_original_source(self):
        self.save(); self.bind()
        row = self.svc.load_for_contract(self.db, 'new', 100)
        self.svc.consume_authority(self.db, row['id'], 99, actor_user_id=1, occurred_at='2026-09-07')
        self.assertEqual(self.svc.resolve_for_cycle(self.db, 'new', 99)['source_classification'], 'new_transfer')
        self.assertEqual(self.svc.resolve_for_contract(self.db, 'new', 100)['source_classification'], 'new_transfer')
        with self.assertRaises(self.svc.SourceAuthorityError):
            self.svc.resolve_for_cycle(self.db, 'existing', 99)

    def test_unbound_or_missing_cycle_never_defaults(self):
        self.save()
        with self.assertRaises(self.svc.SourceAuthorityError):
            self.svc.resolve_for_cycle(self.db, 'new', 99)
