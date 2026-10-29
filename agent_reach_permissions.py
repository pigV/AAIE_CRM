"""Independent CRM capability permission; no content-center inheritance."""
import json


def can_access_agent_reach(user):
    if not user:
        return False
    try:
        if user.get('role_name') == 'Admin':
            return True
        grants = json.loads(user.get('module_permissions_json') or '[]')
        return isinstance(grants, list) and all(isinstance(x, str) for x in grants) and 'agent_reach' in grants
    except (AttributeError, TypeError, ValueError):
        return False
