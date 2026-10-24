"""Join provider readers and observe native session state without changing it."""

from base64 import b64encode
from hashlib import sha1
from copy import deepcopy
import os
from pathlib import Path
import socket
import stat
import struct
import threading
import time
import uuid
import json
from datetime import datetime, timezone

from .codex import CodexSourceStore
from .sources import SourceStore, parse_time, LIST_ID
from .usage import UsageStore
from .liveness import LivenessStore
from .isolated import RUNTIME_LIMIT, _private_endpoint, _runtime_endpoints, runtime_registry_root


CODEX_PREFIX = "codex-"
EARLIEST_TIME = datetime.min.replace(tzinfo=timezone.utc)
DISCOVERY_ERROR = "Session discovery failed; retrying."
QUOTA_READ_ERROR = "Recorded quota could not be refreshed; last observations are retained."


class ProviderStore:
    """Route session reads and expose one account-usage view under monitor serialization."""

    def __init__(self, root, *, codex_root=None, usage_root=None):
        self.claude = SourceStore(Path(root))
        self.codex = CodexSourceStore(Path(codex_root)) if codex_root is not None else None
        self.usage_store = UsageStore(usage_root)
        self._listings = {"claude": [], "codex": []}
        self._discovery_errors = {}
        self._liveness = LivenessStore()
        self._codex_daemon = None
        self._codex_isolated = {}
        self._runtime_registry = runtime_registry_root(usage_root)
        self._closed = False

    def close(self):
        """Stop the Codex observers owned by this source."""
        self._closed = True
        for observer in (self._codex_daemon, *self._codex_isolated.values()):
            if observer is not None:
                observer.close()

    @property
    def discovery_errors(self):
        """Return detached, sanitized discovery health for monitor coordination."""
        return dict(self._discovery_errors)

    def lists(self):
        """Refresh providers independently, retaining each failed provider's last listing."""
        for provider, source in (("claude", self.claude), ("codex", self.codex)):
            if source is None:
                continue
            prefix = CODEX_PREFIX if provider == "codex" else ""
            try:
                listing = [{**item, "id": prefix + item["id"]} for item in source.lists()]
            except Exception:
                self._discovery_errors[provider] = DISCOVERY_ERROR
            else:
                self._listings[provider] = deepcopy(listing)
                self._discovery_errors.pop(provider, None)
        sessions = [item for listing in self._listings.values() for item in listing]
        return deepcopy(sorted(sessions, key=lambda item: (
            parse_time(item["updatedAt"]) or EARLIEST_TIME, item["id"]), reverse=True))

    def state(self, list_id, now):
        _, source_id, source = self._source(list_id)
        model = source.state(source_id, now) if source is not None else None
        return {**model, "id": list_id} if model is not None else None

    def _source(self, list_id):
        if list_id.startswith(CODEX_PREFIX):
            return "codex", list_id.removeprefix(CODEX_PREFIX), self.codex
        return "claude", list_id, self.claude

    def liveness(self, list_id, now):
        """Probe session-bound runtime evidence independently of recorded progress."""
        provider, source_id, source = self._source(list_id)
        return self._liveness.state(provider, source_id, source.root if source is not None else None, now)

    def runtime(self, list_id, now):
        """Read native activity and approval state for a verified session owner."""
        provider, source_id, source = self._source(list_id)
        root = source.root if source is not None else None
        if provider != "codex" or root is None or self._closed:
            return self._liveness.runtime(provider, source_id, root, now)
        if self._liveness.state(provider, source_id, root, now)["status"] != "alive":
            for observer in (self._codex_daemon, *self._codex_isolated.values()):
                if observer is not None:
                    observer.forget(source_id)
            return self._liveness.runtime(provider, source_id, root, now)
        isolated = self._isolated_runtime(root, source_id)
        if isolated is not None:
            return isolated
        if self._codex_daemon is None:
            try:
                self._codex_daemon = _CodexDaemonRuntime(root)
            except (OSError, RuntimeError):
                pass
        return self._codex_daemon.state(source_id) if self._codex_daemon is not None else _unknown_runtime()

    def _isolated_runtime(self, root, source_id):
        endpoints = {}
        for path in _runtime_endpoints(self._runtime_registry, root):
            try:
                endpoints[path] = _private_endpoint(path)
            except (OSError, ValueError):
                continue
        for path, observer in list(self._codex_isolated.items()):
            if path not in endpoints or observer.endpoint_identity != endpoints[path]:
                observer.close()
                del self._codex_isolated[path]
        verified = []
        for path, identity in endpoints.items():
            if path not in self._codex_isolated and len(self._codex_isolated) < RUNTIME_LIMIT:
                try:
                    observer = _CodexDaemonRuntime(root, socket_path=path)
                except (OSError, RuntimeError):
                    continue
                observer.endpoint_identity = identity
                self._codex_isolated[path] = observer
            observer = self._codex_isolated.get(path)
            if observer is not None:
                value = observer.state(source_id)
                if value["phase"] == "waiting" or value["detail"] is None:
                    verified.append(value)
        return verified[0] if len(verified) == 1 else None

    def usage(self, now):
        failed = False
        try:
            samples = self.codex.usage_snapshots() if self.codex is not None else []
        except OSError:
            samples = None
            failed = True
        state = self.usage_store.state(samples, now)
        if failed:
            for provider in state["providers"]:
                if provider["provider"] == "codex":
                    provider["message"] = QUOTA_READ_ERROR
        return state


