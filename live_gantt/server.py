"""Loopback HTTP transport and demand-driven source polling."""

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import select
import socket
import threading
import time
from urllib.parse import parse_qs, urlsplit

from .freshness import assess
from .schedule import project
from .sources import LIST_ID, SourceStore, parse_time


DEFAULT_PORT = 4791
POLL_SECONDS = 2.0
HEARTBEAT_SECONDS = 15.0
SOCKET_TIMEOUT_SECONDS = 20.0
LOOPBACK = "127.0.0.1"
STATIC = Path(__file__).with_name("static")
STATIC_FILES = {"/": ("index.html", "text/html; charset=utf-8"),
                "/style.css": ("style.css", "text/css; charset=utf-8"),
                "/app.js": ("app.js", "text/javascript; charset=utf-8")}


def _favicon():
    stylesheet = (STATIC / "style.css").read_text(encoding="utf-8")
    done, running = (
        re.search(rf"--{token}:\s*(#[0-9a-fA-F]+);", stylesheet).group(1)
        for token in ("done", "running")
    )
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
        f'<rect x="3" y="6" width="19" height="8" rx="2" fill="{done}"/>'
        f'<rect x="11" y="18" width="18" height="8" rx="2" fill="{running}"/>'
        '</svg>'
    ).encode("utf-8")


def encode_json(value):
    """Encode a model as compact, standards-compliant UTF-8 JSON."""
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def _verdict(freshness):
    """The part of a freshness summary that changes only when a finding does, not every second."""
    return {key: value for key, value in freshness.items() if key != "sinceChangeSeconds"}


def fingerprint(value):
    """Return a stable content hash independent of dictionary insertion order."""
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False,
                         allow_nan=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass
class Monitor:
    """Serialize source access and poll only while event-stream readers exist."""

    source: SourceStore
    poll_seconds: float = POLL_SECONDS
    heartbeat_seconds: float = HEARTBEAT_SECONDS
    condition: threading.Condition = field(default_factory=threading.Condition)
    readers: dict = field(default_factory=dict)
    models: dict = field(default_factory=dict)
    list_model: list = field(default_factory=list)
    worker: threading.Thread | None = None
    stopping: bool = False

    def _refresh(self, ids, *, fresh_projection=False):
        self.list_model = self.source.lists()
        now = datetime.now(timezone.utc)
        for list_id in ids:
            raw = self.source.state(list_id, now)
            if raw is None:
                self.models.pop(list_id, None)
                continue
            freshness = assess(raw["tasks"], parse_time(raw["transcriptUpdatedAt"]), now)
            digest = fingerprint({"source": raw, "freshness": _verdict(freshness)})
            previous = self.models.get(list_id)
            if fresh_projection or previous is None or previous[0] != digest:
                projection = project(raw["tasks"], now)
                model = {**raw, **projection, "freshness": freshness, "observedAt": now.isoformat(),
                         "warnings": raw["warnings"] + projection["warnings"]}
                self.models[list_id] = (digest, model)

    def lists(self):
        """Read the session index on an explicit request."""
        with self.condition:
            self.list_model = self.source.lists()
            return self.list_model

    def state(self, list_id):
        """Read and project one session, returning None if it no longer exists."""
        with self.condition:
            self._refresh([list_id], fresh_projection=True)
            snapshot = self.models.get(list_id)
            return snapshot[1] if snapshot else None

    @contextmanager
    def watch(self, list_id):
        """Keep polling alive for the lifetime of one event-stream reader."""
        with self.condition:
            self.readers[list_id] = self.readers.get(list_id, 0) + 1
            if self.worker is None:
                self.worker = threading.Thread(target=self._poll, daemon=True,
                                               name="live-gantt-sources")
                self.worker.start()
            self.condition.notify_all()
        try:
            yield
        finally:
            with self.condition:
                self.readers[list_id] -= 1
                if not self.readers[list_id]:
                    del self.readers[list_id]
                self.condition.notify_all()

    def _poll(self):
        with self.condition:
            deadline = time.monotonic() + self.poll_seconds
            while self.readers and not self.stopping:
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    self.condition.wait(remaining)
                    continue
                self._refresh(tuple(self.readers))
                self.condition.notify_all()
                deadline = time.monotonic() + self.poll_seconds
            self.worker = None
            self.condition.notify_all()

    def close(self):
        """Stop the polling worker and wake connected stream handlers."""
        with self.condition:
            self.stopping = True
            worker = self.worker
            self.condition.notify_all()
        if worker:
            worker.join()


