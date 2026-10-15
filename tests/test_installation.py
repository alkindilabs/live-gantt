from pathlib import Path
import plistlib
import unittest

from live_gantt.installation import (
    installation_directory,
    installation_settings,
    launchd_configuration,
    service_python,
    systemd_configuration,
)


ROOT = Path(__file__).resolve().parents[1]


class InstallationTests(unittest.TestCase):
    def test_service_python_keeps_the_path_found_unresolved(self):
        found = "/opt/homebrew/bin/python3"
        running = "/opt/homebrew/Cellar/python@3.14/3.14.6/bin/python3.14"
        self.assertEqual(service_python(lambda _name: found, running), Path(found))
        self.assertEqual(service_python(lambda _name: None, running), Path(running))

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

    def test_source_configuration_survives_an_upgrade_without_the_variable(self):
        previous = {"CLAUDE_CONFIG_DIR": "/sources/previous"}
        self.assertEqual(installation_settings({}, previous), previous)
        updated = installation_settings({"CLAUDE_CONFIG_DIR": "/sources/current"}, previous)
        self.assertEqual(updated, {"CLAUDE_CONFIG_DIR": "/sources/current"})
        self.assertEqual(installation_settings({}, {}), {})
        self.assertEqual(installation_settings({"CLAUDE_CONFIG_DIR": ""}, previous), {})
        self.assertEqual(previous, {"CLAUDE_CONFIG_DIR": "/sources/previous"})

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
