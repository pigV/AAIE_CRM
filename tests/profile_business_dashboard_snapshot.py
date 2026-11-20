"""Read-only diagnostic for the task-owned staging dashboard."""
import cProfile
import pstats
from pathlib import Path
import unittest
from tests import test_business_change_http as http_fixture


class DashboardProfile(unittest.TestCase):
    def test_profile(self):
        http_fixture.BusinessChangeHTTPTests.setUpClass()
        app=http_fixture.BusinessChangeHTTPTests.app
        handler=object.__new__(app.CRMHandler)
        handler.user=dict(app.db.one('SELECT u.*,r.name role_name,r.permissions_json FROM users u JOIN roles r ON r.id=u.role_id WHERE u.id=1'))
        handler.csrf='synthetic';handler.query={'surface':['dashboard_overview']}
        handler.json_response=lambda packet,*args:packet
        profile=cProfile.Profile()
        result=profile.runcall(handler.business_change_snapshot,'GET')
        self.assertIn('dashboard-overview',result['data']['fragments'])
        from staging_environment import ACTIVE
        root=ACTIVE.root/'reports'
        with (root/'dashboard-snapshot-profile-20261120.txt').open('w') as output:
            pstats.Stats(profile,stream=output).strip_dirs().sort_stats('cumulative').print_stats(45)
