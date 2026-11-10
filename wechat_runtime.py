"""Explicit production WeChat configuration, separate from source and staging."""
from contextvars import ContextVar
import ipaddress
import json
import os
import socket
import stat
import sys
from urllib.parse import urlsplit
import urllib.request

from runtime_configuration import canonical, RuntimeConfigurationError

WECHAT_KEYS = frozenset({
    'WECHAT_APP_ID', 'WECHAT_APP_SECRET',
    'WECHAT_OAUTH_CALLBACK_URL', 'WECHAT_OAUTH_SCOPE',
})


SCHOOL_RADIUS_GEOCODING_ENV = 'AAIE_MANAGED_SCHOOL_RADIUS_GEOCODING'
_NOMINATIM_HOST = 'nominatim.openstreetmap.org'
# None outside the school-radius fetch. Resolved addresses belong to this
# request only, so concurrent requests cannot borrow or replace them.
_nominatim_addresses = ContextVar('school_radius_nominatim_addresses', default=None)


class _NoNominatimRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise PermissionError('School radius geocoding cannot follow redirects')


def open_school_radius_nominatim(request, *, timeout):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoNominatimRedirect())
    token = _nominatim_addresses.set(frozenset())
    try:
        return opener.open(request, timeout=timeout)
    finally:
        _nominatim_addresses.reset(token)


def runtime_wechat_environment(config):
    if config.mode != 'production':
        return {}
    try:
        path = canonical(config.root / 'config/wechat.json')
        if not path.exists():
            return {}
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'r', encoding='utf-8') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != os.getuid() or info.st_mode & 0o077):
                raise ValueError('Unsafe WeChat configuration permissions')
            value = json.load(stream)
        if (not isinstance(value, dict) or set(value) != WECHAT_KEYS
                or any(not isinstance(v, str) or not v.strip()
                       or any(c in v for c in '\r\n\0') for v in value.values())):
            raise ValueError('Invalid WeChat configuration schema')
        value = {k: v.strip() for k, v in value.items()}
        callback = urlsplit(value['WECHAT_OAUTH_CALLBACK_URL'])
        if (callback.scheme != 'https' or not callback.hostname
                or callback.username or callback.password or callback.fragment
                or callback.query or callback.path != '/api/wechat/oauth/callback'
                or value['WECHAT_OAUTH_SCOPE'] not in {'snsapi_base', 'snsapi_userinfo'}):
            raise ValueError('Invalid WeChat authorization configuration')
        return value
    except (OSError, ValueError, RuntimeConfigurationError):
        # Never include credential values or parser excerpts in controller output.
        raise ValueError('Production WeChat configuration is unreadable or invalid') from None


def runtime_wechat_network_rules(config, *, allow_school_radius=False):
    if not runtime_wechat_environment(config) and not allow_school_radius:
        return []
    # Seatbelt accepts only '*' or 'localhost', not a DNS name or literal IP.
    # ManagedRuntime installs the destination guard before importing app code.
    return ['(allow network-outbound (remote ip "*:443") (remote unix-socket))']


def _public_tls_addresses(host, service):
    addresses = set()
    for family, _, _, _, address in socket.getaddrinfo(
            host, 443, type=socket.SOCK_STREAM):
        if family not in (socket.AF_INET, socket.AF_INET6):
            continue
        ip = ipaddress.ip_address(address[0])
        if not ip.is_global:
            raise ValueError(f'{service} DNS returned a non-public address')
        addresses.add(str(ip))
    if not addresses:
        raise ValueError(f'{service} DNS returned no usable TLS addresses')
    return addresses


def wechat_api_addresses():
    return _public_tls_addresses('api.weixin.qq.com', 'WeChat')


class WechatNetworkGuard:
    """Restrict managed Python networking to local ports and approved HTTPS requests."""
    def __init__(self, config, *, allow_school_radius=False, allow_wechat=True):
        self.local_ports = frozenset(config.ports)
        self.allow_school_radius = allow_school_radius
        self.allow_wechat = allow_wechat
        self.addresses = wechat_api_addresses() if allow_wechat else frozenset()

    def audit(self, event, args):
        if event == 'urllib.Request':
            url = urlsplit(args[0])
            port = url.port or (443 if url.scheme == 'https' else 80)
            if _nominatim_addresses.get() is not None:
                if (self.allow_school_radius and url.scheme == 'https'
                        and url.hostname == _NOMINATIM_HOST and port == 443
                        and url.username is None and url.password is None
                        and not url.fragment and url.path == '/search' and args[3] == 'GET'):
                    _nominatim_addresses.set(_public_tls_addresses(_NOMINATIM_HOST, 'Nominatim'))
                    return
                raise PermissionError('Request is outside the school radius geocoding allowlist')
            if url.hostname in {'localhost', '127.0.0.1', '::1'} and port in self.local_ports:
                return
            if (self.allow_wechat and url.scheme == 'https' and url.hostname == 'api.weixin.qq.com'
                    and port == 443 and not url.username and not url.password
                    and url.path in {'/sns/oauth2/access_token', '/sns/userinfo'}):
                # Refresh at each allowed request so DNS changes do not require
                # a service restart. Redirects are checked by the same hook.
                self.addresses = wechat_api_addresses()
                return
            raise PermissionError('External request is outside the WeChat allowlist')
        if event == 'socket.connect':
            address = args[1]
            if not isinstance(address, tuple):
                return  # Local Unix sockets, including the system resolver.
            host, port = str(address[0]), int(address[1])
            nominatim = _nominatim_addresses.get()
            if nominatim is not None:
                if host in nominatim and port == 443:
                    return
                raise PermissionError('Connection is outside the school radius geocoding allowlist')
            if host in {'localhost', '127.0.0.1', '::1'} and port in self.local_ports:
                return
            if host in self.addresses and port == 443:
                return
            raise PermissionError('External connection is outside the WeChat allowlist')


def install_runtime_wechat_network_guard(config):
    allow_wechat = bool(config.mode == 'production' and os.environ.get('WECHAT_APP_ID')
                        and os.environ.get('WECHAT_APP_SECRET'))
    allow_school_radius = os.environ.get(SCHOOL_RADIUS_GEOCODING_ENV) == '1'
    if allow_wechat or allow_school_radius:
        guard = WechatNetworkGuard(config, allow_school_radius=allow_school_radius,
                                   allow_wechat=allow_wechat)
        sys.addaudithook(guard.audit)
