import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPOSITORY = Path(__file__).resolve().parents[1]
HOOKS = REPOSITORY / "hooks"


class TaskCreatedHookTests(unittest.TestCase):
    def run_hook(self, document):
        result = subprocess.run(
            [sys.executable, "-B", str(HOOKS / "task_created.py")],
            input=document,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return result.stdout

    def task_created(self, metadata):
        return json.dumps({
            "hook_event_name": "PostToolUse",
            "tool_name": "TaskCreate",
            "tool_input": {"subject": "Build release", "metadata": metadata},
            "tool_response": {"task": {"id": "17", "subject": "Build release"}},
        })

    def assert_reminder(self, metadata, missing_fields):
        output = json.loads(self.run_hook(self.task_created(metadata)))
        self.assertEqual(set(output), {"hookSpecificOutput"})
        specific = output["hookSpecificOutput"]
        self.assertEqual(set(specific), {"hookEventName", "additionalContext"})
        self.assertEqual(specific["hookEventName"], "PostToolUse")
        context = specific["additionalContext"]
        self.assertIn("Build release", context)
        self.assertIn("#17", context)
        self.assertIn("cannot schedule", context)
        self.assertIn("live-gantt skill", context)
        self.assertIn("TaskUpdate", context)
        for field in missing_fields:
            self.assertIn(f"`{field}`", context)
        for field in {"estimate", "lane"} - set(missing_fields):
            self.assertNotIn(f"`{field}`", context)

    def test_complete_task_is_silent(self):
        output = self.run_hook(self.task_created({
            "estimate": {"low": 1, "likely": 2, "high": 3}, "lane": "Build",
        }))
        self.assertEqual(output, "")

    def test_missing_estimate_names_task_and_requests_only_estimate(self):
        self.assert_reminder({"lane": "Build"}, ["estimate"])

    def test_missing_lane_names_task_and_requests_only_lane(self):
        self.assert_reminder({"estimate": {"low": 1, "likely": 2, "high": 3}}, ["lane"])

    def test_absent_metadata_requests_both_fields(self):
        self.assert_reminder({}, ["estimate", "lane"])

    def test_null_fields_are_missing(self):
        self.assert_reminder({"estimate": None, "lane": None}, ["estimate", "lane"])

    def test_malformed_input_is_silent_and_never_blocks(self):
        event = json.loads(self.task_created({}))
        documents = [
            "", "{not json", "null", "[]", "17",
            json.dumps({**event, "tool_input": []}),
            json.dumps({**event, "tool_input": {"metadata": []}}),
            json.dumps({**event, "tool_input": None}),
            json.dumps({**event, "tool_response": None}),
            json.dumps({**event, "tool_response": {"task": []}}),
            json.dumps({**event, "tool_response": {"task": {"id": 17}}}),
            json.dumps({**event, "tool_name": "TaskUpdate"}),
        ]
        for document in documents:
            with self.subTest(document=document):
                self.assertEqual(self.run_hook(document), "")


class SessionStartHookTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.plugin_root = self.root / "plugin"
        self.manifest = self.plugin_root / ".claude-plugin" / "plugin.json"
        self.manifest.parent.mkdir(parents=True)
        self.manifest.write_text(json.dumps({"version": "0.10.0"}))
        self.installed_root = self.root / "installed"

    def test_only_older_installed_version_runs_the_installer(self):
        from hooks.session_start import update_installation
        from live_gantt.installation import VERSION_FILENAME

        self.installed_root.mkdir()
        installed_version = self.installed_root / VERSION_FILENAME
        versions = {
            "0.9.9": True,
            "0.10.0": False,
            "0.11.0": False,
            "1.0.0": False,
            "invalid": False,
            "0.9": False,
            "0.9.9-beta": False,
            "": False,
        }
        for version, should_install in versions.items():
            with self.subTest(version=version):
                installed_version.write_text(version + "\n")
                with patch("hooks.session_start.subprocess.run") as run:
                    update_installation(self.plugin_root, self.installed_root)
                self.assertEqual(run.call_count, int(should_install))
                if should_install:
                    self.assertEqual(run.call_args.args[0], [
                        sys.executable, "-B", "-m", "live_gantt.installation", "install",
                    ])
                    self.assertEqual(run.call_args.kwargs["cwd"], self.plugin_root)
                    self.assertEqual(run.call_args.kwargs["stdout"], subprocess.DEVNULL)
                    self.assertEqual(run.call_args.kwargs["stderr"], subprocess.DEVNULL)

    def test_no_installed_copy_never_runs_the_installer(self):
        from hooks.session_start import update_installation

        with patch("hooks.session_start.subprocess.run") as run:
            update_installation(self.plugin_root, self.installed_root)
        run.assert_not_called()

    def test_invalid_plugin_version_never_runs_the_installer(self):
        from hooks.session_start import update_installation
        from live_gantt.installation import VERSION_FILENAME

        self.installed_root.mkdir()
        (self.installed_root / VERSION_FILENAME).write_text("0.1.0")
        for manifest in ('{"version":"not-a-version"}', '{"version":null}', "{", "[]"):
            with self.subTest(manifest=manifest):
                self.manifest.write_text(manifest)
                with patch("hooks.session_start.subprocess.run") as run:
                    update_installation(self.plugin_root, self.installed_root)
                run.assert_not_called()

    def test_installer_failure_does_not_interrupt_session(self):
        from hooks.session_start import update_installation
        from live_gantt.installation import VERSION_FILENAME

        self.installed_root.mkdir()
        (self.installed_root / VERSION_FILENAME).write_text("0.9.0")
        with patch("hooks.session_start.subprocess.run", side_effect=OSError("unavailable")):
            update_installation(self.plugin_root, self.installed_root)

    def test_script_is_silent_without_installation(self):
        result = subprocess.run(
            [sys.executable, "-B", str(HOOKS / "session_start.py")],
            cwd=self.root,
            env={**os.environ, "HOME": str(self.root), "XDG_DATA_HOME": str(self.root / "data")},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")
        self.assertEqual(list(self.root.iterdir()), [self.plugin_root])
