from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from live_gantt import endpoint


PROBE_TIMEOUT_SECONDS = 0.1
PROBE_ELAPSED_LIMIT_SECONDS = 0.3
TRICKLE_INTERVAL_SECONDS = 0.03
TRICKLE_HEADER_BYTES = 16
FIXTURE_JOIN_SECONDS = 2
PROCESS_STARTED = "Tue Oct 27 12:00:00 2026"
PROCESS_RESTARTED = "Tue Oct 27 12:00:01 2026"
LINUX_SOCKET_INODE = 12345
LINUX_UNRELATED_INODE = 12346
LINUX_STAT_FIELDS_BEFORE_START = 18


class _Listener(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, host, trickle):
        self.connections = []
        self.requests = []
        self.trickle = trickle
        self.stopping = threading.Event()
        super().__init__((host, 0), _HeadHandler)

    def get_request(self):
        connection, address = super().get_request()
        self.connections.append(address)
        return connection, address


class _HeadHandler(BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.server.requests.append(self.path)
        try:
            if self.server.trickle:
                self.wfile.write(b"HTTP/1.1 200 OK\r\nX-Slow: ")
                self.wfile.flush()
                for _ in range(TRICKLE_HEADER_BYTES):
                    if self.server.stopping.wait(TRICKLE_INTERVAL_SECONDS):
                        return
                    self.wfile.write(b"a")
                    self.wfile.flush()
                self.wfile.write(b"\r\nContent-Length: 0\r\n\r\n")
                self.wfile.flush()
            else:
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *_arguments):
        pass


@contextmanager
def _listening(*, host="127.0.0.1", trickle=False):
    server = _Listener(host, trickle)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield server
    finally:
        server.stopping.set()
        server.shutdown()
        server.server_close()
        thread.join(FIXTURE_JOIN_SECONDS)


class EndpointSecurityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.application = Path(temporary.name)
        (self.application / endpoint.VERSION_FILENAME).write_text("1.0.0\n")

    @contextmanager
    def darwin_metadata(self, server, *, foreign_owner=False, wrong_service=False,
                        restarted=False):
        listener_pid = os.getpid()
        service_pid = listener_pid + int(wrong_service)
        owner = os.getuid() + int(foreign_owner)
        port = server.server_address[1]
        starts = 0

        def command(arguments, **_options):
            nonlocal starts
            if arguments[0] == "/bin/launchctl":
                output = f"{service_pid}\t0\tlocal.live-gantt\n"
            elif arguments[0] == endpoint.PS:
                starts += 1
                started = PROCESS_RESTARTED if restarted and starts > 1 else PROCESS_STARTED
                output = f"{arguments[-1]} {os.getuid()} {started}\n"
            elif arguments[0] == endpoint.LSOF:
                host = "*" if server.server_address[0] == "0.0.0.0" else "127.0.0.1"
                output = (f"p{listener_pid}\0u{owner}\0\nf{server.fileno()}\0"
                          f"tIPv4\0PTCP\0n{host}:{port}\0TST=LISTEN\0\n")
            else:
                raise AssertionError(f"Unexpected process inspection: {arguments}")
            return subprocess.CompletedProcess(arguments, 0, stdout=output.encode())

        with patch.object(endpoint.platform, "system", return_value="Darwin"), \
                patch.object(endpoint, "run_command", side_effect=command), \
                patch.object(endpoint, "DEFAULT_PORT", port):
            yield

    def test_current_user_launchd_listener_is_discovered_without_a_record(self):
        with _listening() as server, self.darwin_metadata(server):
            self.assertEqual(endpoint.discover_server_url(directory=self.application),
                             f"http://127.0.0.1:{server.server_address[1]}")
            self.assertEqual(server.requests, ["/"])
            self.assertFalse((self.application / endpoint.SERVER_DIRECTORY).exists())

    @unittest.skipUnless(platform.system() == "Linux", "Requires native Linux process metadata")
    def test_current_user_systemd_listener_is_discovered_without_a_record(self):
        with _listening() as server:
            def command(arguments, **_options):
                self.assertEqual(arguments[0], "systemctl")
                self.assertIn("--user", arguments)
                self.assertIn("live-gantt.service", arguments)
                return subprocess.CompletedProcess(arguments, 0, stdout=f"{os.getpid()}\n".encode())

            with patch.object(endpoint, "run_command", side_effect=command), \
                    patch.object(endpoint, "DEFAULT_PORT", server.server_address[1]):
                self.assertEqual(endpoint.discover_server_url(directory=self.application),
                                 f"http://127.0.0.1:{server.server_address[1]}")
            self.assertEqual(server.requests, ["/"])

    def test_linux_socket_inode_and_owner_must_match_the_service_process(self):
        process_id = os.getpid()
        proc = self.application / "proc"
        process = proc / str(process_id)
        descriptors = process / "fd"
        descriptors.mkdir(parents=True)
        (descriptors / "3").symlink_to(f"socket:[{LINUX_SOCKET_INODE}]")
        fields = "S " + "0 " * LINUX_STAT_FIELDS_BEFORE_START + "42 0 0\n"
        (process / "stat").write_text(f"{process_id} (fixture ) listener) {fields}")
        kernel = proc / "sys" / "kernel" / "random"
        kernel.mkdir(parents=True)
        (kernel / "boot_id").write_text("00000000-0000-0000-0000-000000000001\n")
        network = proc / "self" / "net"
        network.mkdir(parents=True)
        table = network / "tcp"

        def command(arguments, **_options):
            self.assertEqual(arguments[0], "systemctl")
            self.assertIn("--user", arguments)
            self.assertIn("live-gantt.service", arguments)
            return subprocess.CompletedProcess(arguments, 0, stdout=f"{process_id}\n".encode())

        with _listening() as server, \
                patch.object(endpoint.platform, "system", return_value="Linux"), \
                patch.object(endpoint, "PROC_ROOT", proc), \
                patch.object(endpoint, "run_command", side_effect=command), \
                patch.object(endpoint, "DEFAULT_PORT", server.server_address[1]):
            address = "0100007F" if sys.byteorder == "little" else "7F000001"
            local = f"{address}:{server.server_address[1]:04X}"

            def write_listener(owner, inode):
                table.write_text("sl local_address rem_address st tx_queue tr retrnsmt uid timeout inode\n"
                                 f"0: {local} 00000000:0000 0A 00000000:00000000 "
                                 f"00:00000000 00000000 {owner} 0 {inode} 1\n")

            write_listener(os.getuid(), LINUX_SOCKET_INODE)
            self.assertEqual(endpoint.discover_server_url(directory=self.application),
                             f"http://127.0.0.1:{server.server_address[1]}")
            self.assertEqual(server.requests, ["/"])
            server.connections.clear()
            server.requests.clear()

            for name, owner, inode in (
                ("unrelated socket", os.getuid(), LINUX_UNRELATED_INODE),
                ("foreign owner", os.getuid() + 1, LINUX_SOCKET_INODE),
            ):
                with self.subTest(case=name):
                    write_listener(owner, inode)
                    self.assertIsNone(endpoint.discover_server_url(directory=self.application))
                    self.assertEqual(server.connections, [])
                    self.assertEqual(server.requests, [])

    def test_unverified_legacy_listeners_are_rejected_before_connecting(self):
        cases = (
            ("foreign owner", "127.0.0.1", {"foreign_owner": True}),
            ("different service process", "127.0.0.1", {"wrong_service": True}),
            ("reused process identifier", "127.0.0.1", {"restarted": True}),
            ("wildcard listener", "0.0.0.0", {}),
        )
        for name, host, metadata in cases:
            with self.subTest(case=name), _listening(host=host) as server, \
                    self.darwin_metadata(server, **metadata):
                self.assertIsNone(endpoint.discover_server_url(directory=self.application))
                self.assertEqual(server.connections, [])
                self.assertEqual(server.requests, [])

    def test_trickling_head_response_cannot_extend_the_readiness_deadline(self):
        with _listening(trickle=True) as server, self.darwin_metadata(server), \
                patch.object(endpoint, "READY_TIMEOUT_SECONDS", PROBE_TIMEOUT_SECONDS):
            started = time.monotonic()
            result = endpoint.discover_server_url(directory=self.application)
            elapsed = time.monotonic() - started
            self.assertIsNone(result)
            self.assertLess(elapsed, PROBE_ELAPSED_LIMIT_SECONDS)
            self.assertEqual(server.requests, ["/"])
