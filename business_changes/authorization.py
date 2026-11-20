"""Load staff identity and module access from the caller's current snapshot."""
import json


def current_staff(connection,user_id):
    cursor=connection.execute('''SELECT u.*,r.name role_name,r.permissions_json FROM users u
        JOIN roles r ON r.id=u.role_id WHERE u.id=? AND u.is_active=1 AND u.deleted_at IS NULL''',(user_id,))
    row=cursor.fetchone()
    if row is None:raise PermissionError('员工账号已停用或已删除，请重新登录。')
    actor=dict(zip((column[0] for column in cursor.description),row))
    actor['module_permissions_json']=json.dumps([row[0] for row in connection.execute(
        'SELECT module_key FROM user_module_permissions WHERE user_id=? AND can_access=1 ORDER BY module_key',(user_id,))],ensure_ascii=False)
    return actor
