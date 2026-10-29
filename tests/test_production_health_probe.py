"""Health transport boundaries only; loopback fixtures, no live CRM actions."""
from contextlib import contextmanager, redirect_stderr
from dataclasses import asdict
import io
import json
from pathlib import Path
import socket
import socketserver
import struct
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools import production_graceful_control as graceful
from tools.production_capability_operator import OperatorBackend

GATE = {'state': 'OPEN', 'generation': 'a' * 32}


@contextmanager
def endpoint(role, actions):
    """Each action serves one real TCP connection; reset sends an actual RST."""
    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            request = b''
            self.request.settimeout(2)
            while b'\r\n\r\n' not in request:
                chunk = self.request.recv(4096)
                if not chunk:
                    return
                request += chunk
            index = len(self.server.requests)
            self.server.requests.append((self.client_address, request))
            action = actions[min(index, len(actions) - 1)]
            if action == 'reset':
                self.request.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                                       struct.pack('ii', 1, 0))
                self.request.close()
                return
            payload = {'status': 'ok', 'service': role,
                       'port': self.server.server_address[1],
                       'dependencies': {'database': 'ok'},
                       'business_write_gate': GATE}
            status = 200
            if isinstance(action, dict):
                payload.update(action)
            if isinstance(action, int):
                status = action
            body = b'not-json' if action == 'invalid-json' else json.dumps(payload).encode()
            if action == 'oversize':
                body = b' ' * 65537
            wire = (f'HTTP/1.1 {status} Fixture\r\nContent-Length: {len(body)}\r\n'
                    'Connection: close\r\n\r\n').encode() + body
            try:
                self.request.sendall(wire)
            except (BrokenPipeError, ConnectionResetError):
                pass  # The size-limit test deliberately closes before draining.
    server = socketserver.TCPServer(('127.0.0.1', 0), Handler)
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01})
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(3)
        server.server_close()
        if thread.is_alive():
            raise AssertionError('fixture server did not exit')


class HealthProbeTests(unittest.TestCase):
    def backend(self, role, server):
        backend = OperatorBackend(armed=False)
        port_patch = patch.dict(graceful.legacy.SERVICE_PORTS, {role: server.server_address[1]})
        port_patch.start()
        self.addCleanup(port_patch.stop)
        return backend

    def test_healthy_admin(self):
        self.healthy('admin')

    def test_healthy_parent(self):
        self.healthy('parent')

    def healthy(self, role):
        with endpoint(role, ['healthy']) as server:
            backend = self.backend(role, server)
            self.assertTrue(backend.health_success(role, backend.health(role)))
            self.assertEqual(len(server.requests), 1)
            request = server.requests[0][1]
            self.assertTrue(request.startswith(b'GET /health HTTP/1.1\r\n'))
            self.assertIn(b'Accept: application/json\r\n', request)

    def test_transient_reset_requires_new_connection_and_actual_healthy_response(self):
        with endpoint('admin', ['reset', 'healthy']) as server, redirect_stderr(io.StringIO()) as log:
            backend = self.backend('admin', server)
            try:
                result = backend.health('admin')
            except ConnectionResetError:
                self.fail('transient reset prevented the required bounded healthy retry')
            self.assertTrue(backend.health_success('admin', result))
            self.assertEqual(len(server.requests), 2)
            self.assertNotEqual(server.requests[0][0], server.requests[1][0])
            event = json.loads(log.getvalue())
            self.assertEqual(event['event'], 'formal_health_connection_reset')
            self.assertEqual(event['service'], 'admin')
            self.assertEqual(event['phase'], 'response_headers')
            self.assertEqual(event['attempt'], 1)
            self.assertTrue(event['retry_scheduled'])
            self.assertEqual(event['local_address'][1], server.requests[0][0][1])
            self.assertTrue(event['stack'])
            self.assertTrue(all(set(frame) == {'file', 'function', 'line'} for frame in event['stack']))

    def test_persistent_reset_is_hold_after_two_requests(self):
        with endpoint('parent', ['reset']) as server, redirect_stderr(io.StringIO()) as log:
            with self.assertRaises(ConnectionResetError):
                self.backend('parent', server).health('parent')
            self.assertEqual(len(server.requests), 2)
            events = [json.loads(line) for line in log.getvalue().splitlines()]
            self.assertEqual([event['retry_scheduled'] for event in events], [True, False])

    def test_connection_refused_is_not_retried(self):
        # macOS can time out for a bound but non-listening fixture socket.
        # Inject the precise connect error without depending on that OS policy.
        self.transport_failure(ConnectionRefusedError(61, 'fixture refused'), 'request')

    def test_timeout_is_hold_without_retry(self):
        self.transport_failure(TimeoutError('fixture timeout'), 'response_headers')

    def test_request_phase_reset_is_hold_without_retry(self):
        self.transport_failure(ConnectionResetError(54, 'fixture reset'), 'request')

    def test_body_phase_reset_is_hold_without_retry(self):
        self.transport_failure(ConnectionResetError(54, 'fixture reset'), 'body')

    def transport_failure(self, failure, phase):
        class Connection:
            sock = None
            closed = False
            def request(self, *args, **kwargs):
                if phase == 'request': raise failure
            def getresponse(self):
                if phase == 'response_headers': raise failure
                return self
            status = 200
            def read(self, size): raise failure
            def close(self): self.closed = True
        connection = Connection()
        with patch.object(graceful.http.client, 'HTTPConnection', return_value=connection) as factory, redirect_stderr(io.StringIO()):
            with self.assertRaises(type(failure)):
                OperatorBackend(armed=False).health('parent')
            factory.assert_called_once_with('127.0.0.1', 8781, timeout=5)
            self.assertTrue(connection.closed)

    def test_unhealthy_response_is_not_retried(self):
        self.denied_payload({'status': 'failed'})

    def test_wrong_endpoint_identity_is_not_retried(self):
        self.denied_payload({'service': 'parent'})

    def test_wrong_port_is_not_retried(self):
        self.denied_payload({'port': 1})

    def test_bad_database_dependency_is_not_retried(self):
        self.denied_payload({'dependencies': {'database': 'missing'}})

    def denied_payload(self, payload):
        with endpoint('admin', [payload, 'healthy']) as server:
            backend = self.backend('admin', server)
            self.assertFalse(backend.health_success('admin', backend.health('admin')))
            self.assertEqual(len(server.requests), 1)

    def test_http_error_redirect_invalid_json_and_oversize_are_not_retried(self):
        for action in (503, 302, 'invalid-json', 'oversize'):
            with self.subTest(action=action), endpoint('admin', [action, 'healthy']) as server:
                with self.assertRaises((RuntimeError, ValueError)):
                    self.backend('admin', server).health('admin')
                self.assertEqual(len(server.requests), 1)

    def test_retry_does_not_turn_unhealthy_response_into_health(self):
        with endpoint('admin', ['reset', {'status': 'failed'}, 'healthy']) as server, redirect_stderr(io.StringIO()):
            backend = self.backend('admin', server)
            try:
                payload = backend.health('admin')
            except ConnectionResetError:
                self.fail('reset prevented checking the required second response')
            self.assertFalse(backend.health_success('admin', payload))
            self.assertEqual(len(server.requests), 2)



if __name__ == '__main__':
    unittest.main()
