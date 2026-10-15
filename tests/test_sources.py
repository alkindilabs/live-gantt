import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from live_gantt.sources import SourceStore


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

    def test_lists_use_custom_title_and_include_transcript_only_session(self):
        self.task()
        self.append({"timestamp": "2026-10-15T11:00:00Z", "type": "user", "cwd": "/example/project", "message": {"content": "First prompt\nMore details"}})
        title_dir = self.project_dir / self.list_id
        title_dir.mkdir()
        (title_dir / "custom-title.json").write_text(json.dumps({"customTitle": "Live chart"}))
        other = self.project_dir / "other-session.jsonl"
        self.append({"timestamp": "2026-10-15T11:30:00Z", "type": "user", "cwd": "/example/other", "message": {"content": "Another prompt"}}, path=other)
        os.utime(other, (self.now.timestamp() + 1, self.now.timestamp() + 1))
        lists = self.store.lists()
        self.assertEqual(lists[0]["id"], "other-session")
        by_id = {session["id"]: session for session in lists}
        self.assertEqual(by_id[self.list_id]["title"], "Live chart")
        self.assertEqual(by_id[self.list_id]["project"], "/example/project")
        self.assertEqual(self.store.state("other-session", self.now)["tasks"], [])
        self.assertIsNone(self.store.state("../outside", self.now))

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


if __name__ == "__main__":
    unittest.main()
