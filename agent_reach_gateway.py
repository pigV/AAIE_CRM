"""Finite CRM-facing Foundation gateway; no generic proxy or credential forwarding.

Only the explicitly named operations are reachable. The backend address and paths are
code-owned; each request uses one fresh connection with no retry or redirect.
CRM authentication/authorization and CSRF are enforced by the calling handlers.
"""
from __future__ import annotations

from datetime import datetime
import http.client
import json
import math
import os
from pathlib import Path
import re
from typing import Any

_HOST = '127.0.0.1'
_PORT = 8790
_TIMEOUT_SECONDS = 95
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_ITEMS = 1000
_MAX_COUNT = 1_000_000_000
_OPERATIONS = {
    'search_submit': ('POST', '/api/runs'),
    'trends_submit': ('POST', '/api/trends/refresh'),
    'search_runs': ('GET', '/api/runs'),
    'search_results': ('GET', '/api/results'),
    'trends_runs': ('GET', '/api/trend-runs'),
    'trends_results': ('GET', '/api/trends'),
}
_STATUS_LABELS = {
    'pending': '等待执行', 'running': '执行中', 'completed': '已完成',
    'partial': '部分完成', 'failed': '未完成',
}
_PLATFORM_LABELS = {'weibo': '微博', 'zhihu': '知乎'}
_ERRORS = {
    'invalid_operation': (400, '此操作暂不可用。'),
    'invalid_request': (400, '请输入有效内容。'),
    'backend_busy': (409, '已有任务正在执行，请查看运行记录。'),
    'backend_unavailable': (503, '服务暂时不可用。'),
    'backend_failed': (502, '本次操作未完成，请查看运行记录。'),
    'backend_response_invalid': (502, '暂时无法读取结果。'),
    'request_uncertain': (502, '本次操作结果尚未确认，请先查看运行记录，不要重复提交。'),
}
_PRIVATE_TEXT = re.compile(
    r'(?:/Users/|/Volumes/|/private/|/tmp/|SAFEBOX|'
    r'\b(?:cookie|authorization|token|xsec(?:_token)?|session[_ -]?id|csrf|'
    r'api[_ -]?key|credential|secret)\s*[:=]|\bBearer\s+\S+|'
    r'\b(?:127\.0\.0\.1|localhost)\b|[a-zA-Z][a-zA-Z0-9+.-]*://)',
    re.IGNORECASE,
)
_ID = re.compile(r'^[A-Za-z0-9_.:-]{1,160}$')
_RUN_ID = re.compile(r'^[A-Za-z0-9_-]{1,80}$')


class GatewayError(RuntimeError):
    """Closed, user-safe error contract: no backend text or exception details."""
    def __init__(self, code: str):
        self.code = code if code in _ERRORS else 'backend_response_invalid'
        self.status, self.message = _ERRORS[self.code]
        super().__init__(self.message)


def backend_port():
    """A managed staging request must never use the legacy production backend."""
    path = os.environ.get('AAIE_CRM_RUNTIME_CONFIG')
    if path:
        from runtime_configuration import RuntimeConfiguration, RuntimeConfigurationError
        try:
            config = RuntimeConfiguration.load(path, source_root=Path(__file__).resolve().parent)
            if os.environ.get('AAIE_CRM_ENV') != config.mode:
                raise GatewayError('backend_unavailable')
            return config.agent_reach_port
        except (RuntimeConfigurationError, OSError, ValueError) as exc:
            raise GatewayError('backend_unavailable') from exc
    if os.environ.get('AAIE_CRM_ENV') == 'staging':
        raise GatewayError('backend_unavailable')
    return _PORT


def _bad_response() -> None:
    raise GatewayError('backend_response_invalid')


def _text(value: Any, *, maximum: int, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or len(value) > maximum:
        _bad_response()
    if any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in value) or _PRIVATE_TEXT.search(value):
        _bad_response()
    return value


