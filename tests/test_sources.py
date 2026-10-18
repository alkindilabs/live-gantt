import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from live_gantt.sources import PROMPT_TITLE_CHARACTERS, SourceStore


class SourceStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.list_id = "session-one"
        self.task_dir = self.root / "tasks" / self.list_id
        self.task_dir.mkdir(parents=True)
        self.project_dir = self.root / "projects" / "-example-project"
        self.project_dir.mkdir(parents=True)
        self.transcript = self.project_dir / f"{self.list_id}.jsonl"
        self.now = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)
        self.store = SourceStore(self.root)

    def task(self, task_id="1", **values):
        document = {
            "id": task_id,
            "subject": "Build the chart",
            "description": "Show live work",
            "status": "in_progress",
            "metadata": {},
            "blocks": [],
            "blockedBy": [],
        }
        document.update(values)
        (self.task_dir / f"{task_id}.json").write_text(json.dumps(document))

    def append(self, *records, path=None):
        with (path or self.transcript).open("a") as stream:
            for record in records:
                stream.write(json.dumps(record) + "\n")

    def tool(self, timestamp, name, inputs, tool_id="tool-1"):
        return {
            "timestamp": timestamp,
            "type": "assistant",
            "message": {"content": [{
                "type": "tool_use", "id": tool_id, "name": name, "input": inputs,
            }]},
        }

    def result(self, timestamp, content, tool_id="tool-1"):
        return {
            "timestamp": timestamp,
            "type": "user",
            "message": {"content": [{
                "type": "tool_result", "tool_use_id": tool_id, "content": content,
            }]},
        }

    def state(self):
        return self.store.state(self.list_id, self.now)

    def test_task_lifecycle_skips_malformed_lines_and_uses_first_start_last_end(self):
        self.task(status="completed")
        self.append(
            self.tool("2026-10-15T08:00:00Z", "TaskCreate", {"subject": "Build the chart"}),
            self.result("2026-10-15T08:00:01Z", "Task #1 created successfully: Build the chart"),
            self.tool("2026-10-15T09:00:00Z", "TaskUpdate", {"taskId": "1", "status": "in_progress"}),
        )
        with self.transcript.open("a") as stream:
            stream.write('{"timestamp":"2026-10-15T09:15:00Z","TaskUpdate": broken}\n')
        self.append(
            self.tool("2026-10-15T09:30:00Z", "TaskUpdate", {"taskId": "1", "metadata": {"progress": 40}}),
            self.tool("2026-10-15T10:00:00Z", "TaskUpdate", {"taskId": "1", "status": "completed"}),
            self.tool("2026-10-15T10:15:00Z", "TaskUpdate", {"taskId": "1", "status": "in_progress"}),
            self.tool("2026-10-15T11:00:00Z", "TaskUpdate", {"taskId": "1", "status": "completed"}),
        )
        task = self.state()["tasks"][0]
        self.assertEqual(task["created"], "2026-10-15T08:00:01Z")
        self.assertEqual(task["started"], "2026-10-15T09:00:00Z")
        self.assertEqual(task["ended"], "2026-10-15T11:00:00Z")
        self.assertEqual([entry["event"] for entry in task["history"]],
                         ["created", "started", "progress", "completed", "started", "completed"])
        self.assertEqual(task["history"][2]["value"], 40)

    def test_append_consumes_new_lines_once_and_retains_partial_line(self):
        self.task()
        self.append(self.tool("2026-10-15T09:00:00Z", "TaskUpdate", {"taskId": "1", "status": "in_progress"}))
        self.assertEqual(len(self.state()["tasks"][0]["history"]), 1)
        update = json.dumps(self.tool("2026-10-15T10:00:00Z", "TaskUpdate", {"taskId": "1", "metadata": {"progress": 60}}))
        split = len(update) // 2
        with self.transcript.open("a") as stream:
            stream.write(update[:split])
        self.assertEqual(len(self.state()["tasks"][0]["history"]), 1)
        with self.transcript.open("a") as stream:
            stream.write(update[split:] + "\n")
        history = self.state()["tasks"][0]["history"]
        self.assertEqual([entry["value"] for entry in history if entry["event"] == "progress"], [60])
        self.assertEqual(self.state()["tasks"][0]["history"], history)

    def test_truncation_discards_cached_history(self):
        self.task()
        self.append(
            self.tool("2026-10-15T09:00:00Z", "TaskUpdate", {"taskId": "1", "status": "in_progress"}),
            self.tool("2026-10-15T10:00:00Z", "TaskUpdate", {"taskId": "1", "metadata": {"progress": 90}}),
        )
        self.assertEqual(len(self.state()["tasks"][0]["history"]), 2)
        self.transcript.write_text("")
        self.append(self.tool("2026-10-15T11:00:00Z", "TaskUpdate", {"taskId": "1", "status": "in_progress"}))
        task = self.state()["tasks"][0]
        self.assertEqual(task["started"], "2026-10-15T11:00:00Z")
        self.assertEqual(len(task["history"]), 1)

    def test_complete_final_line_without_newline_is_read_once(self):
        self.task()
        start = self.tool("2026-10-15T09:00:00Z", "TaskUpdate", {"taskId": "1", "status": "in_progress"})
        self.transcript.write_text(json.dumps(start))
        self.assertEqual(self.state()["tasks"][0]["started"], "2026-10-15T09:00:00Z")
        with self.transcript.open("a") as stream:
            stream.write("\n" + json.dumps(self.tool("2026-10-15T10:00:00Z", "TaskUpdate", {"taskId": "1", "metadata": {"progress": 50}})))
        history = self.state()["tasks"][0]["history"]
        self.assertEqual([item["event"] for item in history], ["started", "progress"])
        self.assertEqual(self.state()["tasks"][0]["history"], history)

    def test_agent_notification_correlation_and_silent_boundary(self):
        self.task(metadata={"agents": ["finished-agent"]})
        agents_dir = self.project_dir / self.list_id / "subagents"
        agents_dir.mkdir(parents=True)
        for agent_id in ("finished-agent", "working-agent", "silent-agent"):
            (agents_dir / f"agent-{agent_id}.meta.json").write_text(json.dumps({"description": agent_id, "agentType": "general-purpose"}))
            timestamp = "2026-10-15T11:30:00Z" if agent_id == "silent-agent" else "2026-10-15T11:45:00Z"
            self.append(self.tool(timestamp, "Bash", {"description": f"Check {agent_id}"}), path=agents_dir / f"agent-{agent_id}.jsonl")
        self.append(
            self.tool("2026-10-15T11:00:00Z", "Agent", {"description": "Background work"}),
            self.result("2026-10-15T11:00:01Z", json.dumps({"task_id": "background-id", "agentId": "finished-agent"})),
            {"timestamp": "2026-10-15T11:50:00Z", "type": "user", "message": {"content": "<task-notification><task-id>background-id</task-id><status>completed</status></task-notification>"}},
        )
        state = self.state()
        assigned = state["tasks"][0]["agents"][0]
        self.assertEqual(assigned["status"], "completed")
        self.assertEqual(assigned["ended"], "2026-10-15T11:45:00Z")
        by_id = {agent["id"]: agent for agent in state["agents"]}
        self.assertEqual(by_id["working-agent"]["status"], "running")
        self.assertEqual(by_id["working-agent"]["activity"], "Check working-agent")
        self.assertEqual(by_id["silent-agent"]["status"], "silent")
        self.assertNotIn("finished-agent", by_id)

    def test_metadata_validation_live_journal_and_run_activity(self):
        journal = self.task_dir / "work.jsonl"
        journal.write_text('{"type":"result"}\n{"type": "result"}\n{"type":"other"}\n')
        log = self.task_dir / "worker.log"
        log.write_text("working")
        timestamp = datetime(2026, 10, 15, 11, 59, tzinfo=timezone.utc).timestamp()
        os.utime(log, (timestamp, timestamp))
        self.task(metadata={
            "weight": -1,
            "milestone": "First release",
            "progress": "bad",
            "estimate": {"low": 3, "likely": 2, "high": 1},
            "live": {"journal": "work.jsonl", "planned": 4, "from": 20, "to": 80},
            "runs": [{"label": "Worker", "started": "2026-10-15T11:00:00Z", "ended": None, "log": "worker.log"}],
            "unknown": "ignored",
        })
        task = self.state()["tasks"][0]
        self.assertEqual(task["weight"], 1)
        self.assertEqual(task["lane"], "First release")
        self.assertEqual(task["progress"], 0)
        self.assertEqual(task["liveProgress"], 50)
        self.assertIsNone(task["estimate"])
        self.assertEqual(len(task["warnings"]), 3)
        self.assertEqual(task["runs"][0]["lastActivityAt"], "2026-10-15T11:59:00Z")
        self.assertEqual(task["runs"][0]["status"], "running")
        journal.write_text('{"type":"result"}\n')
        self.assertEqual(self.state()["tasks"][0]["liveProgress"], 35)

    def test_tasks_record_file_change_time_readability_and_transcript_time(self):
        self.task("1")
        changed = datetime(2026, 10, 15, 11, 40, tzinfo=timezone.utc).timestamp()
        os.utime(self.task_dir / "1.json", (changed, changed))
        (self.task_dir / "2.json").write_text("{not json")
        self.append({"timestamp": "2026-10-15T11:00:00Z", "type": "user", "message": {"content": "Prompt"}})
        written = datetime(2026, 10, 15, 11, 58, tzinfo=timezone.utc).timestamp()
        os.utime(self.transcript, (written, written))
        state = self.state()
        tasks = {task["id"]: task for task in state["tasks"]}
        self.assertEqual(tasks["1"]["updatedAt"], "2026-10-15T11:40:00Z")
        self.assertTrue(tasks["1"]["readable"])
        self.assertFalse(tasks["2"]["readable"])
        self.assertEqual(state["transcriptUpdatedAt"], "2026-10-15T11:58:00Z")
        self.transcript.unlink()
        self.assertIsNone(self.state()["transcriptUpdatedAt"])

    def test_plan_reads_task_files_but_not_the_transcript(self):
        self.task("1")
        self.append({"timestamp": "2026-10-15T11:00:00Z", "type": "user", "message": {"content": "Prompt"}})
        opened = []
        original_open = Path.open

        def record_open(path, *args, **kwargs):
            opened.append(path)
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", record_open):
            plan = self.store.plan(self.list_id)
        self.assertEqual([task["id"] for task in plan], ["1"])
        self.assertNotIn(self.transcript, opened)
        self.assertEqual(self.store.plan("unknown-session"), [])
        self.assertEqual(self.store.plan("../outside"), [])

    def test_runs_record_log_change_time_and_process_liveness(self):
        log = self.task_dir / "worker.log"
        log.write_text("working")
        written = datetime(2026, 10, 15, 11, 50, tzinfo=timezone.utc).timestamp()
        os.utime(log, (written, written))
        finished = subprocess.Popen([sys.executable, "-c", "pass"])
        finished.wait()
        started = "2026-10-15T11:00:00Z"
        self.task(metadata={"runs": [
            {"label": "Logged", "started": started, "ended": None, "log": "worker.log", "pid": os.getpid()},
            {"label": "Lost log", "started": started, "ended": None, "log": "missing.log"},
            {"label": "Exited", "started": started, "ended": None, "log": None, "pid": finished.pid},
            {"label": "Bad pid", "started": started, "ended": None, "log": None, "pid": True},
        ]})
        task = self.state()["tasks"][0]
        runs = {run["label"]: run for run in task["runs"]}
        self.assertEqual(set(runs), {"Logged", "Lost log", "Exited"})
        self.assertEqual(runs["Logged"]["logUpdatedAt"], "2026-10-15T11:50:00Z")
        self.assertIs(runs["Logged"]["pidAlive"], True)
        self.assertEqual(runs["Logged"]["pid"], os.getpid())
        self.assertIsNone(runs["Lost log"]["logUpdatedAt"])
        self.assertIsNone(runs["Lost log"]["pid"])
        self.assertIsNone(runs["Lost log"]["pidAlive"])
        self.assertIs(runs["Exited"]["pidAlive"], False)
        self.assertIn("runs[3] has invalid label, timestamps, log path, or pid", task["warnings"])

    def test_lists_use_custom_title_and_include_transcript_only_session(self):
        self.task()
        self.append({"timestamp": "2026-10-15T11:00:00Z", "type": "user", "cwd": "/example/project", "message": {"content": "First prompt\nMore details"}})
        title_dir = self.project_dir / self.list_id
        title_dir.mkdir()
        (title_dir / "custom-title.json").write_text(json.dumps({"customTitle": "Live chart"}))
        other = self.project_dir / "other-session.jsonl"
        self.append({"timestamp": "2026-10-15T11:30:00Z", "type": "user", "cwd": "/example/other", "message": {"content": "Another prompt"}}, path=other)
        earlier, later = self.now.timestamp() - 1, self.now.timestamp() + 1
        for path in (self.task_dir / "1.json", self.transcript):
            os.utime(path, (earlier, earlier))
        os.utime(other, (later, later))
        lists = self.store.lists()
        self.assertEqual(lists[0]["id"], "other-session")
        by_id = {session["id"]: session for session in lists}
        self.assertEqual(by_id[self.list_id]["title"], "Live chart")
        self.assertEqual(by_id[self.list_id]["project"], "/example/project")
        self.assertEqual(self.store.state("other-session", self.now)["tasks"], [])
        self.assertIsNone(self.store.state("../outside", self.now))

    def test_listing_old_session_does_not_open_its_transcript_or_custom_title(self):
        self.append({"type": "user", "cwd": "/old/project", "message": {"content": "Old prompt"}})
        title_dir = self.transcript.with_suffix("")
        title_dir.mkdir()
        custom_title = title_dir / "custom-title.json"
        custom_title.write_text(json.dumps({"customTitle": "Old custom title"}))
        modified = (self.now - timedelta(days=15)).timestamp()
        os.utime(self.transcript, (modified, modified))
        opened = []
        original_open = Path.open

        def record_open(path, *args, **kwargs):
            opened.append(path)
            return original_open(path, *args, **kwargs)

        with patch("live_gantt.sources.datetime", wraps=datetime) as clock, patch.object(Path, "open", record_open):
            clock.now.return_value = self.now
            listing = self.store.lists()[0]
        self.assertNotIn(self.transcript, opened)
        self.assertNotIn(custom_title, opened)
        self.assertEqual(listing, {"id": self.list_id, "title": self.list_id, "project": "",
                                   "updatedAt": "2026-09-30T12:00:00Z", "recent": False})

    def test_selecting_old_session_reads_its_identity_lazily(self):
        self.append({"type": "user", "cwd": "/old/project", "message": {"content": "Old prompt"}})
        modified = (self.now - timedelta(days=15)).timestamp()
        os.utime(self.transcript, (modified, modified))
        with patch("live_gantt.sources.datetime", wraps=datetime) as clock:
            clock.now.return_value = self.now
            self.assertEqual(self.store.lists()[0]["title"], self.list_id)
            selected = self.state()
            self.assertEqual(selected["title"], "Old prompt")
            self.assertEqual(selected["project"], "/old/project")
            self.assertEqual(self.store.lists()[0]["title"], self.list_id)

    def test_recent_session_window_includes_exact_boundary_and_task_changes(self):
        cutoff = self.now - timedelta(days=14)
        self.append({"type": "user", "cwd": "/recent/project", "message": {"content": "At the cutoff"}})
        os.utime(self.transcript, (cutoff.timestamp(), cutoff.timestamp()))
        other = self.project_dir / "older-session.jsonl"
        self.append({"type": "user", "message": {"content": "Older prompt"}}, path=other)
        older = cutoff.timestamp() - 1
        os.utime(other, (older, older))
        with patch("live_gantt.sources.datetime", wraps=datetime) as clock:
            clock.now.return_value = self.now
            by_id = {listing["id"]: listing for listing in self.store.lists()}
            self.assertTrue(by_id[self.list_id]["recent"])
            self.assertEqual(by_id[self.list_id]["title"], "At the cutoff")
            self.assertFalse(by_id["older-session"]["recent"])
            os.utime(self.transcript, (older, older))
            self.task()
            task_path = self.task_dir / "1.json"
            os.utime(task_path, (cutoff.timestamp(), cutoff.timestamp()))
            listing = next(item for item in self.store.lists() if item["id"] == self.list_id)
            self.assertTrue(listing["recent"])
            self.assertEqual(listing["title"], "At the cutoff")

    def test_malformed_values_warn_and_state_remains_json_serializable(self):
        self.task(status=["completed"], metadata={
            "weight": 10 ** 400,
            "milestone": 12,
            "lane": [],
            "progress": None,
            "estimate": None,
            "live": {"journal": "bad\u0000path", "planned": 1, "from": 0, "to": 100},
            "runs": [{"label": "Reverse", "started": "2026-10-15T11:00:00Z", "ended": "2026-10-15T10:00:00Z"}],
            "future": {"value": float("nan")},
        })
        state = self.state()
        task = state["tasks"][0]
        self.assertEqual(task["status"], "pending")
        self.assertEqual(task["weight"], 1)
        self.assertEqual(task["lane"], "Main")
        self.assertEqual(task["runs"], [])
        self.assertIsNone(task["metadata"]["future"]["value"])
        self.assertTrue(task["warnings"])
        json.dumps(state, allow_nan=False)

    def test_nonregular_task_entries_are_ignored(self):
        (self.task_dir / "1.json").mkdir()
        self.assertEqual(self.state()["tasks"], [])

    def test_numeric_task_ids_larger_than_integer_conversion_limit(self):
        giant_id = "9" * 5000
        self.task("1", id=giant_id)
        self.task("2", id="20")
        self.task("3", id="3")
        self.assertCountEqual([task["id"] for task in self.state()["tasks"]], ["3", "20", giant_id])

    def test_full_transcript_fills_identity_beyond_picker_prefix(self):
        for _ in range(150):
            self.append({"timestamp": "2026-10-15T10:00:00Z", "type": "assistant", "message": {"content": "setup"}})
        self.append({"timestamp": "2026-10-15T11:00:00Z", "type": "user", "cwd": "/late/project", "message": {"content": "Late first prompt\nBody"}})
        self.assertEqual(self.store.lists()[0]["title"], self.list_id)
        state = self.state()
        self.assertEqual(state["title"], "Late first prompt")
        self.assertEqual(state["project"], "/late/project")
        self.assertEqual(self.store.lists()[0]["title"], "Late first prompt")

    def test_title_skips_harness_wrappers_to_the_first_typed_prompt(self):
        self.append(
            {"type": "user", "isMeta": True, "message": {"content": "Plain caveat text the harness marks as meta"}},
            {"type": "user", "message": {"content": "<local-command-caveat>Caveat: run locally.</local-command-caveat>"}},
            {"type": "user", "message": {"content": "<command-name>/resume</command-name>\n"
                                                    "<command-message>resume</command-message>\n<command-args></command-args>"}},
            {"type": "user", "message": {"content": "<local-command-stdout>Resumed</local-command-stdout>"}},
            {"type": "user", "message": {"content": [
                {"type": "text", "text": "<system-reminder>\nContext\n</system-reminder>"},
                {"type": "text", "text": "<pasted_content id=\"1\">log</pasted_content>\n\n  Fix the picker\nDetails"},
            ]}},
        )
        self.assertEqual(self.store.lists()[0]["title"], "Fix the picker")
        self.assertEqual(self.state()["title"], "Fix the picker")

    def test_title_keeps_a_prompt_that_only_mentions_markup_and_bounds_its_length(self):
        self.append({"type": "user", "message": {"content": "Why does <b>bold</b> break?"}})
        self.assertEqual(self.store.lists()[0]["title"], "Why does <b>bold</b> break?")
        other = self.project_dir / "long-session.jsonl"
        self.append({"type": "user", "message": {"content": "word " * 200}}, path=other)
        title = next(item["title"] for item in self.store.lists() if item["id"] == "long-session")
        self.assertEqual(len(title), PROMPT_TITLE_CHARACTERS)
        self.assertTrue(title.startswith("word word"))
        self.assertTrue(title.endswith("…"))


if __name__ == "__main__":
    unittest.main()
