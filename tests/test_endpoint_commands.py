from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import platform
import selectors
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from live_gantt import installation
from live_gantt import endpoint
from live_gantt.endpoint import account_server, discover_server_url
from tests.test_endpoint import serving


REPOSITORY = Path(__file__).resolve().parents[1]
PROCESS_WAIT_SECONDS = 8


class EndpointCommandTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.environment = {**os.environ, "HOME": str(self.home),
                            "XDG_DATA_HOME": str(self.home / "data"),
                            "XDG_CONFIG_HOME": str(self.home / "config"),
                            "CLAUDE_CONFIG_DIR": str(self.home / "claude"),
                            "CODEX_HOME": str(self.home / "codex")}
        self.application = installation.installation_directory(platform.system(), self.home, self.environment)

    def start(self, arguments, cwd=REPOSITORY, environment=None):
        process = subprocess.Popen(arguments, cwd=cwd, env=environment or self.environment,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.stop, process)
        return process

    def stop(self, process):
        if process.poll() is None:
            process.terminate()
        try:
            process.communicate(timeout=PROCESS_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=PROCESS_WAIT_SECONDS)

    def first_line(self, process):
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            self.assertTrue(selector.select(PROCESS_WAIT_SECONDS), "server did not report its startup")
        line = process.stdout.readline().strip()
        if not line:
            self.fail(process.communicate(timeout=PROCESS_WAIT_SECONDS)[1])
        return line

    def test_foreground_publishes_actual_port_and_open_command_finds_it(self):
        with account_server(self.home / "claude", directory=self.application,
                            codex_root=self.home / "codex", usage_root=self.home / "usage") as seed:
            port = seed.server_address[1]
        process = self.start([sys.executable, "-B", "-m", "live_gantt", "--port", str(port)])
        expected = f"http://127.0.0.1:{port}"
        self.assertEqual(self.first_line(process), f"Live Gantt: {expected}")
        self.assertEqual(discover_server_url(directory=self.application), expected)
        result = subprocess.run(
            ["/usr/bin/python3" if Path("/usr/bin/python3").exists() else sys.executable,
             "-B", "-m", "live_gantt.python_support", "live_gantt.open"],
            cwd=REPOSITORY, env=self.environment, text=True, capture_output=True,
            timeout=PROCESS_WAIT_SECONDS, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f"Live Gantt: {expected}")

    def test_unavailable_open_command_reports_failure_without_success_url(self):
        result = subprocess.run(
            [sys.executable, "-B", "-m", "live_gantt.python_support", "live_gantt.open"],
            cwd=REPOSITORY, env=self.environment, text=True, capture_output=True,
            timeout=PROCESS_WAIT_SECONDS, check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("this account", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_session_start_reports_verified_account_address(self):
        with account_server(self.home / "claude", directory=self.application,
                            codex_root=self.home / "codex", usage_root=self.home / "usage") as server, serving(server):
            result = subprocess.run(
                [sys.executable, "-B", str(REPOSITORY / "hooks/session_start.py")],
                env=self.environment, text=True, capture_output=True, timeout=PROCESS_WAIT_SECONDS,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            response = json.loads(result.stdout)["hookSpecificOutput"]
            self.assertEqual(response["hookEventName"], "SessionStart")
            self.assertIn(f"http://127.0.0.1:{server.server_address[1]}", response["additionalContext"])

    def test_installer_reports_only_the_address_of_the_started_service(self):
        run = subprocess.run
        started = []

        def service_command(arguments, **kwargs):
            if arguments[0] not in {"launchctl", "systemctl"}:
                return run(arguments, **kwargs)
            if arguments[1] == "bootstrap" or "enable" in arguments:
                process = self.start([sys.executable, "-B", "-m", "live_gantt"], cwd=self.application)
                started.append(self.first_line(process))
            return subprocess.CompletedProcess(arguments, 1 if arguments[1] == "print" else 0)

        output = io.StringIO()
        with account_server(self.home / "unused", directory=self.home / "occupied",
                            codex_root=self.home / "unused-codex", usage_root=self.home / "unused-usage"), \
                patch.dict(os.environ, self.environment, clear=True), \
                patch.object(sys, "argv", ["installation", "install"]), \
                patch("live_gantt.installation.subprocess.run", side_effect=service_command), \
                redirect_stdout(output):
            installation.main()
        self.assertEqual(len(started), 1)
        address = discover_server_url(directory=self.application)
        self.assertIsNotNone(address)
        self.assertEqual(started, [f"Live Gantt: {address}"])
        self.assertEqual(output.getvalue().strip(), f"Live Gantt installed: {address}")

    def test_failed_service_readiness_never_prints_installation_success(self):
        run = subprocess.run

        def unavailable_service(arguments, **kwargs):
            if arguments[0] in {"launchctl", "systemctl"}:
                return subprocess.CompletedProcess(arguments, 1 if arguments[1] == "print" else 0)
            return run(arguments, **kwargs)

        output, diagnostic = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, self.environment, clear=True), \
                patch.object(sys, "argv", ["installation", "install"]), \
                patch("live_gantt.installation.subprocess.run", side_effect=unavailable_service), \
                patch.object(endpoint, "run_command", return_value=subprocess.CompletedProcess([], 1, b"")), \
                patch.object(endpoint, "STARTUP_WAIT_SECONDS", 0), \
                redirect_stdout(output), redirect_stderr(diagnostic), self.assertRaises(SystemExit) as result:
            installation.main()
        self.assertNotEqual(result.exception.code, 0)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("no running server could be verified for this account", diagnostic.getvalue())
