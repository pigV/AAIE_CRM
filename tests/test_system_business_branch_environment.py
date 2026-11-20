"""Explicitly authorized permissions, confined to this task's synthetic DB."""
import json
from pathlib import Path
import unittest


class AuthorizedBranchEnvironment(unittest.TestCase):
    def test_enable_only_task_staging_branch_acceptance(self):
        from staging_environment import ACTIVE
        from tests import test_business_change_http as fixture
        import student_case_permissions as permissions
        from database import TRANSFER_COMPLETE_REQUIRED_PERMISSIONS,COMPLETION_ADMIN_STUDENT_CASE_PERMISSIONS,reconciled_role_permissions
        from student_case_feature_flags import StudentCaseFeatureFlags
        self.assertIsNotNone(ACTIVE)
        expected=Path('/Volumes/Data/AAIE_CRM/worktrees/system-business-sync-20261119/STAGING_DATA')
        self.assertEqual(expected,ACTIVE.root)
        import app
        self.assertEqual((expected/'database/staging.db').resolve(),app.db.path.resolve())
        keys=('STUDENT_CASES_READ_ENABLED','TRANSFER_CASES_WRITE_ENABLED',
              'PROGRESSION_CASES_WRITE_ENABLED','CASE_COMPLETION_ENABLED')
        path=expected/'reports/authorized-branch-test-settings-20261120.json'
        with app.db.transaction() as c:
            user=c.execute("SELECT id,role_id FROM users WHERE username='admin' AND is_active=1 AND deleted_at IS NULL").fetchone()
            self.assertEqual(1,user['id'])
            # Only this synthetic account receives the test-only role.
            self.assertEqual(1,c.execute('SELECT count(*) FROM users WHERE role_id=?',(user['role_id'],)).fetchone()[0])
            role=c.execute('SELECT permissions_json FROM roles WHERE id=?',(user['role_id'],)).fetchone()
            before={'role_id':user['role_id'],'permissions_json':role[0],
                'settings':{k:(lambda row:row[0] if row else None)(c.execute('SELECT value FROM system_settings WHERE key=?',(k,)).fetchone()) for k in keys}}
            if not path.exists():path.write_text(json.dumps(before,ensure_ascii=False,indent=2))
            original=json.loads(path.read_text())
            c.execute('UPDATE roles SET permissions_json=? WHERE id=?',
                (original['permissions_json'],original['role_id']))
            extra=set(TRANSFER_COMPLETE_REQUIRED_PERMISSIONS)|set(COMPLETION_ADMIN_STUDENT_CASE_PERMISSIONS)|set(permissions.PROGRESSION_CASE_PERMISSIONS)
            new=sorted(set(json.loads(original['permissions_json']))|extra)
            role_name='Manager + Student Cases'
            reconciled_role_permissions(role_name,json.dumps(new),[],feature_flags=StudentCaseFeatureFlags(
                read_enabled=True,transfer_write_enabled=True,progression_write_enabled=True,completion_enabled=True))
            # Preserve the earlier unused synthetic role as empty history.
            c.execute("UPDATE roles SET permissions_json='[]' WHERE name='SYNTHETIC Branch Acceptance Only'")
            c.execute('INSERT INTO roles(name,permissions_json) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET permissions_json=excluded.permissions_json',
                (role_name,json.dumps(new)))
            test_role=c.execute('SELECT id FROM roles WHERE name=?',(role_name,)).fetchone()[0]
            c.execute('UPDATE users SET role_id=? WHERE id=1',(test_role,))
            for key in keys:
                c.execute('INSERT INTO system_settings(key,value,updated_at,updated_by) VALUES(?,?,?,1) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at,updated_by=excluded.updated_by',
                    (key,'true',app.now_iso()))
            self.assertEqual('disabled',c.execute("SELECT COALESCE((SELECT value FROM system_settings WHERE key='EXTERNAL_TRANSFER_AUTO_CREATE_ENABLED'),'disabled')").fetchone()[0])
        print('AUTHORIZED isolated flags enabled; sole synthetic account assigned a dedicated test role; built-in Admin restored. Prior state:',path,flush=True)