class LocalServer(ThreadingHTTPServer):
    """An IPv4 loopback server owning one shared source monitor."""

    daemon_threads = True

    def __init__(self, root, port, poll_seconds, heartbeat_seconds):
        self.monitor = Monitor(SourceStore(root), poll_seconds, heartbeat_seconds)
        super().__init__((LOOPBACK, port), Handler)

    def server_close(self):
        """Release listeners and finish demand-driven polling."""
        self.monitor.close()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    """Serve fixed assets and read-only JSON or event-stream endpoints."""

    protocol_version = "HTTP/1.1"

    def setup(self):
        """Bound slow-client writes without introducing source polling."""
        super().setup()
        self.connection.settimeout(SOCKET_TIMEOUT_SECONDS)

    def parse_request(self):
        """Reject foreign, absent, duplicate or malformed Host headers first."""
        if not super().parse_request():
            return False
        port = self.server.server_address[1]
        hosts = self.headers.get_all("Host", [])
        if len(hosts) != 1 or hosts[0] not in {f"{LOOPBACK}:{port}", f"localhost:{port}"}:
            self.send_error(403, "Untrusted Host")
            self.close_connection = True
            return False
        return True

    def end_headers(self):
        """Apply the same browser isolation headers to every response."""
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; connect-src 'self'; "
                         "object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        super().end_headers()

    def do_HEAD(self):
        """Return asset and API headers without streaming a response body."""
        self._get(head=True)

    def do_GET(self):
        """Dispatch the fixed read-only routes."""
        self._get(head=False)

    def _send(self, payload, content_type, head):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if not head:
            self.wfile.write(payload)

    def _get(self, head):
        url = urlsplit(self.path)
        if url.path in STATIC_FILES:
            filename, content_type = STATIC_FILES[url.path]
            self._send((STATIC / filename).read_bytes(), content_type, head)
        elif url.path == "/favicon.ico":
            self._send(_favicon(), "image/svg+xml", head)
        elif url.path == "/api/lists":
            self._send(encode_json({"lists": self.server.monitor.lists()}),
                       "application/json; charset=utf-8", head)
        elif url.path in {"/api/state", "/events"}:
            values = parse_qs(url.query).get("list", [])
            if len(values) != 1 or not LIST_ID.fullmatch(values[0]):
                self.send_error(400, "Supply one valid list ID")
                return
            list_id = values[0]
            state = self.server.monitor.state(list_id)
            if state is None:
                self.send_error(404, "Unknown task list")
            elif url.path == "/api/state":
                self._send(encode_json(state), "application/json; charset=utf-8", head)
            elif head:
                self._send(b"", "text/event-stream", head)
            else:
                self._events(list_id)
        else:
            self.send_error(404, "Not found")

    def _event(self, kind, value):
        self.wfile.write(b"event: " + kind.encode() + b"\ndata: " + encode_json(value) + b"\n\n")
        self.wfile.flush()

    def _disconnected(self):
        readable, _, _ = select.select([self.connection], [], [], 0)
        return bool(readable) and not self.connection.recv(1, socket.MSG_PEEK)

    def _events(self, list_id):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        monitor = self.server.monitor
        last_state = last_lists = None
        heartbeat = time.monotonic() + monitor.heartbeat_seconds
        self.close_connection = True
        try:
            with monitor.watch(list_id):
                while not monitor.stopping and not self._disconnected():
                    with monitor.condition:
                        lists = monitor.list_model
                        snapshot = monitor.models.get(list_id)
                    lists_digest = fingerprint(lists)
                    if lists_digest != last_lists:
                        self._event("lists", {"lists": lists})
                        last_lists = lists_digest
                    if snapshot and snapshot[0] != last_state:
                        self._event("state", snapshot[1])
                        last_state = snapshot[0]
                    elif snapshot is None and last_state is not None:
                        self._event("missing", {"id": list_id})
                        break
                    remaining = heartbeat - time.monotonic()
                    if remaining <= 0:
                        self.wfile.write(b": heartbeat\n\n")
                        self._event("heartbeat", {"observedAt": datetime.now(timezone.utc).isoformat()})
                        heartbeat = time.monotonic() + monitor.heartbeat_seconds
                    with monitor.condition:
                        monitor.condition.wait(min(monitor.poll_seconds,
                                                   max(0, heartbeat - time.monotonic())))
        except (BrokenPipeError, ConnectionError, TimeoutError):
            pass


def make_server(root, port=DEFAULT_PORT, *, poll_seconds=POLL_SECONDS,
                heartbeat_seconds=HEARTBEAT_SECONDS):
    """Create an unstarted server bound exclusively to IPv4 loopback."""
    return LocalServer(Path(root), port, poll_seconds, heartbeat_seconds)
