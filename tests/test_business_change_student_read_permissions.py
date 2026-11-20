import unittest

import app
from tests import test_business_change_case_snapshots as case_fixture


class StudentSnapshotPermissionsTests(unittest.TestCase):
    setUp=case_fixture.VisaCaseSnapshotTests.setUp

    def test_student_snapshot_requires_enabled_student_module_not_only_role_permission(self):
        role=self.db.one("SELECT id FROM roles WHERE name='View Only'")[0]
        uid=self.db.execute("""INSERT INTO users(username,display_name,password_hash,role_id,is_active,created_at)
            VALUES('synthetic-no-student-module','SYNTHETIC ONLY','unused',?,1,?)""",(role,self.f.stamp))
        app.save_user_module_permissions(uid,['visa'],1,role_name='View Only')
        self.h.user=dict(self.db.one('''SELECT u.*,r.name role_name,r.permissions_json FROM users u
            JOIN roles r ON r.id=u.role_id WHERE u.id=?''',(uid,)))
        self.h.user['module_permissions_json']=app.modules_json_for_user(uid)
        self.assertTrue(app.allowed(self.h.user,'students.view'))
        self.assertFalse(app.can_access_module(self.h.user,'students'))
        self.h.query={'surface':['student_summary'],'student_id':[self.f.sid]}
        status,packet=self.h.business_change_snapshot('GET')
        self.assertEqual(403,status)
        self.assertNotIn('data',packet)
