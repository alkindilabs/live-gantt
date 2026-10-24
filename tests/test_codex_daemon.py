from base64 import b64encode
from datetime import datetime, timezone
from hashlib import sha1
import fcntl
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from live_gantt.providers import ProviderStore


NOW = datetime(2026, 10, 18, tzinfo=timezone.utc)
SESSION = 'approval-session'
WEBSOCKET_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
CONTINUATION_OPCODE, TEXT_OPCODE, CLOSE_OPCODE, PING_OPCODE, PONG_OPCODE = 0, 1, 8, 9, 10
FINAL_BIT = MASK_BIT = 0x80
OPCODE_BITS = 0x0f
LENGTH_BITS = 0x7f
SIXTEEN_BIT_LENGTH, SIXTY_FOUR_BIT_LENGTH = 126, 127
MASK_BYTES = 4


def frame(payload, opcode=TEXT_OPCODE, final=True):
    body = json.dumps(payload).encode() if isinstance(payload, dict) else payload
    size = len(body)
    prefix = bytes([(FINAL_BIT if final else 0) | opcode])
    short = size < SIXTEEN_BIT_LENGTH
    return prefix + (bytes([size]) if short else bytes([SIXTEEN_BIT_LENGTH]) + struct.pack('!H', size)) + body


class DaemonPeer:
    def __init__(self, root):
        directory = root / 'app-server-control'
        directory.mkdir(mode=0o700)
        target_directory = root / 'native-daemon'
        target_directory.mkdir(mode=0o700)
        self.path = target_directory / 'daemon.sock'
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(self.path))
        self.path.chmod(0o600)
        (directory / 'app-server-control.sock').symlink_to(self.path)
        self.listener.listen()
        self.listener.settimeout(0.03)
        self.stopping = threading.Event()
        self.connection = None
        self.send_lock = threading.Lock()
        self.messages = []
        self.status = {'type': 'active', 'activeFlags': ['waitingOnApproval']}
        self.response_id = SESSION
        self.respond = True
        self.hold_reads = False
        self.held_reads = []
        self.error = False
        self.bad_accept = False
        self.connections = 0
        self.thread = threading.Thread(target=self.run)
        self.thread.start()

    def send(self, payload, opcode=TEXT_OPCODE, final=True):
        with self.send_lock:
            if self.connection:
                self.connection.sendall(frame(payload, opcode, final))

    def notify(self, session=SESSION, status=None):
        self.send({'jsonrpc': '2.0', 'method': 'thread/status/changed', 'params': {
            'threadId': session, 'status': self.status if status is None else status}})

    def disconnect(self):
        with self.send_lock:
            if self.connection:
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def read(self, count):
        data = b''
        while len(data) < count and not self.stopping.is_set():
            try:
                block = self.connection.recv(count - len(data))
            except socket.timeout:
                continue
            if not block:
                raise ConnectionError('closed')
            data += block
        return data

    def run(self):
        while not self.stopping.is_set():
            try:
                self.connection, _ = self.listener.accept()
                self.connections += 1
                self.connection.settimeout(0.03)
                header = b''
                while not header.endswith(b'\r\n\r\n'):
                    header += self.read(1)
                key = next(line.split(b':', 1)[1].strip() for line in header.split(b'\r\n')
                           if line.lower().startswith(b'sec-websocket-key:'))
                accept = b64encode(sha1(key + WEBSOCKET_GUID.encode()).digest()).decode()
                if self.bad_accept:
                    accept = 'invalid'
                self.connection.sendall(('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n'
                                         'Connection: Upgrade\r\nSec-WebSocket-Accept: ' + accept + '\r\n\r\n').encode())
                while not self.stopping.is_set():
                    first, second = self.read(2)
                    size = second & LENGTH_BITS
                    if size == SIXTEEN_BIT_LENGTH:
                        size = struct.unpack('!H', self.read(2))[0]
                    elif size == SIXTY_FOUR_BIT_LENGTH:
                        size = struct.unpack('!Q', self.read(8))[0]
                    if not second & MASK_BIT:
                        raise ValueError('client frame must be masked')
                    mask = self.read(MASK_BYTES)
                    payload = bytes(value ^ mask[index % MASK_BYTES] for index, value in enumerate(self.read(size)))
                    opcode = first & OPCODE_BITS
                    if opcode == PONG_OPCODE:
                        self.messages.append({'pong': payload.decode()})
                        continue
                    if opcode == CLOSE_OPCODE:
                        break
                    message = json.loads(payload)
                    self.messages.append(message)
                    if message.get('method') == 'initialize':
                        self.send({'jsonrpc': '2.0', 'id': message['id'], 'result': {'userAgent': 'codex-test/1'}})
                    elif message.get('method') == 'thread/read' and self.respond:
                        if self.error:
                            self.send({'jsonrpc': '2.0', 'id': message['id'], 'error': {'code': -32600,
                                       'message': 'private unavailable credentials'}})
                        else:
                            response = {'jsonrpc': '2.0', 'id': message['id'], 'result': {'thread': {
                                'id': self.response_id, 'status': self.status, 'preview': 'private user text'}}}
                            if self.hold_reads:
                                self.held_reads.append(response)
                            else:
                                self.send(response)
            except (OSError, ValueError, StopIteration, ConnectionError):
                pass
            finally:
                if self.connection:
                    self.connection.close()
                    self.connection = None

    def close(self):
        self.stopping.set()
        try:
            self.disconnect()
        except OSError:
            pass
        self.thread.join(2)
        self.listener.close()


