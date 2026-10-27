from contextlib import contextmanager
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from live_gantt import endpoint
from live_gantt.endpoint import account_server, discover_server_url


@contextmanager
def serving(server):
    thread = threading.Thread(target=server.serve_forever,
                              kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield
    finally:
        server.shutdown()
        thread.join(timeout=2)


class AccountEndpointTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def account(self, name):
        directory = self.root / name
        tasks = directory / "claude" / "tasks" / name
        tasks.mkdir(parents=True)
        (tasks / "1.json").write_text(json.dumps({
            "id": "1", "subject": name, "status": "pending",
            "metadata": {}, "blockedBy": [],
        }))
        return directory

    def server(self, directory, **kwargs):
        return account_server(directory / "claude", directory=directory / "app",
                              codex_root=directory / "codex", usage_root=directory / "usage",
                              **kwargs)

    def request(self, server, path, *, headers=None):
        connection = http.client.HTTPConnection(*server.server_address, timeout=2)
        self.addCleanup(connection.close)
        connection.request("GET", path, headers=headers or {})
        response = connection.getresponse()
        return response.status, response.getheaders(), response.read()

    def test_two_accounts_serve_only_their_own_sessions_and_discover_their_ports(self):
        first, second = self.account("first"), self.account("second")
        with self.server(first) as one, self.server(second) as two, serving(one), serving(two):
            self.assertNotEqual(one.server_address, two.server_address)
            for directory, server in ((first, one), (second, two)):
                with self.subTest(account=directory.name):
                    expected = f"http://127.0.0.1:{server.server_address[1]}"
                    self.assertEqual(discover_server_url(directory=directory / "app"), expected)
                    status, headers, payload = self.request(server, "/api/lists")
                    self.assertEqual(status, 200)
                    self.assertEqual([item["id"] for item in json.loads(payload)["lists"]],
                                     [directory.name])
                    self.assertNotIn("access-control-allow-origin", {key.lower(): value for key, value in headers})
                    self.assertEqual(self.request(server, "/api/lists", headers={"Host": "foreign"})[0], 403)

    def test_restart_reuses_port_but_stopped_record_is_not_discovered(self):
        directory = self.account("restart")
        with self.server(directory) as server, serving(server):
            address = server.server_address
            self.assertIsNotNone(discover_server_url(directory=directory / "app"))
        self.assertIsNone(discover_server_url(directory=directory / "app"))
        with self.server(directory) as restarted:
            self.assertEqual(restarted.server_address, address)
        record = directory / "app" / "server" / "endpoint.json"
        self.assertEqual(record.stat().st_mode & 0o777, 0o600)
        self.assertEqual(record.parent.stat().st_mode & 0o777, 0o700)

    def test_occupied_saved_port_is_not_contacted_and_explicit_port_fails(self):
        directory = self.account("occupied")
        with self.server(directory) as server:
            address = server.server_address
        with socket.socket() as occupant:
            occupant.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            occupant.bind(address)
            occupant.listen()
            occupant.setblocking(False)
            with self.server(directory) as replacement:
                self.assertNotEqual(replacement.server_address, address)
            with self.assertRaises(OSError):
                with self.server(directory, port=address[1]):
                    self.fail("Explicit occupied port was accepted")
            with self.assertRaises(BlockingIOError):
                occupant.accept()

    def test_second_instance_cannot_replace_the_same_accounts_record(self):
        directory = self.account("locked")
        with self.server(directory):
            record = directory / "app" / "server" / "endpoint.json"
            original = record.read_bytes()
            with self.assertRaisesRegex(OSError, "already running"):
                with self.server(directory):
                    self.fail("Second instance started")
            self.assertEqual(record.read_bytes(), original)

    def test_unsafe_record_is_rejected_before_reading_or_binding(self):
        directory = self.account("unsafe")
        with self.server(directory):
            pass
        record = directory / "app" / "server" / "endpoint.json"
        record.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "private"):
            with self.server(directory):
                self.fail("Unsafe record was accepted")
        self.assertIsNone(discover_server_url(directory=directory / "app"))
        record.unlink()
        outside = self.root / "not-endpoint-data"
        outside.write_text("do not read")
        record.symlink_to(outside)
        with self.assertRaises(OSError):
            with self.server(directory):
                self.fail("Symlink was accepted")
        self.assertIsNone(discover_server_url(directory=directory / "app"))

    def test_invalid_port_fails_before_creating_account_state(self):
        directory = self.account("invalid")
        for port in (0, -1, 65536, True):
            with self.subTest(port=port), self.assertRaises(ValueError):
                with self.server(directory, port=port):
                    self.fail("Invalid port was accepted")
        self.assertFalse((directory / "app").exists())

    def test_present_record_with_missing_lock_cannot_fall_back_to_legacy_service(self):
        directory = self.account("missing-lock")
        application = directory / "app"
        with self.server(directory) as server, serving(server):
            (application / "version").write_text("0.5.2\n")
            (application / "server" / "lock").unlink()
            command = endpoint.run_command

            def service_command(arguments, **kwargs):
                if arguments[:2] == ["/bin/launchctl", "list"]:
                    output = f"PID\tStatus\tLabel\n{os.getpid()}\t0\tlocal.live-gantt\n"
                    return subprocess.CompletedProcess(arguments, 0, output.encode())
                if arguments[0] == "systemctl":
                    return subprocess.CompletedProcess(arguments, 0, f"{os.getpid()}\n".encode())
                return command(arguments, **kwargs)

            with patch.object(endpoint, "run_command", side_effect=service_command), \
                    patch.object(endpoint, "DEFAULT_PORT", server.server_address[1]):
                self.assertIsNone(discover_server_url(directory=application))
