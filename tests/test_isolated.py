from contextlib import ExitStack
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from live_gantt.providers import ProviderStore, _CodexSocket
from tests import test_codex_daemon as fixtures

DaemonPeer, NOW, SESSION = fixtures.DaemonPeer, fixtures.NOW, fixtures.SESSION


class IsolatedProviderTests(unittest.TestCase):
    setUp = fixtures.CodexDaemonTests.setUp
    state = fixtures.CodexDaemonTests.state
    wait = fixtures.CodexDaemonTests.wait

    def isolated_peer(self, name='isolated'):
        root = self.root / name
        root.mkdir(mode=0o700)
        peer = DaemonPeer(root)
        self.addCleanup(peer.close)
        registry = self.root / 'codex-runtimes'
        registry.mkdir(mode=0o700, exist_ok=True)
        started = subprocess.check_output(['/bin/ps', '-o', 'lstart=', '-p', str(os.getpid())], text=True,
                                          env={**os.environ, 'LC_ALL': 'C'}).strip()
        lease = registry / (name.ljust(32, 'a')[:32].replace('isolated', 'abcdefab') + '.json')
        lease.write_text(json.dumps({'version': 1, 'codexRoot': str(self.root.resolve()),
                                    'socket': str(peer.path.resolve()), 'pid': os.getpid(), 'started': started}))
        lease.chmod(0o600)
        self.store.close()
        self.store = ProviderStore(self.root / 'claude', codex_root=self.root, usage_root=self.root / 'usage')
        self.addCleanup(self.store.close)
        return peer, lease

    def test_isolated_exact_session_takes_precedence_and_removed_lease_drops_waiting(self):
        self.peer.status = {'type': 'idle'}
        peer, lease = self.isolated_peer()
        self.wait(lambda value: value['phase'] == 'waiting')
        self.assertTrue(peer.messages)
        self.assertTrue(all(message.get('method') in {'initialize', 'initialized', 'thread/read'}
                            for message in peer.messages))
        lease.unlink()
        self.wait(lambda value: value['detail'] is None)
        self.assertEqual(self.state()['phase'], 'unknown')

    def test_unavailable_observer_thread_keeps_runtime_unverified_and_recovers(self):
        self.isolated_peer()
        with patch('threading.Thread.start', side_effect=RuntimeError('private thread limit')):
            value = self.state()
        self.assertEqual(value['phase'], 'unknown')
        self.assertIsNotNone(value['detail'])
        self.assertNotIn('private', str(value))
        self.wait(lambda value: value['phase'] == 'waiting')

    def test_private_request_transport_returns_only_matching_responses_and_sanitizes_errors(self):
        with _CodexSocket(self.peer.path.resolve(), private=True) as connection:
            result = connection.request('thread/read', {'threadId': SESSION, 'includeTurns': False})
            self.assertEqual(result['thread']['id'], SESSION)
            self.peer.error = True
            with self.assertRaisesRegex(ValueError, '^Native request failed$'):
                connection.request('thread/read', {'threadId': SESSION, 'includeTurns': False})
            self.peer.respond = False
            with patch('live_gantt.providers._DAEMON_REQUEST_SECONDS', 0.05):
                with self.assertRaisesRegex(ValueError, '^Native request timed out$'):
                    connection.request('thread/read', {'threadId': SESSION, 'includeTurns': False})
        self.assertIsNone(connection.socket)

    def test_changed_lease_identity_revokes_a_previous_waiting_observation(self):
        self.peer.status = {'type': 'idle'}
        peer, lease = self.isolated_peer()
        self.wait(lambda value: value['phase'] == 'waiting')
        payload = json.loads(lease.read_text())
        payload['started'] = 'different process incarnation'
        lease.write_text(json.dumps(payload))
        self.wait(lambda value: value['detail'] is None)
        self.assertEqual(self.state()["phase"], "unknown")

    def test_wrong_session_and_untrusted_lease_never_override_native_state(self):
        self.peer.status = {'type': 'idle'}
        peer, lease = self.isolated_peer()
        peer.response_id = 'different-session'
        self.wait(lambda value: bool(peer.messages) and value['detail'] is None)
        self.assertEqual(self.state()['phase'], 'unknown')
        lease.chmod(0o644)
        time.sleep(0.05)
        self.assertIsNone(self.state()['detail'])