def _count(value: Any, *, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    if type(value) is not int or not 0 <= value <= _MAX_COUNT:
        _bad_response()
    return value


def _time(value: Any) -> str | None:
    if value is None or value == '':
        return None
    value = _text(value, maximum=48)
    valid = True
    try:
        datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        valid = False
    if not valid:
        _bad_response()
    return value


def _publication_display(value: Any) -> str | None:
    """Display the source publication scalar without inventing a time format."""
    if value is None or value == '':
        return None
    if type(value) in (int, float):
        if not math.isfinite(value) or abs(value) > 1_000_000_000_000_000:
            _bad_response()
        return str(value)
    return _text(value, maximum=96)


def _native_id(value: Any) -> str | None:
    if value is None or value == '':
        return None
    if not isinstance(value, str) or not _ID.fullmatch(value) or _PRIVATE_TEXT.search(value):
        _bad_response()
    return value


def _platforms(value: Any) -> list[str]:
    if not isinstance(value, list) or len(value) > 2:
        _bad_response()
    if any(not isinstance(item, str) or item not in _PLATFORM_LABELS for item in value):
        _bad_response()
    if len(set(value)) != len(value):
        _bad_response()
    return list(value)


def _run(item: dict, *, trends: bool) -> dict:
    if item.get('source') != ('trendradar' if trends else 'opencli'):
        _bad_response()
    run_id = item.get('run_id')
    status = item.get('status')
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        _bad_response()
    if not isinstance(status, str) or status not in _STATUS_LABELS or (not trends and status == 'partial'):
        _bad_response()
    result = {
        'run_id': run_id,
        'status': status,
        'status_label': _STATUS_LABELS[status],
        'started_at': _time(item.get('started_at')),
        'finished_at': _time(item.get('finished_at')),
        'raw_count': _count(item.get('raw_count')),
        'cleaned_count': _count(item.get('cleaned_count')),
        'unique_count': _count(item.get('unique_count')),
    }
    if trends:
        for name in ('requested_platforms', 'successful_platforms', 'failed_platforms'):
            result[name] = _platforms(item.get(name))
        requested = set(result['requested_platforms'])
        successful = set(result['successful_platforms'])
        failed = set(result['failed_platforms'])
        if not requested or successful & failed or successful | failed != requested:
            _bad_response()
    else:
        result['keyword'] = _text(item.get('keyword'), maximum=120)
    return result


def _search_rank(value):
    rank = _count(value, optional=True)
    if rank == 0:
        _bad_response()
    return rank


def _search_keywords(value):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > _MAX_ITEMS:
        _bad_response()
    return [_text(keyword, maximum=120) for keyword in value]


def _search_rank_history(value):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > _MAX_ITEMS or any(not isinstance(row, dict) for row in value):
        _bad_response()
    return [{'keyword': _text(row.get('keyword'), maximum=120),
             'search_rank': _search_rank(row.get('search_rank')),
             'run_started_at': _time(row.get('run_started_at')),
             'run_id': _native_id(row.get('run_id'))} for row in value]


def _search_result(item: dict) -> dict:
    if item.get('source') != 'opencli':
        _bad_response()
    return {
        'title': _text(item.get('title'), maximum=2000, optional=True),
        'author': _text(item.get('author'), maximum=300, optional=True),
        'likes': _count(item.get('likes'), optional=True),
        'published_at': _publication_display(item.get('published_at')),
        'note_id': _native_id(item.get('note_id')),
        'source': 'opencli',
        'source_label': '小红书',
        'stable_identity': _native_id(item.get('stable_identity')),
        'keywords': _search_keywords(item.get('keywords')),
        'last_seen': _time(item.get('last_seen')),
        'latest_search_rank': _search_rank(item.get('latest_search_rank')),
        'latest_search_run_at': _time(item.get('latest_search_run_at')),
        'latest_search_run_id': _native_id(item.get('latest_search_run_id')),
        'keyword_search_ranks': _search_rank_history(item.get('keyword_search_ranks')),
    }


def _trend_result(item: dict) -> dict:
    platform = item.get('platform')
    if item.get('source') != 'trendradar' or not isinstance(platform, str) or platform not in _PLATFORM_LABELS:
        _bad_response()
    return {
        'title': _text(item.get('title'), maximum=2000),
        'platform': platform,
        'platform_label': _PLATFORM_LABELS[platform],
        'latest_rank': _count(item.get('latest_rank'), optional=True),
        'occurrence_count': _count(item.get('occurrence_count')),
        'first_seen': _time(item.get('first_seen')),
        'last_seen': _time(item.get('last_seen')),
    }


def _project(operation: str, payload: Any) -> dict:
    if not isinstance(payload, dict):
        _bad_response()
    if operation.startswith('monitoring_'):
        return _project_monitoring(operation, payload)
    if operation.endswith('_submit'):
        result = _run(payload, trends=operation == 'trends_submit')
        if result['status'] not in ('completed', 'partial'):
            _bad_response()
        return result
    items = payload.get('items')
    if not isinstance(items, list) or len(items) > _MAX_ITEMS or any(not isinstance(item, dict) for item in items):
        _bad_response()
    if operation == 'search_runs':
        projected = [_run(item, trends=False) for item in items]
    elif operation == 'trends_runs':
        projected = [_run(item, trends=True) for item in items]
    elif operation == 'search_results':
        if len(items) > 100:
            _bad_response()
        projected = [_search_result(item) for item in items]
    else:
        projected = [_trend_result(item) for item in items]
    result = {'items': projected}
    if operation == 'search_results':
        metadata = {'total_count', 'returned_count', 'limit', 'has_more', 'scope'}
        if metadata & payload.keys():
            if not metadata <= payload.keys():
                _bad_response()
            total = _count(payload['total_count'])
            returned = _count(payload['returned_count'])
            if type(payload['limit']) is not int or payload['limit'] != 100 or returned != len(projected) or total < returned:
                _bad_response()
            if type(payload['has_more']) is not bool or payload['has_more'] != (total > returned) or payload['scope'] != 'latest_saved_results':
                _bad_response()
            result.update(total_count=total, returned_count=returned, limit=100, has_more=payload['has_more'], scope='latest_saved_results')
    return result


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def _reject_constant(_value: str):
    raise ValueError()


def _request_body(operation: str, payload: Any) -> bytes | None:
    if operation.startswith('monitoring_'):
        value = validate_monitoring_request(operation, payload)
        return None if value is None else json.dumps(value, ensure_ascii=False).encode('utf-8')
    if operation == 'search_submit':
        if not isinstance(payload, dict) or set(payload) != {'keyword'} or not isinstance(payload['keyword'], str):
            raise GatewayError('invalid_request')
        keyword = payload['keyword'].strip()
        if not 1 <= len(keyword) <= 120 or keyword.startswith('//') or _PRIVATE_TEXT.search(keyword):
            raise GatewayError('invalid_request')
        if any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in keyword):
            raise GatewayError('invalid_request')
        return json.dumps({'keyword': keyword}, ensure_ascii=False).encode('utf-8')
    if operation == 'trends_submit':
        if payload not in (None, {}) and payload != {'platforms': ['weibo', 'zhihu']}:
            raise GatewayError('invalid_request')
        return json.dumps({'platforms': ['weibo', 'zhihu']}).encode('utf-8')
    if payload not in (None, {}):
        raise GatewayError('invalid_request')
    return None