_DAEMON_READ_BYTES = 64 * 1024
_DAEMON_FRAME_LIMIT = 16 * 1024 * 1024
_DAEMON_REQUEST_LIMIT = 256
_DAEMON_WATCH_LIMIT = 64
_DAEMON_IO_SECONDS = 0.1
_DAEMON_REQUEST_SECONDS = 3.0
_DAEMON_IDLE_SECONDS = 60.0
_DAEMON_RETRY_SECONDS = 0.25
_DAEMON_RETRY_MAX_SECONDS = 8.0
_DAEMON_WAITING_FLAGS = frozenset({"waitingOnApproval", "waitingOnUserInput"})
_DAEMON_UNAVAILABLE = "Codex approval monitoring is unavailable; live requests cannot be verified."


def _unknown_runtime(detail=_DAEMON_UNAVAILABLE):
    return {"phase": "unknown", "lastActivityAt": None, "detail": detail}


_DAEMON_POLL_SECONDS = 2.0
_DAEMON_CLIENT_INFO = {"name": "live-gantt", "title": "Live Gantt", "version": "1"}
_WEBSOCKET_HEADER_LIMIT =16 * 1024
_WEBSOCKET_CONTROL_LIMIT = 125
_WEBSOCKET_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_WEBSOCKET_LENGTH_SHORT = 126
_WEBSOCKET_LENGTH_LONG = 127
_WEBSOCKET_LENGTH_16 = struct.Struct("!H")
_WEBSOCKET_LENGTH_64 = struct.Struct("!Q")
_WEBSOCKET_LENGTH_16_MAX = 65535
_WEBSOCKET_BASE_HEADER_BYTES = 2
_WEBSOCKET_MASK_BYTES = 4
_WEBSOCKET_KEY_BYTES = 16
_WEBSOCKET_FINAL_BIT = 0x80
_WEBSOCKET_MASK_BIT = 0x80
_WEBSOCKET_RESERVED_BITS = 0x70
_WEBSOCKET_LENGTH_BITS = 0x7f
_WEBSOCKET_CONTINUATION = 0
_WEBSOCKET_TEXT = 1
_WEBSOCKET_CLOSE = 8
_WEBSOCKET_PING = 9
_WEBSOCKET_PONG = 10
_WEBSOCKET_OPCODES = frozenset({_WEBSOCKET_CONTINUATION, _WEBSOCKET_TEXT, _WEBSOCKET_CLOSE,
                               _WEBSOCKET_PING, _WEBSOCKET_PONG})
_WEBSOCKET_OPCODE_BITS = 0x0f


