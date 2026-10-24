from pathlib import Path
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from live_gantt import installation
from live_gantt.installation import (
    installation_directory,
    installation_settings,
    install,
    launchd_configuration,
    service_python,
    systemd_configuration,
    uninstall,
)


ROOT = Path(__file__).resolve().parents[1]
RUNNING_PYTHON = "/opt/homebrew/Cellar/python@3.14/3.14.6/bin/python3.14"
SUPPORTED_EXIT = 0
UNSUPPORTED_EXIT = 1


def linked_python3(directory, version_check_exit):
    """A `python3` symlink, as a package manager installs one, to a stand-in interpreter that
    answers the supported-version check with `version_check_exit`."""
    target = Path(directory) / "Cellar" / "python3.x"
    target.parent.mkdir()
    target.write_text(f"#!/bin/sh\nexit {version_check_exit}\n")
    target.chmod(0o755)
    link = Path(directory) / "bin" / "python3"
    link.parent.mkdir()
    link.symlink_to(target)
    return str(link)


class InstalledCodexCommandTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="live gantt installation ")
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve() / "user home"
        self.home.mkdir()
        self.plugin = Path(self.temporary.name) / "plugin source"
        self.plugin.mkdir()
        for name in (".claude-plugin", "launchd", "systemd"):
            shutil.copytree(ROOT / name, self.plugin / name)
        self.package = self.plugin / "live_gantt"
        self.package.mkdir()
        (self.package / "__init__.py").write_text("")
        self.write_launcher("first")
        self.command = self.home / ".local/bin/live-gantt-codex"
        self.environment = {"PATH": "", "HOME": str(self.home)}

    def write_launcher(self, version):
        (self.package / "codex_launcher.py").write_text(
            "import json, os, sys\n"
            f"print(json.dumps({{'version': {version!r}, 'args': sys.argv[1:], "
            "'pythonpath': os.environ['PYTHONPATH'], 'cwd': os.getcwd()}))\n"
            "sys.exit(17)\n"
        )

    def change_installation(self, action):
        with patch("live_gantt.installation.platform.system", return_value="Linux"), \
                patch("live_gantt.installation.Path.home", return_value=self.home), \
                patch.dict(os.environ, self.environment, clear=True), \
                patch("live_gantt.installation.subprocess.run",
                      return_value=subprocess.CompletedProcess([], 0)):
            action(self.plugin)

    def test_install_and_upgrade_create_an_executable_stable_command(self):
        self.change_installation(install)
        self.assertTrue(self.command.is_file())
        self.assertTrue(os.access(self.command, os.X_OK))
        first = subprocess.run([str(self.command)], capture_output=True, text=True)
        self.assertEqual(first.returncode, 17, first.stderr)
        self.assertEqual(json.loads(first.stdout)["version"], "first")
        self.write_launcher("upgraded")
        self.change_installation(install)
        upgraded = subprocess.run([str(self.command)], capture_output=True, text=True)
        self.assertEqual(upgraded.returncode, 17, upgraded.stderr)
        self.assertEqual(json.loads(upgraded.stdout)["version"], "upgraded")

    def test_command_preserves_arguments_and_uses_stable_paths_with_spaces(self):
        binaries = self.home / "python binaries"
        binaries.mkdir()
        (binaries / "python3").symlink_to(sys.executable)
        self.environment["PATH"] = str(binaries)
        self.change_installation(install)
        self.assertTrue(self.command.is_file())
        hostile_directory = self.home / "untrusted project"
        hostile_package = hostile_directory / "live_gantt"
        hostile_package.mkdir(parents=True)
        (hostile_package / "__init__.py").write_text("raise RuntimeError('untrusted package')\n")
        args = ["", "two words", "literal'quote", '"double"', "$(exit 9)", "--flag"]
        result = subprocess.run(
            [str(self.command), *args], cwd=hostile_directory,
            env={**os.environ, "PYTHONPATH": str(hostile_directory)},
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 17, result.stderr)
        reported = json.loads(result.stdout)
        self.assertEqual(reported["args"], args)
        self.assertEqual(reported["cwd"], str(hostile_directory))
        self.assertEqual(reported["pythonpath"], str(self.home / ".local/share/live-gantt"))

    def assert_conflicting_command_is_preserved(self, kind):
        self.command.parent.mkdir(parents=True)
        unrelated = self.home / "unrelated command"
        unrelated.write_text("user-owned content")
        if kind == "file":
            self.command.write_text("user-owned content")
        elif kind == "symlink":
            self.command.symlink_to(unrelated)
        else:
            self.command.mkdir()
        with self.assertRaisesRegex(FileExistsError, "live-gantt-codex"):
            self.change_installation(install)
        self.assertFalse((self.home / ".local/share/live-gantt").exists())
        self.assertEqual(unrelated.read_text(), "user-owned content")
        if kind == "directory":
            self.assertTrue(self.command.is_dir())
        else:
            self.assertEqual(self.command.read_text(), "user-owned content")
            self.assertEqual(self.command.is_symlink(), kind == "symlink")

    def test_install_rejects_an_unrelated_command_file(self):
        self.assert_conflicting_command_is_preserved("file")

    def test_install_rejects_a_binary_command_without_decoding_it(self):
        self.command.parent.mkdir(parents=True)
        content = b"\xff\xfe user binary"
        self.command.write_bytes(content)
        with self.assertRaisesRegex(FileExistsError, "live-gantt-codex"):
            self.change_installation(install)
        self.assertEqual(self.command.read_bytes(), content)

    def test_install_rejects_a_command_symlink(self):
        self.assert_conflicting_command_is_preserved("symlink")

    def test_install_rejects_a_command_directory(self):
        self.assert_conflicting_command_is_preserved("directory")

    def test_uninstall_removes_only_the_owned_command(self):
        self.change_installation(install)
        self.assertTrue(self.command.exists())
        self.change_installation(uninstall)
        self.assertFalse(self.command.exists())
        self.command.write_text("user-owned content")
        self.change_installation(uninstall)
        self.assertEqual(self.command.read_text(), "user-owned content")
        self.command.unlink()
        unrelated = self.home / "unrelated command"
        unrelated.write_text("user-owned content")
        self.command.symlink_to(unrelated)
        self.change_installation(uninstall)
        self.assertTrue(self.command.is_symlink())
        self.assertEqual(unrelated.read_text(), "user-owned content")


