"""Finite CRM-owned API surface; authorization precedes all gateway work."""
import hmac
import json
import logging
from agent_reach_permissions import can_access_agent_reach
from agent_reach_gateway import FoundationGateway, GatewayError, validate_monitoring_request
from agent_reach_views import render_workspace

ROUTES = {
    ('GET', '/agent-reach/api/monitoring'): 'monitoring_read',
    ('POST', '/agent-reach/api/monitoring/keywords'): 'monitoring_keyword_create',
    ('POST', '/agent-reach/api/monitoring/keywords/update'): 'monitoring_keyword_update',
    ('POST', '/agent-reach/api/monitoring/keywords/delete'): 'monitoring_keyword_delete',
    ('POST', '/agent-reach/api/monitoring/keywords/run'): 'monitoring_keyword_run',
    ('GET', '/agent-reach/api/monitoring/trends'): 'monitoring_trends_read',
    ('POST', '/agent-reach/api/monitoring/trends'): 'monitoring_trends_update',
    ('POST', '/agent-reach/api/search'): 'search_submit',
    ('POST', '/agent-reach/api/trends'): 'trends_submit',
    ('GET', '/agent-reach/api/search/runs'): 'search_runs',
    ('GET', '/agent-reach/api/search/results'): 'search_results',
    ('GET', '/agent-reach/api/trends/runs'): 'trends_runs',
    ('GET', '/agent-reach/api/trends/results'): 'trends_results',
}


def handle_agent_reach(handler, method, render_shell, gateway_factory=FoundationGateway):
    def reject(message, status):
        return handler.json_response({'error': message}, status)
    if not handler.user:
        return reject('请先登录。', 401)
    if not can_access_agent_reach(handler.user):
        return reject('无权使用此功能，请联系管理员。', 403)
    path = handler.parsed.path
    if handler.parsed.query:
        return reject('请求参数无效。', 400)
    if path == '/agent-reach' and method == 'GET':
        return render_shell(render_workspace(handler.csrf))
    operation = ROUTES.get((method, path))
    if operation is None:
        return reject('此功能暂未开放。', 404)
    payload = None
    if method == 'POST':
        expected = str(handler.csrf or '')
        supplied = handler.headers.get('X-CSRF-Token', '')
        if not expected or not hmac.compare_digest(expected.encode(), supplied.encode()):
            return reject('安全验证已过期，请刷新页面。', 403)
        if handler.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json' or handler.headers.get('Transfer-Encoding'):
            return reject('请求格式无效。', 400)
        try:
            length = int(handler.headers.get('Content-Length', '0'))
            if not 0 < length <= 4096:
                raise ValueError()
            payload = json.loads(handler.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError()
            if operation.startswith('monitoring_'):
                validate_monitoring_request(operation, payload)
            elif operation == 'search_submit':
                if set(payload) != {'keyword'} or not isinstance(payload['keyword'], str) or not 1 <= len(payload['keyword'].strip()) <= 100:
                    raise ValueError()
            elif payload:
                raise ValueError()
        except (ValueError, UnicodeError, GatewayError):
            return reject('请求参数无效。', 400)
    try:
        result = gateway_factory().request(operation, payload)
        return handler.json_response(result)
    except GatewayError as exc:
        logging.getLogger(__name__).warning('agent_reach_gateway_failure operation=%s', operation)
        return reject(exc.message, exc.status)
    except Exception:
        logging.getLogger(__name__).error('agent_reach_gateway_failure category=unexpected')
        return reject('服务暂时不可用，请联系管理员。', 503)
