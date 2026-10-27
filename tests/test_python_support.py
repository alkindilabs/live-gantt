import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from live_gantt import python_support


REPOSITORY = Path(__file__).resolve().parents[1]
SYSTEM_PYTHON = "/usr/bin/python3"
PROBE_EXIT = 17
PROBE = """import json, os, sys, tomllib
print(json.dumps({"args": sys.argv[1:], "cwd": os.getcwd(),
                  "environment": os.environ["BOOTSTRAP_FIXTURE"],
                  "stdin": sys.stdin.read(), "version": list(sys.version_info[:2])}))
sys.exit(17)
"""


class PythonBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="live gantt bootstrap ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.package = self.root / "live_gantt"
        self.package.mkdir()
        (self.package / "__init__.py").write_text("")
        shutil.copyfile(REPOSITORY / "live_gantt/python_support.py", self.package / "python_support.py")
        (self.package / "probe.py").write_text(PROBE)
        self.cwd = self.root / "working directory"
        self.cwd.mkdir()
        self.environment = {**os.environ, "PYTHONPATH": str(self.root),
                            "BOOTSTRAP_FIXTURE": "preserved value"}
        self.arguments = ["", "two words", "literal'quote", '"double"', "$(exit 9)", "--flag"]

    def run_bootstrap(self, executable):
        return subprocess.run(
            [executable, "-B", "-m", "live_gantt.python_support", "live_gantt.probe", *self.arguments],
            cwd=self.cwd, env=self.environment, input="unread standard input\n", text=True,
            capture_output=True, check=False, timeout=10,
        )

    def assert_probe(self, result, arguments, cwd):
        self.assertEqual(result.returncode, PROBE_EXIT, result.stderr)
        self.assertEqual(result.stderr, "")
        reported = json.loads(result.stdout)
        self.assertEqual(reported["args"], arguments)
        self.assertEqual(reported["cwd"], str(cwd))
        self.assertEqual(reported["environment"], "preserved value")
        self.assertEqual(reported["stdin"], "unread standard input\n")
        self.assertGreaterEqual(tuple(reported["version"]), python_support.MINIMUM_PYTHON)

    def test_supported_interpreter_preserves_process_contract_and_module_exit(self):
        self.assert_probe(self.run_bootstrap(sys.executable), self.arguments, self.cwd)

    def test_system_python_hands_off_before_importing_a_supported_only_module(self):
        if not Path(SYSTEM_PYTHON).exists():
            self.skipTest("system Python is unavailable")
        version = subprocess.run(
            [SYSTEM_PYTHON, "-c", "import sys; print(*sys.version_info[:2])"],
            text=True, capture_output=True, check=False,
        )
        if version.returncode or tuple(map(int, version.stdout.split())) >= python_support.MINIMUM_PYTHON:
            self.skipTest("system Python is not an older interpreter")
        if not any(python_support.supported(candidate) for candidate in python_support.NEWER_PYTHONS):
            self.skipTest("no supported interpreter at the bootstrap locations")
        self.assert_probe(self.run_bootstrap(SYSTEM_PYTHON), self.arguments, self.cwd)

    def test_installer_scripts_bootstrap_before_importing_installation(self):
        (self.package / "installation.py").write_text(PROBE)
        scripts = self.root / "scripts"
        scripts.mkdir()
        binaries = self.root / "bin"
        binaries.mkdir()
        python = (SYSTEM_PYTHON if Path(SYSTEM_PYTHON).exists()
                  and not python_support.supported(SYSTEM_PYTHON) else sys.executable)
        (binaries / "python3").symlink_to(python)
        for action in ("install", "uninstall"):
            with self.subTest(action=action):
                destination = scripts / f"{action}.sh"
                shutil.copyfile(REPOSITORY / "scripts" / f"{action}.sh", destination)
                result = subprocess.run(
                    ["/bin/sh", str(destination)], cwd=self.cwd,
                    env={**self.environment, "PATH": str(binaries) + os.pathsep + "/usr/bin:/bin"},
                    input="unread standard input\n", text=True, capture_output=True, check=False,
                )
                self.assert_probe(result, [action], self.root)

    def test_missing_supported_python_reports_failure_without_importing_target(self):
        result = subprocess.run(
            [sys.executable, "-B", "-c",
             "import sys; from live_gantt import python_support as p; "
             "p.NEWER_PYTHONS = (); sys.version_info = (3, 9); "
             "sys.exit(p.main(['live_gantt.probe']))"],
            cwd=self.cwd, env=self.environment, capture_output=True, text=True, check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("Python 3.11 or newer", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_missing_module_argument_reports_usage(self):
        result = subprocess.run(
            [sys.executable, "-B", "-m", "live_gantt.python_support"],
            cwd=self.cwd, env=self.environment, capture_output=True, text=True, check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("usage:", result.stderr)

    def test_execution_failure_reports_the_operating_system_error(self):
        with patch("os.execv", side_effect=PermissionError("execution denied by fixture")):
            with patch("sys.stderr") as stderr:
                status = python_support.main(["live_gantt.probe", "--flag"])
        self.assertNotEqual(status, 0)
        self.assertIn("execution denied by fixture", "".join(call.args[0] for call in stderr.write.call_args_list))

    def test_hook_without_supported_python_stays_quiet_and_successful(self):
        with patch.object(sys, "version_info", (3, 9)), patch.object(python_support, "NEWER_PYTHONS", ()):
            with self.assertRaises(SystemExit) as exit_result:
                python_support.require_supported_python()
        self.assertEqual(exit_result.exception.code, 0)

    def test_hook_execution_failure_stays_successful(self):
        with patch.object(sys, "version_info", (3, 9)), \
                patch.object(python_support, "NEWER_PYTHONS", (sys.executable,)), \
                patch("os.execv", side_effect=OSError("interpreter disappeared")):
            with self.assertRaises(SystemExit) as exit_result:
                python_support.require_supported_python()
        self.assertEqual(exit_result.exception.code, 0)


class PythonCandidateTests(unittest.TestCase):
    def test_executable_with_an_invalid_format_is_unavailable(self):
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "python3"
            executable.write_text("invalid executable format\n")
            executable.chmod(0o755)
            self.assertFalse(python_support.supported(str(executable)))

    def test_hung_version_probe_is_bounded(self):
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "python3"
            executable.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(2)\n")
            executable.chmod(0o755)
            try:
                result = subprocess.run(
                    [sys.executable, "-B", "-c",
                     "import sys; from live_gantt.python_support import supported; "
                     "sys.exit(1 if supported(sys.argv[1]) else 0)", str(executable)],
                    cwd=REPOSITORY, capture_output=True, text=True, timeout=3, check=False,
                )
            except subprocess.TimeoutExpired:
                self.fail("interpreter probe did not finish within three seconds")
            self.assertEqual(result.returncode, 0, result.stderr)