class InstallationTests(unittest.TestCase):
    def test_service_python_keeps_a_supported_python3_on_path_unresolved(self):
        with tempfile.TemporaryDirectory() as directory:
            found = linked_python3(directory, SUPPORTED_EXIT)
            self.assertEqual(service_python(lambda _name: found, RUNNING_PYTHON), Path(found))
        self.assertEqual(service_python(lambda _name: None, RUNNING_PYTHON), Path(RUNNING_PYTHON))

    def test_service_python_passes_over_an_older_python3_found_first_on_path(self):
        with tempfile.TemporaryDirectory() as directory:
            found = linked_python3(directory, UNSUPPORTED_EXIT)
            self.assertEqual(service_python(lambda _name: found, RUNNING_PYTHON), Path(RUNNING_PYTHON))

    def test_installation_directory_by_os_and_xdg_setting(self):
        home = Path("/users/reader")
        cases = (
            ("Darwin", {"XDG_DATA_HOME": "/ignored"},
             home / "Library" / "Application Support" / "live-gantt"),
            ("Linux", {}, home / ".local" / "share" / "live-gantt"),
            ("Linux", {"XDG_DATA_HOME": "/data"}, Path("/data/live-gantt")),
            ("Linux", {"XDG_DATA_HOME": "relative"},
             home / ".local" / "share" / "live-gantt"),
            ("Linux", {"XDG_DATA_HOME": ""},
             home / ".local" / "share" / "live-gantt"),
        )
        for system, environ, expected in cases:
            with self.subTest(system=system, environ=environ):
                self.assertEqual(installation_directory(system, home, environ), expected)
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            installation_directory("Windows", home, {})

    def test_variables_of_removed_features_are_not_carried_into_the_service(self):
        previous = {"CODEX_HOME": "/sources/codex", "LIVE_GANTT_CODEX_BIN": "/stable/bin/codex",
                    "LIVE_GANTT_LIVE_USAGE": "1", "LIVE_GANTT_CLAUDE_BIN": "/old/claude"}
        self.assertEqual(installation_settings({}, previous),
                         {"CODEX_HOME": "/sources/codex", "LIVE_GANTT_CODEX_BIN": "/stable/bin/codex"})

    def test_source_configuration_survives_an_upgrade_without_the_variable(self):
        previous = {"CLAUDE_CONFIG_DIR": "/sources/previous"}
        self.assertEqual(installation_settings({}, previous), previous)
        updated = installation_settings({"CLAUDE_CONFIG_DIR": "/sources/current"}, previous)
        self.assertEqual(updated, {"CLAUDE_CONFIG_DIR": "/sources/current"})
        self.assertEqual(installation_settings({}, {}), {})
        self.assertEqual(installation_settings({"CLAUDE_CONFIG_DIR": ""}, previous), {})
        self.assertEqual(previous, {"CLAUDE_CONFIG_DIR": "/sources/previous"})

    def test_codex_and_claude_source_overrides_are_preserved_independently(self):
        previous = {"CLAUDE_CONFIG_DIR": "/sources/claude", "CODEX_HOME": "/sources/codex"}
        self.assertEqual(installation_settings({}, previous), previous)
        self.assertEqual(installation_settings({"CODEX_HOME": ""}, previous),
                         {"CLAUDE_CONFIG_DIR": "/sources/claude"})
        self.assertEqual(installation_settings({"CLAUDE_CONFIG_DIR": ""}, previous),
                         {"CODEX_HOME": "/sources/codex"})

    def test_launchd_runs_the_stable_copy_with_interactive_priorities(self):
        directory = Path("/users/reader/Library/Application Support/live-gantt")
        python = Path("/usr/local/bin/python3")
        log = Path("/users/reader/Library/Logs/live-gantt.log")
        config = launchd_configuration(
            ROOT, directory, python, log, {"CLAUDE_CONFIG_DIR": "/sources/claude"},
        )
        config = plistlib.loads(plistlib.dumps(config))
        self.assertEqual(config["ProgramArguments"], [str(python), "-B", "-m", "live_gantt"])
        self.assertEqual(config["WorkingDirectory"], str(directory))
        self.assertEqual(config["Nice"], 10)
        self.assertEqual(config["ProcessType"], "Standard")
        self.assertTrue(config["LowPriorityIO"])
        self.assertEqual(config["StandardOutPath"], str(log))
        self.assertEqual(config["StandardErrorPath"], str(log))
        self.assertEqual(config["EnvironmentVariables"]["CLAUDE_CONFIG_DIR"], "/sources/claude")

    def test_systemd_runs_the_stable_copy_with_non_idle_io(self):
        directory = Path('/users/reader/data 100%/live-gantt')
        python = Path('/users/reader/python $stable/bin/python3')
        config = systemd_configuration(
            ROOT, directory, python, {"CLAUDE_CONFIG_DIR": '/sources/claude "custom"'},
        )
        self.assertIn('WorkingDirectory="/users/reader/data 100%%/live-gantt"', config)
        self.assertIn('ExecStart=:"/users/reader/python $stable/bin/python3" -B -m live_gantt', config)
        self.assertIn('Environment="CLAUDE_CONFIG_DIR=/sources/claude \\"custom\\""', config)
        self.assertIn("Nice=10", config)
        self.assertIn("IOSchedulingClass=best-effort", config)
        self.assertIn("IOSchedulingPriority=7", config)
        self.assertIn("WantedBy=default.target", config)
        self.assertNotIn("@", config)


if __name__ == "__main__":
    unittest.main()
