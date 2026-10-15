from datetime import datetime, timedelta, timezone
import json
import os
import platform
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


AUDITED_RUN = """
import runpy
import sys

watched, script = sys.argv[1], sys.argv[2]


def report_reads(event, arguments):
    if event in ("open", "os.listdir", "os.scandir") and arguments and str(arguments[0]).startswith(watched):
        sys.stderr.write(f"{event} {arguments[0]}\\n")


sys.addaudithook(report_reads)
sys.argv = [script]
runpy.run_path(script, run_name="__main__")
"""
SESSION_ID = "11111111-1111-4111-8111-111111111111"


class FreshnessHookTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = self.root / "claude"
        self.data = self.root / "plugin-data"
        self.task_dir = self.config / "tasks" / SESSION_ID
        self.task_dir.mkdir(parents=True)
        self.transcript = self.config / "projects" / "-example" / f"{SESSION_ID}.jsonl"
        self.transcript.parent.mkdir(parents=True)
        self.transcript.write_text('{"type":"user","message":{"content":"Work"}}\n')
        self.write_task({"estimate": {"low": 1, "likely": 2, "high": 3}, "lane": "Build"})
        self.environment = {**os.environ, "HOME": str(self.root), "XDG_DATA_HOME": str(self.root / "xdg"),
                            "CLAUDE_CONFIG_DIR": str(self.config), "CLAUDE_PLUGIN_DATA": str(self.data)}

    def write_task(self, metadata, status="in_progress"):
        (self.task_dir / "1.json").write_text(json.dumps({
            "id": "1", "subject": "Build release", "status": status,
            "blocks": [], "blockedBy": [], "metadata": metadata,
        }))

    def event(self, name, **fields):
        return json.dumps({"session_id": SESSION_ID, "transcript_path": str(self.transcript),
                           "hook_event_name": name, **fields})

    def run_hook(self, document, environment=None):
        result = subprocess.run(
            [sys.executable, "-B", str(HOOKS / "freshness.py")], input=document, text=True,
            capture_output=True, env=environment or self.environment, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return result.stdout

    def run_audited(self, document):
        result = subprocess.run(
            [sys.executable, "-B", "-c", AUDITED_RUN, str(self.task_dir), str(HOOKS / "freshness.py")],
            input=document, text=True, capture_output=True, env=self.environment, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout, result.stderr

    def sources_snapshot(self):
        return {path: path.read_bytes() for path in self.config.rglob("*") if path.is_file()}

    def test_stop_blocks_once_naming_each_finding_then_cools_down(self):
        before = self.sources_snapshot()
        output = json.loads(self.run_hook(self.event("Stop", stop_hook_active=False)))
        self.assertEqual(set(output), {"decision", "reason"})
        self.assertEqual(output["decision"], "block")
        self.assertIn("Task #1 is in progress without a numeric progress value.", output["reason"])
        self.assertIn("TaskUpdate", output["reason"])
        self.assertIn("live-gantt skill", output["reason"])
        self.assertEqual(self.run_hook(self.event("Stop", stop_hook_active=False)), "")
        self.assertEqual(self.sources_snapshot(), before)

    def test_stop_blocks_again_once_the_cooldown_has_passed(self):
        from hooks.freshness import BLOCK_COOLDOWN_MINUTES

        self.run_hook(self.event("Stop", stop_hook_active=False))
        state, = (self.data / "freshness").iterdir()
        recorded = json.loads(state.read_text())
        for minutes_ago, blocks in ((BLOCK_COOLDOWN_MINUTES - 0.5, False), (BLOCK_COOLDOWN_MINUTES, True)):
            with self.subTest(minutes_ago=minutes_ago):
                earlier = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
                state.write_text(json.dumps({**recorded, "blockedAt": earlier.isoformat()}))
                output = self.run_hook(self.event("Stop", stop_hook_active=False))
                self.assertEqual(bool(output), blocks)

    def test_stop_never_blocks_while_a_stop_hook_is_already_active(self):
        self.assertEqual(self.run_hook(self.event("Stop", stop_hook_active=True)), "")
        self.assertFalse(self.data.exists())

    def test_fresh_plan_never_blocks(self):
        self.write_task({"estimate": {"low": 1, "likely": 2, "high": 3}, "lane": "Build", "progress": 5})
        self.assertEqual(self.run_hook(self.event("Stop", stop_hook_active=False)), "")

    def test_unreadable_task_files_never_block(self):
        (self.task_dir / "1.json").write_text("{half written")
        self.assertEqual(self.run_hook(self.event("Stop", stop_hook_active=False)), "")
        (self.task_dir / "1.json").unlink()
        self.task_dir.rmdir()
        self.task_dir.write_text("not a directory")
        self.assertEqual(self.run_hook(self.event("Stop", stop_hook_active=False)), "")

    def test_malformed_events_are_silent(self):
        documents = ["", "{not json", "null", "[]",
                     json.dumps({"hook_event_name": "Stop", "session_id": "../outside"}),
                     json.dumps({"hook_event_name": "Stop", "session_id": 7}),
                     self.event("Stop", transcript_path=12, stop_hook_active=False)[:-1]]
        for document in documents:
            with self.subTest(document=document):
                self.assertEqual(self.run_hook(document), "")

    def test_unwritable_data_directory_never_blocks(self):
        self.data.write_text("not a directory")
        self.assertEqual(self.run_hook(self.event("Stop", stop_hook_active=False)), "")

    def test_state_defaults_to_the_installed_files_directory(self):
        from live_gantt.installation import installation_directory

        environment = {key: value for key, value in self.environment.items() if key != "CLAUDE_PLUGIN_DATA"}
        self.assertTrue(self.run_hook(self.event("Stop", stop_hook_active=False), environment))
        installed = installation_directory(platform.system(), self.root, environment)
        self.assertEqual([path.name for path in (installed / "freshness").iterdir()], [f"{SESSION_ID}.json"])

    def test_prompt_submission_adds_context_without_blocking(self):
        for _ in range(2):
            output = json.loads(self.run_hook(self.event("UserPromptSubmit", prompt="Next")))
            self.assertEqual(set(output), {"hookSpecificOutput"})
            specific = output["hookSpecificOutput"]
            self.assertEqual(specific["hookEventName"], "UserPromptSubmit")
            self.assertIn("Task #1 is in progress without a numeric progress value.", specific["additionalContext"])

    def test_tool_use_reminds_once_then_reads_nothing_inside_the_cooldown(self):
        event = self.event("PostToolUse", tool_name="Bash", tool_input={}, tool_response={})
        output, reads = self.run_audited(event)
        specific = json.loads(output)["hookSpecificOutput"]
        self.assertEqual(specific["hookEventName"], "PostToolUse")
        self.assertIn("Task #1", specific["additionalContext"])
        self.assertNotIn("decision", json.loads(output))
        self.assertIn(str(self.task_dir), reads)
        self.assertEqual(self.run_audited(event), ("", ""))

    def test_hooks_are_registered_for_stop_prompts_and_every_tool(self):
        registration = json.loads((HOOKS / "hooks.json").read_text())["hooks"]
        command = 'python3 -B "${CLAUDE_PLUGIN_ROOT}/hooks/freshness.py"'
        for event, matcher in (("Stop", None), ("UserPromptSubmit", None), ("PostToolUse", "*")):
            with self.subTest(event=event):
                entries = [entry for entry in registration[event]
                           if any(hook["command"] == command for hook in entry["hooks"])]
                self.assertEqual(len(entries), 1)
                self.assertEqual(entries[0].get("matcher"), matcher)