class IsolatedRegistryTests(unittest.TestCase):
    def test_registration_lifetime_and_invalid_identity_fail_closed(self):
        from live_gantt.isolated import _register_runtime, _runtime_endpoints

        with tempfile.TemporaryDirectory(dir='/tmp') as temporary, ExitStack() as stack:
            root = Path(temporary).resolve()
            registry = root / 'registry'
            socket_root = root / 'native'
            socket_root.mkdir(mode=0o700)
            stream = stack.enter_context(socket.socket(socket.AF_UNIX))
            endpoint = socket_root / 'native.sock'
            stream.bind(str(endpoint))
            endpoint.chmod(0o600)
            with _register_runtime(registry, root, endpoint, os.getpid()) as lease:
                self.assertEqual(_runtime_endpoints(registry, root), [endpoint])
                self.assertEqual(_runtime_endpoints(registry, root / 'other'), [])
                original = lease.read_text()
                payload = json.loads(original)
                payload['started'] = 'stale process identity'
                lease.write_text(json.dumps(payload))
                self.assertEqual(_runtime_endpoints(registry, root), [])
                lease.write_text(original)
                endpoint.chmod(0o666)
                self.assertEqual(_runtime_endpoints(registry, root), [])
                endpoint.chmod(0o600)
                registry.chmod(0o755)
                self.assertEqual(_runtime_endpoints(registry, root), [])
                registry.chmod(0o700)
            self.assertEqual(list(registry.iterdir()), [])

    def test_oversized_symlink_and_foreign_entries_are_ignored(self):
        from live_gantt.isolated import _register_runtime, _runtime_endpoints

        with tempfile.TemporaryDirectory(dir='/tmp') as temporary, socket.socket(socket.AF_UNIX) as stream:
            root = Path(temporary).resolve()
            registry = root / 'registry'
            endpoint = root / 'native.sock'
            stream.bind(str(endpoint))
            endpoint.chmod(0o600)
            with _register_runtime(registry, root, endpoint, os.getpid()) as lease:
                original = lease.read_bytes()
                lease.write_bytes(b' ' * 8193)
                self.assertEqual(_runtime_endpoints(registry, root), [])
                lease.unlink()
                outside = root / 'outside.json'
                outside.write_bytes(original)
                outside.chmod(0o600)
                lease.symlink_to(outside)
                self.assertEqual(_runtime_endpoints(registry, root), [])
            self.assertTrue(outside.exists())
            self.assertTrue(lease.is_symlink())

    def test_registry_first_entry_over_limit_fails_closed_without_deleting_entries(self):
        from live_gantt.isolated import RUNTIME_LIMIT, _register_runtime, _runtime_endpoints

        with tempfile.TemporaryDirectory(dir='/tmp') as temporary, socket.socket(socket.AF_UNIX) as stream:
            root = Path(temporary).resolve()
            registry = root / 'registry'
            endpoint = root / 'native.sock'
            stream.bind(str(endpoint))
            endpoint.chmod(0o600)
            with _register_runtime(registry, root, endpoint, os.getpid()):
                for number in range(RUNTIME_LIMIT):
                    (registry / f'foreign-{number}.txt').write_text('unrelated')
                self.assertEqual(_runtime_endpoints(registry, root), [])
                with self.assertRaisesRegex(ValueError, 'full'):
                    with _register_runtime(registry, root, endpoint, os.getpid()):
                        self.fail('over-limit registration was accepted')
            self.assertEqual(len(list(registry.iterdir())), RUNTIME_LIMIT)

    def test_discovery_uses_one_bounded_process_probe_for_all_leases(self):
        from live_gantt.isolated import _register_runtime, _runtime_endpoints

        with tempfile.TemporaryDirectory(dir='/tmp') as temporary, ExitStack() as stack:
            root = Path(temporary).resolve()
            registry = root / 'registry'
            endpoint = root / 'native.sock'
            stream = stack.enter_context(socket.socket(socket.AF_UNIX))
            stream.bind(str(endpoint))
            endpoint.chmod(0o600)
            for _ in range(2):
                stack.enter_context(_register_runtime(registry, root, endpoint, os.getpid()))
            native_run = subprocess.run
            with patch('live_gantt.isolated.subprocess.run', wraps=native_run) as probe:
                self.assertEqual(_runtime_endpoints(registry, root), [endpoint])
                self.assertEqual(probe.call_count, 1)
                self.assertEqual(probe.call_args.kwargs['timeout'], 1.0)
            with patch('live_gantt.isolated.subprocess.run',
                       side_effect=subprocess.TimeoutExpired('ps', 1.0)) as probe:
                self.assertEqual(_runtime_endpoints(registry, root), [])
                self.assertEqual(probe.call_count, 1)