_MONITOR_OPERATIONS = {
    'monitoring_read': ('GET', '/api/monitoring'),
    'monitoring_keyword_create': ('POST', '/api/monitoring/keywords'),
    'monitoring_keyword_update': ('POST', '/api/monitoring/keywords/update'),
    'monitoring_keyword_delete': ('POST', '/api/monitoring/keywords/delete'),
    'monitoring_keyword_run': ('POST', '/api/monitoring/keywords/run'),
    'monitoring_trends_read': ('GET', '/api/monitoring/trends'),
    'monitoring_trends_update': ('POST', '/api/monitoring/trends'),
}
_OPERATIONS.update(_MONITOR_OPERATIONS)
_FREQUENCIES = {'daily', 'weekdays', 'weekly', 'interval_days', 'custom_weekdays'}
_SCHEDULE_KEYS = {'frequency', 'time', 'weekdays', 'interval_days', 'anchor_date', 'timezone'}
_CONTENT_RANGES = {'7', '30', '90', '180', '365', 'all'}
_TASK_STATUSES = {'pending', 'running', 'completed', 'partial', 'failed', 'skipped', 'cancelled', 'uncertain'}


def _monitor_schedule(value):
    if not isinstance(value, dict) or set(value) != _SCHEDULE_KEYS:
        raise ValueError()
    frequency = value['frequency']
    if not isinstance(frequency, str) or frequency not in _FREQUENCIES or value['timezone'] != 'Australia/Melbourne':
        raise ValueError()
    if not isinstance(value['time'], str) or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', value['time']):
        raise ValueError()
    days = value['weekdays']
    if not isinstance(days, list) or len(days) > 7 or any(type(day) is not int or not 1 <= day <= 7 for day in days) or len(set(days)) != len(days):
        raise ValueError()
    if frequency == 'weekly' and len(days) != 1 or frequency == 'custom_weekdays' and not days:
        raise ValueError()
    if frequency not in {'weekly', 'custom_weekdays'} and days:
        raise ValueError()
    interval, anchor = value['interval_days'], value['anchor_date']
    if frequency == 'interval_days':
        if type(interval) is not int or not 1 <= interval <= 365 or not isinstance(anchor, str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', anchor):
            raise ValueError()
        from datetime import date
        date.fromisoformat(anchor)
    elif interval is not None or anchor is not None:
        raise ValueError()
    return {key: value[key] for key in ('frequency', 'time', 'weekdays', 'interval_days', 'anchor_date', 'timezone')}


def _monitor_id(value):
    if type(value) is not int or not 1 <= value <= _MAX_COUNT:
        raise ValueError()
    return value


def validate_monitoring_request(operation, payload):
    """Both route and transport boundaries reject extra fields before any send."""
    try:
        if operation not in _MONITOR_OPERATIONS:
            raise ValueError()
        if operation in {'monitoring_read', 'monitoring_trends_read'}:
            if payload not in (None, {}):
                raise ValueError()
            return None
        if not isinstance(payload, dict):
            raise ValueError()
        if operation in {'monitoring_keyword_delete', 'monitoring_keyword_run'}:
            if set(payload) != {'id'}:
                raise ValueError()
            return {'id': _monitor_id(payload['id'])}
        expected = {'enabled', 'auto_enabled', 'schedule'}
        if operation != 'monitoring_trends_update':
            expected.add('keyword')
        if operation == 'monitoring_keyword_update':
            expected.add('id')
        if operation != 'monitoring_trends_update' and 'content_range' in payload:
            expected.add('content_range')
        if set(payload) != expected or type(payload['enabled']) is not bool or type(payload['auto_enabled']) is not bool:
            raise ValueError()
        schedule = _monitor_schedule(payload['schedule']) if payload['schedule'] is not None else None
        if payload['auto_enabled'] and schedule is None:
            raise ValueError()
        if operation == 'monitoring_trends_update' and schedule is not None and schedule['frequency'] not in {'daily','weekdays','weekly'}:
            raise ValueError()
        result = {'enabled': payload['enabled'], 'auto_enabled': payload['auto_enabled'], 'schedule': schedule}
        if 'keyword' in expected:
            keyword = payload['keyword']
            if not isinstance(keyword, str) or not 1 <= len(keyword.strip()) <= 120 or keyword.startswith('//'):
                raise ValueError()
            result['keyword'] = _text(keyword.strip(), maximum=120)
        if 'content_range' in expected:
            value = payload['content_range']
            if not isinstance(value, str) or value not in _CONTENT_RANGES:
                raise ValueError()
            result['content_range'] = value
        if 'id' in expected:
            result['id'] = _monitor_id(payload['id'])
        return result
    except (ValueError, TypeError, GatewayError):
        raise GatewayError('invalid_request') from None


def _monitor_time(value):
    result = _time(value)
    if result is not None and datetime.fromisoformat(result.replace('Z', '+00:00')).tzinfo is None:
        _bad_response()
    return result


def _execution_phase(value):
    phase = value.get('execution_phase')
    if phase is not None and (not isinstance(phase, str) or phase not in {'preparing', 'running'}):
        _bad_response()
    return phase


def _monitor_plan(value, *, trends=False):
    if not isinstance(value, dict):
        _bad_response()
    try:
        schedule = _monitor_schedule(value['schedule']) if value['schedule'] is not None else None
        if value['auto_enabled'] and schedule is None:
            raise ValueError()
        if trends and schedule is not None and schedule['frequency'] not in {'daily','weekdays','weekly'}:
            raise ValueError()
        result = {'enabled': value['enabled'], 'auto_enabled': value['auto_enabled'], 'schedule': schedule}
        if type(result['enabled']) is not bool or type(result['auto_enabled']) is not bool or type(value['running']) is not bool:
            raise ValueError()
        result['running'] = value['running']
        phase = _execution_phase(value)
        result['execution_phase'] = phase if value['running'] and not trends else None
        result['run_mode'] = 'automatic' if result['auto_enabled'] else 'manual'
        if not trends:
            result['id'] = _monitor_id(value['id'])
            result['keyword'] = _text(value['keyword'], maximum=120)
            content_range = value.get('content_range', '30')
            if not isinstance(content_range, str) or content_range not in _CONTENT_RANGES:
                raise ValueError()
            result['content_range'] = content_range
        for name in ['last_run_at', 'next_run_at']:
            result[name] = _monitor_time(value.get(name))
        status = value.get('last_status')
        if status is not None and status not in _TASK_STATUSES:
            raise ValueError()
        result['last_status'] = status
        result['last_new_count'] = _count(value.get('last_new_count'), optional=True)
        return result
    except (KeyError, ValueError, TypeError):
        _bad_response()


def _monitor_task(value):
    if not isinstance(value, dict):
        _bad_response()
    result = {}
    for name, allowed in [('kind', {'keyword', 'trends'}), ('trigger', {'manual', 'automatic'}), ('status', _TASK_STATUSES)]:
        if not isinstance(value.get(name), str) or value[name] not in allowed:
            _bad_response()
        result[name] = value[name]
    phase = _execution_phase(value)
    result['execution_phase'] = phase if result['status'] == 'running' and result['kind'] == 'keyword' else None
    result['keyword'] = _text(value.get('keyword'), maximum=120, optional=True)
    result['started_at'] = _monitor_time(value.get('started_at'))
    result['finished_at'] = _monitor_time(value.get('finished_at'))
    result['new_count'] = _count(value.get('new_count'), optional=True)
    return result


def _project_monitoring(operation, payload):
    if not isinstance(payload, dict):
        _bad_response()
    if operation == 'monitoring_keyword_delete':
        if payload.get('deleted') is not True:
            _bad_response()
        return {'deleted': True}
    if operation == 'monitoring_keyword_run':
        return {'task': _monitor_task(payload.get('task'))}
    if operation in {'monitoring_keyword_create', 'monitoring_keyword_update', 'monitoring_trends_read', 'monitoring_trends_update'}:
        return {'item': _monitor_plan(payload.get('item'), trends='trends' in operation)}
    if payload.get('timezone') != 'Australia/Melbourne':
        _bad_response()
    keywords, tasks = payload.get('keywords'), payload.get('tasks')
    if not isinstance(keywords, list) or len(keywords) > _MAX_ITEMS or not isinstance(tasks, list) or len(tasks) > _MAX_ITEMS:
        _bad_response()
    return {'keywords': [_monitor_plan(v) for v in keywords], 'trends': _monitor_plan(payload.get('trends'), trends=True),
            'tasks': [_monitor_task(v) for v in tasks], 'timezone': 'Australia/Melbourne'}


class FoundationGateway:
    """One finite backend operation; no caller URLs, headers or connection options."""
    def request(self, operation: str, payload: dict | None = None) -> dict:
        if not isinstance(operation, str) or operation not in _OPERATIONS:
            raise GatewayError('invalid_operation')
        body = _request_body(operation, payload)
        method, path = _OPERATIONS[operation]
        headers = {'Accept': 'application/json'}
        if body is not None:
            headers['Content-Type'] = 'application/json'
        connection = None
        response = None
        result = None
        error_code = None
        dispatch_attempted = False
        try:
            connection = http.client.HTTPConnection(_HOST, backend_port(), timeout=120 if operation in {'search_submit', 'monitoring_keyword_run'} else _TIMEOUT_SECONDS)
            dispatch_attempted = True
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            status = response.status
            if type(status) is not int:
                _bad_response()
            if 300 <= status < 400:
                _bad_response()
            if status == 409:
                raise GatewayError('backend_busy')
            if status in (429, 503):
                raise GatewayError('backend_unavailable')
            expected = (201, 207) if operation == 'trends_submit' else ((201,) if method == 'POST' else (200,))
            if operation.startswith('monitoring_'):
                expected = (201,) if operation == 'monitoring_keyword_create' else (200,)
            if status not in expected:
                raise GatewayError('backend_failed')
            content_type = response.getheader('Content-Type', '').split(';', 1)[0].strip().lower()
            if content_type != 'application/json' or response.getheader('Content-Encoding', 'identity').lower() != 'identity':
                _bad_response()
            length = response.getheader('Content-Length')
            if length is not None and (not length.isascii() or not length.isdigit() or len(length) > 10 or int(length) > _MAX_RESPONSE_BYTES):
                _bad_response()
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
            if not isinstance(raw, bytes) or len(raw) > _MAX_RESPONSE_BYTES:
                _bad_response()
            if length is not None and len(raw) != int(length):
                _bad_response()
            decoded = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object, parse_constant=_reject_constant)
            result = _project(operation, decoded)
        except GatewayError as safe_error:
            error_code = safe_error.code
        except (OSError, http.client.HTTPException):
            error_code = 'request_uncertain' if method == 'POST' and dispatch_attempted else 'backend_unavailable'
        except Exception:
            error_code = 'backend_response_invalid'
        finally:
            for resource in (response, connection):
                if resource is not None:
                    try:
                        resource.close()
                    except Exception:
                        # No retry, raw exception logging or body/header disclosure.
                        pass
        if error_code is not None:
            # Outside raw exception handlers: no sensitive chained exception.
            raise GatewayError(error_code)
        return result