def held_writer_lock(path):
    info = Path(path).stat()
    return (info.st_dev, info.st_ino), True


@unittest.skipUnless(hasattr(socket, 'AF_UNIX'), 'Local Unix socket contract')
class CodexDaemonTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir='/tmp')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.peer = DaemonPeer(self.root)
        self.addCleanup(self.peer.close)
        lock = self.root / 'thread-writer-locks' / (SESSION + '.lock')
        lock.parent.mkdir()
        self.owned = lock.open('w')
        fcntl.flock(self.owned, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(self.owned.close)
        def command(args, **kwargs):
            if args[0].endswith('ps'):
                output = 'Sun Oct 18 00:00:00 2026\n'
            else:
                info = Path(args[-1]).stat()
                output = f'p{os.getpid()}\0\nf9\0i{info.st_ino}\0D0x{info.st_dev:x}\0\n'
            return subprocess.CompletedProcess(args, 0, stdout=output)
        self.addCleanup(patch.stopall)
        patch('live_gantt.liveness.run_command', side_effect=command).start()
        patch('platform.system', return_value='Darwin').start()
        if sys.platform != 'darwin':
            patch('live_gantt.liveness._writer_lock', side_effect=held_writer_lock).start()
        self.store = ProviderStore(self.root / 'claude', codex_root=self.root)
        self.addCleanup(self.store.close)

    def state(self):
        return self.store.runtime('codex-' + SESSION, NOW)

    def wait(self, predicate, message='daemon condition did not arrive'):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            value = self.state()
            if predicate(value):
                return value
            time.sleep(0.01)
        self.fail(message + ': ' + repr(value))

    def test_pending_resolves_and_notifications_are_isolated_without_owning_session(self):
        value = self.wait(lambda value: value['phase'] == 'waiting')
        self.assertIsNone(value['lastActivityAt'])
        self.assertNotIn('private', str(value))
        self.peer.status = {'type': 'active', 'activeFlags': []}
        self.peer.notify()
        self.wait(lambda value: value['detail'] is None)
        self.peer.notify('other-session', {'type': 'active', 'activeFlags': ['waitingOnUserInput']})
        time.sleep(0.05)
        self.assertIsNone(self.state()['detail'])
        self.peer.status = {'type': 'active', 'activeFlags': ['waitingOnUserInput']}
        self.peer.notify()
        self.wait(lambda value: value['phase'] == 'waiting')
        self.assertTrue(all(message.get('method') in {'initialize', 'initialized', 'thread/read'}
                            for message in self.peer.messages))
        reads = [message for message in self.peer.messages if message.get('method') == 'thread/read']
        self.assertTrue(reads)
        self.assertTrue(all(message['params'] == {'threadId': SESSION, 'includeTurns': False} for message in reads))

    def test_wrong_thread_not_loaded_and_request_errors_never_claim_coverage(self):
        self.peer.response_id = 'different-session'
        self.peer.hold_reads = True
        self.wait(lambda value: bool(self.peer.held_reads))
        wrong_thread_response = self.peer.held_reads.pop(0)
        self.assertIsNotNone(self.state()['detail'])
        self.peer.response_id = SESSION
        self.peer.status = {'type': 'notLoaded'}
        self.peer.hold_reads = False
        self.peer.send(wrong_thread_response)
        self.wait(lambda value: self.peer.connections > 1 and value['detail'] is not None)
        self.peer.error = True
        self.peer.disconnect()
        time.sleep(0.15)
        self.assertNotIn('private', str(self.state()))
        self.assertIsNotNone(self.state()['detail'])
        self.peer.error = False
        self.peer.status = {'type': 'idle'}
        self.peer.disconnect()
        self.wait(lambda value: value['detail'] is None)

    def test_malformed_status_or_handshake_cannot_retain_waiting(self):
        self.wait(lambda value: value['phase'] == 'waiting')
        self.peer.status = {'type': 'active', 'activeFlags': 'waitingOnApproval'}
        self.peer.notify()
        self.wait(lambda value: value['phase'] == 'unknown' and value['detail'])
        self.peer.bad_accept = True
        self.peer.disconnect()
        time.sleep(0.1)
        self.assertIsNotNone(self.state()['detail'])
        self.peer.bad_accept = False
        self.peer.status = {'type': 'active', 'activeFlags': []}
        self.wait(lambda value: value['detail'] is None)

    def test_fragments_ping_and_server_requests_are_safe(self):
        self.wait(lambda value: value['phase'] == 'waiting')
        self.peer.send(b'alive', opcode=PING_OPCODE)
        data = json.dumps({'jsonrpc': '2.0', 'method': 'thread/status/changed', 'params': {
            'threadId': SESSION, 'status': {'type': 'idle'}}}).encode()
        self.peer.send(data[:20], final=False)
        self.peer.send(data[20:], opcode=CONTINUATION_OPCODE)
        self.wait(lambda value: value['detail'] is None)
        self.peer.send({'jsonrpc': '2.0', 'id': 'server-approval',
                        'method': 'item/commandExecution/requestApproval', 'params': {'threadId': SESSION}})
        time.sleep(0.05)
        self.assertTrue(any(message.get('pong') == 'alive' for message in self.peer.messages))
        self.assertFalse(any(message.get('id') == 'server-approval' for message in self.peer.messages))

    def test_missing_responses_disconnect_recovery_and_close(self):
        with patch('live_gantt.providers._DAEMON_POLL_SECONDS', 0.04), \
                patch('live_gantt.providers._DAEMON_REQUEST_SECONDS', 0.12):
            self.wait(lambda value: value['phase'] == 'waiting')
            self.peer.respond = False
            self.wait(lambda value: value['phase'] == 'unknown' and value['detail'])
            self.peer.respond = True
            self.wait(lambda value: value['phase'] == 'waiting')
            self.peer.respond = False
            self.peer.disconnect()
            self.wait(lambda value: value['phase'] == 'unknown' and value['detail'])
            self.store.close()
            self.assertFalse(any(thread.name == 'live-gantt-codex-daemon' and thread.is_alive()
                                 for thread in threading.enumerate()))

    def test_unverified_liveness_and_unsafe_socket_never_connect(self):
        self.owned.close()
        self.assertEqual(self.state()['phase'], 'unknown')
        time.sleep(0.05)
        self.assertEqual(self.peer.connections, 0)
        self.owned = (self.root / 'thread-writer-locks' / (SESSION + '.lock')).open('w')
        fcntl.flock(self.owned, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(self.owned.close)
        self.peer.path.parent.chmod(0o777)
        self.store.close()
        self.store = ProviderStore(self.root / 'claude', codex_root=self.root)
        self.addCleanup(self.store.close)
        self.assertIsNotNone(self.state()['detail'])
        time.sleep(0.1)
        self.assertEqual(self.peer.connections, 0)

    def test_new_notification_wins_over_older_in_flight_read(self):
        with patch('live_gantt.providers._DAEMON_POLL_SECONDS', 0.04):
            self.peer.status = {'type': 'active', 'activeFlags': []}
            self.wait(lambda value: value['detail'] is None)
            self.peer.hold_reads = True
            self.wait(lambda value: bool(self.peer.held_reads))
            self.peer.status = {'type': 'active', 'activeFlags': ['waitingOnApproval']}
            self.peer.notify()
            self.wait(lambda value: value['phase'] == 'waiting')
            self.peer.send(self.peer.held_reads.pop(0))
            time.sleep(0.05)
            self.assertEqual(self.state()['phase'], 'waiting')
            self.peer.hold_reads = False
            self.wait(lambda value: value['phase'] == 'waiting')

    def test_unverified_notification_cannot_prove_loaded_thread(self):
        self.peer.hold_reads = True
        self.wait(lambda value: bool(self.peer.held_reads))
        self.peer.notify()
        time.sleep(0.05)
        self.assertIsNotNone(self.state()['detail'])
        self.assertEqual(self.state()['phase'], 'unknown')
        self.peer.hold_reads = False
        self.peer.send(self.peer.held_reads.pop(0))
        self.wait(lambda value: value['phase'] == 'waiting')

    def test_invalid_frames_and_incomplete_active_flags_fail_closed(self):
        self.wait(lambda value: value['phase'] == 'waiting')
        self.peer.status = {'type': 'active'}
        self.peer.notify()
        self.wait(lambda value: value['phase'] == 'unknown' and value['detail'])
        self.peer.status = {'type': 'active', 'activeFlags': ['waitingOnApproval']}
        self.wait(lambda value: value['phase'] == 'waiting')
        self.peer.respond = False
        with self.peer.send_lock:
            self.peer.connection.sendall(b'\x81\x80' + b'mask')
        self.wait(lambda value: value['phase'] == 'unknown' and value['detail'])
        self.peer.respond = True
        self.wait(lambda value: value['phase'] == 'waiting')
        self.peer.respond = False
        with self.peer.send_lock:
            self.peer.connection.sendall(b'\x81\x7f' + struct.pack('!Q', 2 ** 40))
        self.wait(lambda value: value['phase'] == 'unknown' and value['detail'])

    def test_new_notification_wins_over_older_in_flight_error(self):
        with patch('live_gantt.providers._DAEMON_POLL_SECONDS', 0.04):
            self.peer.status = {'type': 'active', 'activeFlags': []}
            self.wait(lambda value: value['detail'] is None)
            self.peer.hold_reads = True
            self.wait(lambda value: bool(self.peer.held_reads))
            self.peer.status = {'type': 'active', 'activeFlags': ['waitingOnApproval']}
            self.peer.notify()
            self.wait(lambda value: value['phase'] == 'waiting')
            response = self.peer.held_reads.pop(0)
            self.peer.send({'jsonrpc': '2.0', 'id': response['id'],
                            'error': {'code': -32600, 'message': 'old failure'}})
            time.sleep(0.05)
            self.assertEqual(self.state()['phase'], 'waiting')
