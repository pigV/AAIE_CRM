import json
from pathlib import Path
import unittest
from unittest.mock import patch

from revenue.school_commission_pilot import PilotPolicy, read_pilot_policy


class SchoolCommissionPilotTests(unittest.TestCase):
    def setUp(self):
        import os
        from tempfile import TemporaryDirectory
        fixture = TemporaryDirectory(dir=os.environ["REVENUE_POSIX_TEST_ROOT"])
        self.addCleanup(fixture.cleanup)
        self.root = Path(fixture.name).resolve()
        self.config = self.root / "config"
        self.config.mkdir()
        self.path = self.config / "school-commission-pilot.json"
        self.data = dict(mode="rehearsal", enabled=True, stage="contracts", school_id=7,
                         entity_id=8, admin_ids=[1,2])
        self.admin = {"id":1,"role_name":"Admin"}

    def read(self, mode="rehearsal"):
        return read_pilot_policy(self.root,mode)

    def test_missing_invalid_or_other_mode_is_closed(self):
        self.assertFalse(self.read().allows(self.admin,"contracts"))
        for changes in ({"enabled":"true"},{"stage":"unknown"},{"school_id":True},{"admin_ids":[1,1]},
                        {"mode":"production"},{"extra":1}):
            self.path.write_text(json.dumps({**self.data,**changes}))
            self.assertEqual(PilotPolicy(),self.read())
        self.path.write_text(json.dumps(self.data))
        self.assertEqual(PilotPolicy(simulation=False),self.read("production"))

    def test_stages_and_read_after_shutdown(self):
        for count,stage in enumerate(("contracts","invoicing","settlement"),1):
            self.path.write_text(json.dumps({**self.data,"stage":stage}))
            policy = self.read()
            self.assertEqual([True]*count+[False]*(3-count),
                             [policy.allows(self.admin, name) for name in ("contracts","invoicing","settlement")])
            self.assertFalse(policy.allows({"id":9,"role_name":"Admin"},"read"))
            self.assertFalse(policy.allows({"id":1,"role_name":"Staff"},"read"))
        self.path.write_text(json.dumps({**self.data,"enabled":False}))
        policy = self.read()
        self.assertTrue(policy.allows(self.admin,"read"))
        self.assertFalse(policy.allows(self.admin,"contracts"))

    def test_rehearsal_override_cannot_escape_config_root(self):
        outside = self.root / "outside.json"
        outside.write_text(json.dumps(self.data))
        self.assertEqual(PilotPolicy(),read_pilot_policy(self.root,"rehearsal",outside))
        self.path.write_text(json.dumps(self.data))
        resolve = Path.resolve

        def follow_alias(path, **kwargs):
            return outside if path==self.path else resolve(path,**kwargs)

        # The managed sandbox blocks creating actual symlinks during the full suite.
        with patch.object(Path,"resolve",follow_alias):
            self.assertEqual(PilotPolicy(),self.read())

    def test_self_service_is_explicit_and_does_not_extend_global_stage(self):
        from dataclasses import replace
        self.path.write_text(json.dumps({**self.data,"self_service":True}))
        policy = self.read()
        self.assertTrue(policy.self_service)
        self.assertTrue(policy.allows(self.admin,"setup"))
        self.assertFalse(policy.allows(self.admin,"invoicing"))
        self.assertFalse(replace(policy,enabled=False).allows(self.admin,"setup"))