def _daemon_status(status):
    if not isinstance(status, dict):
        raise ValueError("Invalid daemon status")
    kind = status.get("type")
    if kind not in {"active", "idle", "notLoaded", "systemError"}:
        raise ValueError("Unknown daemon status")
    flags = status.get("activeFlags", [])
    if (kind == "active" and "activeFlags" not in status
            or not isinstance(flags, list) or len(flags) > _DAEMON_REQUEST_LIMIT
            or any(not isinstance(flag, str) or flag not in _DAEMON_WAITING_FLAGS for flag in flags)
            or kind != "active" and flags):
        raise ValueError("Invalid daemon activity flags")
    if kind in {"notLoaded", "systemError"}:
        return _unknown_runtime()
    if kind == "active" and flags:
        return {"phase": "waiting", "lastActivityAt": None,
                "detail": "Codex is waiting for your approval or input."}
    return _unknown_runtime(None)


class _CodexSocket:
    def __init__(self, path, *, private=False):
        self.path = Path(path)
        self.private = private
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.socket = None
        self.buffer = bytearray()

    def close(self):
        self.stopping.set()
        with self.lock:
            stream = self.socket
            self.socket = None
        if stream is not None:
            try:
                stream.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            stream.close()

    def __enter__(self):
        try:
            self.connect()
            self.buffer = self.handshake(self.socket)
            self.request("initialize", {"clientInfo": _DAEMON_CLIENT_INFO})
            self.send(self.socket, {"jsonrpc": "2.0", "method": "initialized", "params": {}})
            return self
        except Exception:
            self.close()
            raise

    def __exit__(self, *exc):
        self.close()

    def request(self, method, params):
        request_id = str(uuid.uuid4())
        self.send(self.socket, {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + _DAEMON_REQUEST_SECONDS
        fragments = None
        while time.monotonic() < deadline and not self.stopping.is_set():
            for opcode, final, body in self.frames(self.socket, self.buffer):
                if opcode == _WEBSOCKET_TEXT:
                    if fragments is not None:
                        raise ValueError("Overlapping WebSocket fragments")
                    fragments = bytearray(body)
                elif fragments is None:
                    raise ValueError("Unexpected WebSocket continuation")
                else:
                    fragments.extend(body)
                if len(fragments) > _DAEMON_FRAME_LIMIT:
                    raise ValueError("Oversized WebSocket message")
                if not final:
                    continue
                message = json.loads(fragments)
                fragments = None
                if not isinstance(message, dict) or message.get("jsonrpc", "2.0") != "2.0":
                    raise ValueError("Invalid native response")
                if message.get("id") == request_id and "method" not in message:
                    if "error" in message or "result" not in message:
                        raise ValueError("Native request failed")
                    return message["result"]
            try:
                block = self.socket.recv(_DAEMON_READ_BYTES)
            except socket.timeout:
                continue
            if not block:
                raise ValueError("Native socket disconnected")
            self.buffer.extend(block)
        raise ValueError("Native request timed out")

    def _endpoint(self):
        if self.private:
            return self.path, _private_endpoint(self.path)
        advertised = self.path.lstat()
        directory = self.path.parent.lstat()
        physical = self.path.resolve(strict=True)
        target = physical.stat()
        target_directory = physical.parent.lstat()
        uid = os.getuid()
        if (not stat.S_ISDIR(directory.st_mode) or not stat.S_ISDIR(target_directory.st_mode)
                or not stat.S_ISSOCK(target.st_mode)
                or not (stat.S_ISSOCK(advertised.st_mode) or stat.S_ISLNK(advertised.st_mode))
                or any(info.st_uid != uid for info in (advertised, directory, target, target_directory))
                or any(info.st_mode & 0o022 for info in (directory, target, target_directory))):
            raise ValueError("Untrusted daemon endpoint")
        identity = tuple((info.st_dev, info.st_ino) for info in (advertised, directory, target, target_directory))
        return physical, identity

    def connect(self):
        physical, identity = self._endpoint()
        stream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stream.settimeout(_DAEMON_IO_SECONDS)
        with self.lock:
            self.socket = stream
        try:
            stream.connect(str(physical))
            if self._endpoint() != (physical, identity):
                raise ValueError("Daemon endpoint changed")
        except Exception:
            stream.close()
            raise
        return stream

    def _send_frame(self, stream, body, opcode=_WEBSOCKET_TEXT):
        length = len(body)
        if length > _DAEMON_FRAME_LIMIT or opcode >= _WEBSOCKET_CLOSE and length > _WEBSOCKET_CONTROL_LIMIT:
            raise ValueError("Oversized WebSocket frame")
        prefix = bytes([_WEBSOCKET_FINAL_BIT | opcode])
        if length < _WEBSOCKET_LENGTH_SHORT:
            prefix += bytes([_WEBSOCKET_MASK_BIT | length])
        elif length <= _WEBSOCKET_LENGTH_16_MAX:
            prefix += bytes([_WEBSOCKET_MASK_BIT | _WEBSOCKET_LENGTH_SHORT]) + _WEBSOCKET_LENGTH_16.pack(length)
        else:
            prefix += bytes([_WEBSOCKET_MASK_BIT | _WEBSOCKET_LENGTH_LONG]) + _WEBSOCKET_LENGTH_64.pack(length)
        mask = os.urandom(_WEBSOCKET_MASK_BYTES)
        stream.sendall(prefix + mask + bytes(value ^ mask[index % _WEBSOCKET_MASK_BYTES] for index, value in enumerate(body)))

    def send(self, stream, message):
        self._send_frame(stream, json.dumps(message, separators=(",", ":")).encode())

    def handshake(self, stream):
        key = b64encode(os.urandom(_WEBSOCKET_KEY_BYTES))
        stream.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                       b"Sec-WebSocket-Key: " + key + b"\r\nSec-WebSocket-Version: 13\r\n\r\n")
        deadline = time.monotonic() + _DAEMON_REQUEST_SECONDS
        data = bytearray()
        while b"\r\n\r\n" not in data:
            if time.monotonic() >= deadline or self.stopping.is_set():
                raise ValueError("Daemon handshake timed out")
            try:
                block = stream.recv(_DAEMON_READ_BYTES)
            except socket.timeout:
                continue
            if not block:
                raise ValueError("Daemon disconnected during handshake")
            data.extend(block)
            if len(data) > _WEBSOCKET_HEADER_LIMIT:
                raise ValueError("Oversized daemon handshake")
        header, remaining = bytes(data).split(b"\r\n\r\n", 1)
        lines = header.split(b"\r\n")
        headers = {}
        for line in lines[1:]:
            name, separator, value = line.partition(b":")
            name = name.strip().lower()
            if not separator or name in headers:
                raise ValueError("Malformed daemon handshake")
            headers[name] = value.strip()
        if (lines[0].split()[:2] != [b"HTTP/1.1", b"101"]
                or headers.get(b"upgrade", b"").lower() != b"websocket"
                or b"upgrade" not in [token.strip() for token in headers.get(b"connection", b"").lower().split(b",")]
                or headers.get(b"sec-websocket-accept") != b64encode(sha1(key + _WEBSOCKET_GUID).digest())
                or b"sec-websocket-extensions" in headers):
            raise ValueError("Untrusted daemon handshake")
        return bytearray(remaining)

    def frames(self, stream, buffer):
        while len(buffer) >= _WEBSOCKET_BASE_HEADER_BYTES:
            first, second = buffer[:_WEBSOCKET_BASE_HEADER_BYTES]
            opcode, final = first & _WEBSOCKET_OPCODE_BITS, bool(first & _WEBSOCKET_FINAL_BIT)
            length, offset = second & _WEBSOCKET_LENGTH_BITS, _WEBSOCKET_BASE_HEADER_BYTES
            if first & _WEBSOCKET_RESERVED_BITS or second & _WEBSOCKET_MASK_BIT or opcode not in _WEBSOCKET_OPCODES:
                raise ValueError("Unsupported WebSocket frame")
            length_format = (_WEBSOCKET_LENGTH_16 if length == _WEBSOCKET_LENGTH_SHORT else
                             _WEBSOCKET_LENGTH_64 if length == _WEBSOCKET_LENGTH_LONG else None)
            if length_format is not None:
                offset += length_format.size
                if len(buffer) < offset:
                    return
                length = length_format.unpack(buffer[_WEBSOCKET_BASE_HEADER_BYTES:offset])[0]
            if length > _DAEMON_FRAME_LIMIT or opcode >= _WEBSOCKET_CLOSE and (not final or length > _WEBSOCKET_CONTROL_LIMIT):
                raise ValueError("Oversized WebSocket frame")
            if len(buffer) < offset + length:
                return
            body = bytes(buffer[offset:offset + length])
            del buffer[:offset + length]
            if opcode == _WEBSOCKET_CLOSE:
                raise ValueError("Daemon closed WebSocket")
            if opcode == _WEBSOCKET_PING:
                self._send_frame(stream, body, _WEBSOCKET_PONG)
            elif opcode != _WEBSOCKET_PONG:
                yield opcode, final, body


class _CodexDaemonRuntime:
    def __init__(self, root, *, socket_path=None):
        path = socket_path or Path(root) / "app-server-control" / "app-server-control.sock"
        self.transport = _CodexSocket(path, private=socket_path is not None)
        self.lock = threading.Lock()
        self.stopping = self.transport.stopping
        self.entries = {}
        self.thread = threading.Thread(target=self._run, name="live-gantt-codex-daemon", daemon=True)
        self.thread.start()

    def state(self, source_id):
        if not isinstance(source_id, str) or not LIST_ID.fullmatch(source_id):
            return _unknown_runtime()
        now = time.monotonic()
        with self.lock:
            if source_id not in self.entries and len(self.entries) < _DAEMON_WATCH_LIMIT:
                self.entries[source_id] = {"requested": now, "checked": 0, "runtime": _unknown_runtime()}
            entry = self.entries.get(source_id)
            if entry is None:
                return _unknown_runtime()
            entry["requested"] = now
            if now - entry["checked"] > _DAEMON_POLL_SECONDS + _DAEMON_REQUEST_SECONDS:
                return _unknown_runtime()
            return dict(entry["runtime"])

    def forget(self, source_id):
        with self.lock:
            self.entries.pop(source_id, None)

    def close(self):
        self.transport.close()
        self.thread.join(_DAEMON_REQUEST_SECONDS + _DAEMON_IO_SECONDS)

    def _publish(self, source_id, value, now):
        with self.lock:
            if source_id in self.entries:
                self.entries[source_id].update(runtime=value, checked=now)

    def _run(self):
        retry = _DAEMON_RETRY_SECONDS
        while not self.stopping.is_set():
            stream = None
            self.received_read = False
            try:
                with self.lock:
                    now = time.monotonic()
                    self.entries = {key: entry for key, entry in self.entries.items()
                                    if now - entry["requested"] <= _DAEMON_IDLE_SECONDS}
                    wanted = bool(self.entries)
                if not wanted:
                    self.stopping.wait(_DAEMON_IO_SECONDS)
                    continue
                stream = self.transport.connect()
                self._observe(stream)
            except (OSError, ValueError, TypeError, KeyError, IndexError, RecursionError):
                pass
            finally:
                with self.lock:
                    self.transport.socket = None
                    for entry in self.entries.values():
                        entry.update(runtime=_unknown_runtime(), checked=0)
                if stream is not None:
                    stream.close()
            if self.received_read:
                retry = _DAEMON_RETRY_SECONDS
            self.stopping.wait(retry)
            if not self.received_read:
                retry = min(_DAEMON_RETRY_MAX_SECONDS, retry * 2)

    def _observe(self, stream):
        buffer = self.transport.handshake(stream)
        initializing = str(uuid.uuid4())
        self.transport.send(stream, {"jsonrpc": "2.0", "id": initializing, "method": "initialize", "params": {
            "clientInfo": _DAEMON_CLIENT_INFO}})
        pending = {initializing: (None, 0, time.monotonic() + _DAEMON_REQUEST_SECONDS)}
        initialized = False
        watched = {}
        fragments = None
        while not self.stopping.is_set():
            now = time.monotonic()
            if any(deadline <= now for _, _, deadline in pending.values()):
                raise ValueError("Daemon response timed out")
            with self.lock:
                wanted = {source_id for source_id, entry in self.entries.items()
                          if now - entry["requested"] <= _DAEMON_IDLE_SECONDS}
                for source_id in set(self.entries) - wanted:
                    self.entries.pop(source_id, None)
            for source_id in set(watched) - wanted:
                watched.pop(source_id)
            if initialized:
                for source_id in wanted:
                    state = watched.setdefault(source_id, {"polled": 0, "generation": 0, "loaded": False})
                    if (now - state["polled"] >= _DAEMON_POLL_SECONDS
                            and not any(item[0] == source_id for item in pending.values())):
                        request_id = str(uuid.uuid4())
                        self.transport.send(stream, {"jsonrpc": "2.0", "id": request_id, "method": "thread/read",
                                            "params": {"threadId": source_id, "includeTurns": False}})
                        pending[request_id] = (source_id, state["generation"], now + _DAEMON_REQUEST_SECONDS)
                        state["polled"] = now
            try:
                block = stream.recv(_DAEMON_READ_BYTES)
            except socket.timeout:
                block = None
            if block == b"":
                raise ValueError("Daemon disconnected")
            if block:
                buffer.extend(block)
            for opcode, final, body in self.transport.frames(stream, buffer):
                if opcode == _WEBSOCKET_TEXT:
                    if fragments is not None:
                        raise ValueError("Overlapping WebSocket fragments")
                    fragments = bytearray(body)
                elif fragments is None:
                    raise ValueError("Unexpected WebSocket continuation")
                else:
                    fragments.extend(body)
                if len(fragments) > _DAEMON_FRAME_LIMIT:
                    raise ValueError("Oversized WebSocket message")
                if not final:
                    continue
                message = json.loads(fragments)
                fragments = None
                if not isinstance(message, dict) or message.get("jsonrpc", "2.0") != "2.0":
                    raise ValueError("Invalid daemon message")
                if "method" in message:
                    if "id" in message or message["method"] != "thread/status/changed":
                        continue
                    params = message.get("params")
                    if not isinstance(params, dict):
                        raise ValueError("Invalid daemon notification")
                    source_id = params.get("threadId")
                    state = watched.get(source_id) if isinstance(source_id, str) else None
                    if state is not None and state["loaded"]:
                        value = _daemon_status(params.get("status"))
                        state["generation"] += 1
                        state["loaded"] = value["detail"] is None or value["phase"] == "waiting"
                        self._publish(source_id, value, now)
                    continue
                request_id = message.get("id")
                if not isinstance(request_id, str) or request_id not in pending:
                    continue
                source_id, generation, _ = pending.pop(request_id)
                if source_id is None:
                    if "error" in message or not isinstance(message.get("result"), dict):
                        raise ValueError("Daemon initialization failed")
                    self.transport.send(stream, {"jsonrpc": "2.0", "method": "initialized", "params": {}})
                    initialized = True
                    continue
                state = watched.get(source_id)
                if state is None or generation != state["generation"]:
                    continue
                if "error" in message:
                    state["loaded"] = False
                    self._publish(source_id, _unknown_runtime(), now)
                    continue
                result = message.get("result")
                thread = result.get("thread") if isinstance(result, dict) else None
                if not isinstance(thread, dict) or thread.get("id") != source_id:
                    raise ValueError("Daemon returned a different thread")
                value = _daemon_status(thread.get("status"))
                self.received_read = True
                state["loaded"] = value["detail"] is None or value["phase"] == "waiting"
                self._publish(source_id, value, now)
