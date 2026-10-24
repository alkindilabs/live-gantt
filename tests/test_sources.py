import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from live_gantt.freshness import ACTIVE_SESSION_MINUTES
from live_gantt.sources import IDENTITY_SCAN_LINES, IDENTITY_TAIL_BYTES, PROMPT_TITLE_CHARACTERS, SourceStore


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

    def test_runtime_activity_ignores_metadata_accounting_and_malformed_records(self):
        self.append({"type": "user", "timestamp": "2026-10-15T09:00:00Z", "message": {"content": "Please build it"}})
        first = self.state()["_runtime"]
        self.assertEqual(first, {"phase": "working", "lastActivityAt": "2026-10-15T09:00:00Z", "detail": None})
        self.append({"type": "assistant", "timestamp": "2026-10-15T10:00:00Z",
                     "message": {"id": "usage-only", "content": [], "usage": {"output_tokens": 7}}},
                    {"type": "custom-title", "timestamp": "2026-10-15T10:01:00Z", "customTitle": "Updated title"},
                    {"type": "user", "isMeta": True, "timestamp": "2026-10-15T10:02:00Z", "message": {"content": "Metadata"}})
        with self.transcript.open("a") as stream:
            stream.write('{"timestamp":"2026-10-15T10:03:00Z","type":"user",bad}\n')
        self.assertEqual(self.state()["_runtime"], first)
        self.append(self.tool("2026-10-15T10:04:00Z", "Bash", {"command": "build"}))
        self.assertEqual(self.state()["_runtime"]["lastActivityAt"], "2026-10-15T10:04:00Z")
        self.append(self.result("2026-10-15T10:05:00Z", "Build result"),
                    {"type": "system", "subtype": "turn_duration", "timestamp": "2026-10-15T10:06:00Z"})
        self.assertEqual(self.state()["_runtime"],
                         {"phase": "idle", "lastActivityAt": "2026-10-15T10:06:00Z", "detail": None})

    def test_runtime_terminal_failure_is_sanitized_and_next_request_recovers(self):
        self.append({"type": "result", "subtype": "error_during_execution", "timestamp": "2026-10-15T09:00:00Z",
                     "errors": ["private path and credentials"]})
        failure = self.state()["_runtime"]
        self.assertEqual(failure["phase"], "failed")
        self.assertEqual(failure["lastActivityAt"], "2026-10-15T09:00:00Z")
        self.assertNotIn("private", str(failure))
        self.append({"type": "user", "timestamp": "2026-10-15T09:01:00Z", "message": {"content": "Try again"}})
        self.assertEqual(self.state()["_runtime"]["phase"], "working")
        error = self.result("2026-10-15T09:02:00Z", "recoverable tool failure")
        error["message"]["content"][0]["is_error"] = True
        self.append(error)
        self.assertEqual(self.state()["_runtime"]["phase"], "working")
        self.append({"type": "user", "timestamp": "2026-10-15T09:03:00Z", "message": {"content": "[Request interrupted by user]"}})
        self.assertEqual(self.state()["_runtime"]["phase"], "interrupted")

    def test_assistant_response_end_does_not_confirm_harness_completion(self):
        self.append({"type": "assistant", "timestamp": "2026-10-15T10:00:00Z",
                     "message": {"content": [{"type": "text", "text": "The command finished."}],
                                 "stop_reason": "end_turn"}})
        self.assertEqual(self.state()["_runtime"],
                         {"phase": "unknown", "lastActivityAt": "2026-10-15T10:00:00Z", "detail": None})
        self.append({"type": "system", "subtype": "turn_duration", "timestamp": "2026-10-15T10:00:02Z"})
        self.assertEqual(self.state()["_runtime"],
                         {"phase": "idle", "lastActivityAt": "2026-10-15T10:00:02Z", "detail": None})

    def test_runtime_ignores_repeated_message_with_only_changed_accounting(self):
        message = {"id": "response", "content": [{"type": "text", "text": "Working on it"}], "stop_reason": None}
        self.append({"type": "assistant", "timestamp": "2026-10-15T09:00:00Z", "message": message})
        self.assertEqual(self.state()["_runtime"]["lastActivityAt"], "2026-10-15T09:00:00Z")
        self.append({"type": "assistant", "timestamp": "2026-10-15T10:00:00Z",
                     "message": {**message, "usage": {"output_tokens": 70}}})
        self.assertEqual(self.state()["_runtime"]["lastActivityAt"], "2026-10-15T09:00:00Z")
        self.append({"type": "assistant", "timestamp": "2026-10-15T10:01:00Z",
                     "message": {**message, "stop_reason": "end_turn"}})
        self.assertEqual(self.state()["_runtime"],
                         {"phase": "unknown", "lastActivityAt": "2026-10-15T10:01:00Z", "detail": None})

    def test_runtime_turn_end_with_background_work_does_not_claim_completion(self):
        self.append({"type": "system", "subtype": "turn_duration", "timestamp": "2026-10-15T09:00:00Z",
                     "pendingBackgroundAgentCount": 1})
        self.assertEqual(self.state()["_runtime"]["phase"], "working")
        self.append({"type": "assistant", "timestamp": "2026-10-15T09:01:00Z",
                     "message": {"content": [{"type": "text", "text": "Turn response"}], "stop_reason": "end_turn"}})
        self.assertEqual(self.state()["_runtime"]["phase"], "unknown")
        self.transcript.write_text("")
        self.assertEqual(self.state()["_runtime"], {"phase": "unknown", "lastActivityAt": None, "detail": None})

    def test_runtime_unanswered_question_stays_unverified_until_matching_result(self):
        question = {"questions": [{"question": "Private question", "header": "Choice",
                     "options": [{"label": "First", "description": "One"},
                                 {"label": "Second", "description": "Two"}], "multiSelect": False}]}
        self.append(self.tool("2026-10-15T09:00:00Z", "AskUserQuestion", question, "question-1"))
        runtime = self.state()["_runtime"]
        self.assertEqual(runtime["phase"], "unknown")
        self.assertEqual(runtime["detail"], "Recorded input request has no recorded response.")
        self.assertNotIn("Private", json.dumps(runtime))
        self.append(self.result("2026-10-15T09:01:00Z", "unrelated", "other-tool"),
                    {"type": "user", "isMeta": True, "timestamp": "2026-10-15T09:02:00Z",
                     "message": {"content": "Metadata is not a response"}})
        self.assertEqual(self.state()["_runtime"]["phase"], "unknown")
        self.append({"type": "user", "timestamp": "2026-10-15T09:02:30Z",
                     "message": {"content": "<task-notification><status>completed</status></task-notification>"}})
        self.assertEqual(self.state()["_runtime"]["phase"], "unknown")
        self.append(self.result("2026-10-15T09:03:00Z", "answered", "question-1"))
        self.assertEqual(self.state()["_runtime"],
                         {"phase": "working", "lastActivityAt": "2026-10-15T09:03:00Z", "detail": None})

    def test_runtime_question_clears_on_user_turn_response_end_or_turn_end(self):
        question = {"questions": [{"question": "Choose"}]}
        endings = [
            ({"type": "user", "message": {"content": "New request"}}, "working"),
            ({"type": "assistant", "message": {"content": "Response finished", "stop_reason": "end_turn"}}, "unknown"),
            ({"type": "system", "subtype": "turn_duration"}, "idle"),
            ({"type": "result", "subtype": "error_during_execution"}, "failed"),
        ]
        for record, expected in endings:
            with self.subTest(kind=record["type"]):
                self.append(self.tool("2026-10-15T10:00:00Z", "AskUserQuestion", question, "question-2"))
                self.assertEqual(self.state()["_runtime"]["phase"], "unknown")
                self.append({**record, "timestamp": "2026-10-15T10:01:00Z"})
                self.assertEqual(self.state()["_runtime"]["phase"], expected)
                if expected == "unknown":
                    self.assertIsNone(self.state()["_runtime"]["detail"])
                self.transcript.write_text("")
                self.state()

    def test_runtime_malformed_question_does_not_create_pending_input(self):
        for questions in (None, [], [{}], [{"question": ""}], [{"question": 4}]):
            self.append(self.tool("2026-10-15T09:00:00Z", "AskUserQuestion", {"questions": questions}, "invalid"))
            self.assertEqual(self.state()["_runtime"]["phase"], "working")

    def test_runtime_malformed_tool_blocks_do_not_restart_idle_work(self):
        self.append({"type": "system", "subtype": "turn_duration", "timestamp": "2026-10-15T09:00:00Z"})
        idle = self.state()["_runtime"]
        malformed = [
            {"type": "tool_use"},
            {"type": "tool_use", "id": "call", "name": "Bash", "input": None},
            {"type": "tool_use", "id": "", "name": "Bash", "input": {}},
            {"type": "tool_result"},
            {"type": "tool_result", "tool_use_id": " "},
        ]
        for block in malformed:
            with self.subTest(block=block):
                self.append({"type": "user" if block["type"] == "tool_result" else "assistant",
                             "timestamp": "2026-10-15T10:00:00Z", "message": {"content": [block]}})
                self.assertEqual(self.state()["_runtime"], idle)
        for role, block in (("user", {"type": "tool_use", "id": "call", "name": "Bash", "input": {}}),
                            ("assistant", {"type": "tool_result", "tool_use_id": "call", "content": ""})):
            self.append({"type": role, "timestamp": "2026-10-15T10:00:00Z", "message": {"content": [block]}})
            self.assertEqual(self.state()["_runtime"], idle)
        self.append(self.tool("2026-10-15T10:01:00Z", "Bash", {}))
        self.assertEqual(self.state()["_runtime"]["phase"], "working")
        self.assertEqual(self.state()["_runtime"]["lastActivityAt"], "2026-10-15T10:01:00Z")

    def test_runtime_empty_tool_result_requires_identifier_to_resolve_input(self):
        self.append(self.tool("2026-10-15T09:00:00Z", "AskUserQuestion",
                              {"questions": [{"question": "Choose"}]}, "question"))
        pending = self.state()["_runtime"]
        self.append(self.result("2026-10-15T10:00:00Z", "", ""))
        self.assertEqual(self.state()["_runtime"], pending)
        self.append(self.result("2026-10-15T10:01:00Z", "", "question"))
        self.assertEqual(self.state()["_runtime"],
                         {"phase": "working", "lastActivityAt": "2026-10-15T10:01:00Z", "detail": None})

    def test_session_usage_counts_each_response_once_and_excludes_child_usage(self):
        def response(identifier, output):
            return {"type": "assistant", "timestamp": "2026-10-15T09:00:00Z",
                    "message": {"id": identifier, "content": [], "usage": {
                        "input_tokens": 10, "cache_read_input_tokens": 20,
                        "cache_creation_input_tokens": 5, "output_tokens": output}}}

        self.append(response("message-one", 1), response("message-one", 7),
                    response("message-two", 3))
        child = self.project_dir / self.list_id / "subagents" / "agent-child.jsonl"
        child.parent.mkdir(parents=True)
        self.append(response("child-message", 999), path=child)
        usage = self.state()["tokenUsage"]
        self.assertEqual(usage, {"inputTokens": 70, "cachedInputTokens": 40,
                                 "cacheWriteTokens": 10, "outputTokens": 10,
                                 "reasoningOutputTokens": None, "totalTokens": 80})
        self.assertEqual(self.state()["tokenUsage"], usage)

    def test_missing_and_invalid_usage_stays_unknown(self):
        self.append({"type": "assistant", "message": {"id": "invalid", "usage": {
            "input_tokens": True, "output_tokens": -1}}})
        self.assertTrue(all(value is None for value in self.state()["tokenUsage"].values()))

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

    def test_incomplete_task_write_retains_last_valid_state_until_recovery_or_removal(self):
        self.task(metadata={"progress": 70, "lane": "Build", "estimate": {"low": 1, "likely": 2, "high": 3}})
        original = self.state()["tasks"][0]
        path = self.task_dir / "1.json"
        path.write_text('{"id":"1","status":')
        retained = self.state()["tasks"][0]
        self.assertEqual((retained["status"], retained["progress"], retained["lane"], retained["estimate"]),
                         (original["status"], original["progress"], original["lane"], original["estimate"]))
        self.assertFalse(retained["readable"])
        self.assertTrue(retained["warnings"])
        self.task(status="completed", metadata={"progress": 100})
        recovered = self.state()["tasks"][0]
        self.assertEqual((recovered["status"], recovered["progress"], recovered["readable"]),
                         ("completed", 100, True))
        path.unlink()
        self.assertEqual(self.state()["tasks"], [])
        path.write_text("broken")
        self.assertEqual(self.state()["tasks"][0]["progress"], 0)
        self.assertFalse(self.state()["tasks"][0]["readable"])

    def test_failed_task_update_cannot_create_lifecycle_or_progress_history(self):
        self.task(status="pending", metadata={"progress": 5})
        self.append(self.tool("2026-10-15T09:00:00Z", "TaskUpdate",
                              {"taskId": "1", "status": "in_progress", "metadata": {"progress": 20}}, "success"),
                    self.result("2026-10-15T09:00:01Z", "Updated task #1", "success"))
        self.assertEqual(len(self.state()["tasks"][0]["history"]), 2)
        self.append(self.tool("2026-10-15T10:00:00Z", "TaskUpdate",
                              {"taskId": "1", "status": "completed", "metadata": {"progress": 90}}, "denied"))
        failed = self.result("2026-10-15T10:00:01Z", "Permission denied", "denied")
        failed["message"]["content"][0]["is_error"] = True
        self.append(failed)
        task = self.state()["tasks"][0]
        self.assertIsNone(task["ended"])
        self.assertEqual([entry["event"] for entry in task["history"]], ["started", "progress"])
        self.assertEqual([entry["value"] for entry in task["history"] if entry["event"] == "progress"], [20])

    def test_discovery_io_failure_is_distinct_from_removed_sources(self):
        self.task(metadata={"progress": 70})
        self.assertEqual(self.store.lists()[0]["taskCount"], 1)

        original_iterdir = Path.iterdir

        def unavailable_directory(path):
            if path == self.root / "tasks":
                raise PermissionError("source temporarily unavailable")
            return original_iterdir(path)

        with patch.object(Path, "iterdir", unavailable_directory):
            with self.assertRaises(PermissionError):
                self.store.lists()
        original_stat = Path.stat

        def unavailable_task(path, *args, **kwargs):
            if path == self.task_dir / "1.json":
                raise PermissionError("task temporarily unavailable")
            return original_stat(path, *args, **kwargs)

        with patch.object(Path, "stat", unavailable_task):
            with self.assertRaises(PermissionError):
                self.store.lists()
        self.assertEqual(self.store.lists()[0]["taskCount"], 1)

    def test_larger_in_place_transcript_rewrite_discards_prior_history(self):
        self.task()
        self.append(self.tool("2026-10-15T09:00:00Z", "TaskUpdate", {"taskId": "1", "status": "in_progress"}),
                    self.tool("2026-10-15T10:00:00Z", "TaskUpdate", {"taskId": "1", "metadata": {"progress": 70}}))
        self.assertEqual(len(self.state()["tasks"][0]["history"]), 2)
        previous_size = self.transcript.stat().st_size
        replacement = self.tool("2026-10-15T11:00:00Z", "TaskUpdate", {
            "taskId": "1", "status": "in_progress", "description": "replacement" * previous_size})
        self.transcript.write_text(json.dumps(replacement) + "\n")
        current = self.state()["tasks"][0]
        self.assertEqual(current["started"], "2026-10-15T11:00:00Z")
        self.assertEqual([event["event"] for event in current["history"]], ["started"])

    def test_journal_counts_only_valid_top_level_results_and_detects_larger_rewrite(self):
        journal = self.task_dir / "work.jsonl"
        self.task(metadata={"live": {"journal": "work.jsonl", "planned": 4, "from": 0, "to": 100}})
        journal.write_text('{"type":"result", BROKEN}\n{"type":"note","payload":{"type":"result"}}\n')
        self.assertEqual(self.state()["tasks"][0]["liveProgress"], 0)
        journal.write_text('{"type":"result"}\n{"type":"result"}\n')
        self.assertEqual(self.state()["tasks"][0]["liveProgress"], 50)
        journal.write_text(json.dumps({"type": "result", "payload": "replacement" * 100}) + "\n")
        self.assertEqual(self.state()["tasks"][0]["liveProgress"], 25)

    def test_required_transcript_and_journal_read_failures_propagate(self):
        journal = self.task_dir / "work.jsonl"
        journal.write_text('{"type":"result"}\n')
        self.task(metadata={"live": {"journal": "work.jsonl", "planned": 4, "from": 0, "to": 100}})
        self.append(self.tool("2026-10-15T09:00:00Z", "TaskUpdate", {"taskId": "1", "status": "in_progress"}))
        self.assertEqual(self.state()["tasks"][0]["liveProgress"], 25)
        original_open = Path.open
        for failed_path in (self.transcript, journal):
            with self.subTest(path=failed_path.name):
                def denied(path, *args, **kwargs):
                    if path == failed_path:
                        raise PermissionError("source temporarily unavailable")
                    return original_open(path, *args, **kwargs)
                with patch.object(Path, "open", denied):
                    with self.assertRaises(PermissionError):
                        self.state()
        self.assertEqual(self.state()["tasks"][0]["liveProgress"], 25)

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
        self.assertIsNone(by_id["silent-agent"]["ended"])
        self.assertNotIn("finished-agent", by_id)

    def agent(self, agent_id, *records, directory=None, meta=None):
        self.transcript.touch()
        agents_dir = directory or self.project_dir / self.list_id / "subagents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        (agents_dir / f"agent-{agent_id}.meta.json").write_text(json.dumps(meta or {"description": agent_id}))
        self.append(*records, path=agents_dir / f"agent-{agent_id}.jsonl")

    def agents_by_id(self):
        return {agent["id"]: agent for agent in self.state()["agents"]}

    @staticmethod
    def notification(task_id, status="completed"):
        return f"<task-notification>\n<task-id>{task_id}</task-id>\n<status>{status}</status>\n</task-notification>"

    def test_notifications_queued_or_attached_mid_turn_end_their_agents(self):
        for agent_id in ("queued-agent", "attached-agent"):
            self.agent(agent_id, self.tool("2026-10-15T11:00:00Z", "Bash", {"description": "Work"}))
        self.append(
            {"type": "queue-operation", "operation": "enqueue", "timestamp": "2026-10-15T11:10:00Z",
             "content": self.notification("queued-agent")},
            {"type": "attachment", "timestamp": "2026-10-15T11:11:00Z",
             "attachment": {"type": "queued_command", "commandMode": "task-notification",
                            "prompt": self.notification("attached-agent", "failed")}},
        )
        agents = self.agents_by_id()
        self.assertEqual(agents["queued-agent"]["status"], "completed")
        self.assertEqual(agents["queued-agent"]["ended"], "2026-10-15T11:00:00Z")
        self.assertEqual(agents["attached-agent"]["status"], "failed")

    def hand_back(self, timestamp, tool_id="hand-back"):
        ended = self.result(timestamp, "Report delivered", tool_id)
        ended["toolEndsTurn"] = True
        return ended

    def test_an_agent_that_handed_back_is_finished_without_a_notification(self):
        self.agent("handed-back",
                   self.tool("2026-10-15T09:00:00Z", "Bash", {"description": "Work"}),
                   self.tool("2026-10-15T09:20:00Z", "SubagentHandback", {"message": "Report"}, "hand-back"),
                   self.hand_back("2026-10-15T09:20:01Z"))
        agent = self.agents_by_id()["handed-back"]
        self.assertEqual(agent["status"], "completed")
        self.assertEqual(agent["started"], "2026-10-15T09:00:00Z")
        self.assertEqual(agent["ended"], "2026-10-15T09:20:01Z")

    def test_a_resumed_agent_runs_again_until_it_hands_back_again(self):
        self.agent("resumed",
                   self.tool("2026-10-15T11:00:00Z", "Bash", {"description": "Work"}),
                   self.hand_back("2026-10-15T11:10:00Z"))
        self.append({"type": "user", "timestamp": "2026-10-15T11:10:01Z", "message": {"content": self.notification("resumed")}})
        self.assertEqual(self.agents_by_id()["resumed"]["status"], "completed")
        self.append({"type": "user", "timestamp": "2026-10-15T11:40:00Z", "message": {"content": "Follow-up message"}},
                    self.tool("2026-10-15T11:41:00Z", "Bash", {"description": "More work"}),
                    path=self.project_dir / self.list_id / "subagents" / "agent-resumed.jsonl")
        agent = self.agents_by_id()["resumed"]
        self.assertEqual(agent["status"], "running")
        self.assertIsNone(agent["ended"])
        self.append(self.hand_back("2026-10-15T11:50:00Z"), path=self.project_dir / self.list_id / "subagents" / "agent-resumed.jsonl")
        self.assertEqual(self.agents_by_id()["resumed"]["status"], "completed")

    def test_nonexecution_records_do_not_revive_terminal_agents(self):
        for status in ("completed", "failed"):
            with self.subTest(status=status):
                agent_id = f"terminal-{status}"
                self.agent(agent_id, self.tool("2026-10-15T11:00:00Z", "Bash", {"description": "Work"}))
                self.append({"type": "user", "timestamp": "2026-10-15T11:10:00Z",
                             "message": {"content": self.notification(agent_id, status)}})
                before = self.agents_by_id()[agent_id]
                path = self.project_dir / self.list_id / "subagents" / f"agent-{agent_id}.jsonl"
                self.append(
                    {"type": "assistant", "timestamp": "2026-10-15T11:20:00Z", "message": {
                        "id": "accounting", "content": [], "usage": {"output_tokens": 10}}},
                    {"type": "custom-title", "timestamp": "2026-10-15T11:21:00Z", "customTitle": "Metadata"},
                    {"type": "user", "isMeta": True, "timestamp": "2026-10-15T11:22:00Z",
                     "message": {"content": "Harness metadata"}}, path=path)
                with path.open("a") as stream:
                    stream.write('{"type":"assistant","timestamp":"2026-10-15T11:23:00Z",bad}\n')
                after = self.agents_by_id()[agent_id]
                self.assertEqual(after["status"], status)
                self.assertEqual(after["ended"], before["ended"])
                self.assertEqual(after["lastActivityAt"], before["lastActivityAt"])

    def test_nonexecution_records_do_not_clear_agent_handback_or_extend_activity(self):
        self.agent("handed-back", self.tool("2026-10-15T11:00:00Z", "Bash", {"description": "Work"}),
                   self.hand_back("2026-10-15T11:10:00Z"))
        before = self.agents_by_id()["handed-back"]
        path = self.project_dir / self.list_id / "subagents" / "agent-handed-back.jsonl"
        self.append({"type": "assistant", "timestamp": "2026-10-15T11:20:00Z", "message": {
            "id": "accounting", "content": [], "usage": {"output_tokens": 10}}}, path=path)
        self.assertEqual(self.agents_by_id()["handed-back"], before)

    def test_native_idle_observation_does_not_revive_terminal_agent(self):
        self.agent("idle-child", self.tool("2026-10-15T11:00:00Z", "Bash", {"description": "Work"}))
        self.append({"type": "user", "timestamp": "2026-10-15T11:10:00Z",
                     "message": {"content": self.notification("idle-child")}})
        before = self.agents_by_id()["idle-child"]
        self.append({"type": "system", "subtype": "session_state_changed", "state": "idle",
                     "timestamp": "2026-10-15T11:59:00Z"},
                    path=self.project_dir / self.list_id / "subagents" / "agent-idle-child.jsonl")
        self.assertEqual(self.agents_by_id()["idle-child"], before)

    def test_agent_activity_is_the_latest_step_description_never_a_tool_name(self):
        self.agent("worker",
                   self.tool("2026-10-15T11:50:00Z", "Bash", {"command": "make", "description": "Build the chart"}),
                   self.tool("2026-10-15T11:51:00Z", "Read", {"file_path": "/example/notes"}, "read"))
        self.assertEqual(self.agents_by_id()["worker"]["activity"], "")

    def workflow(self, run_id, journal, agents):
        run_dir = self.project_dir / self.list_id / "subagents" / "workflows" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        self.append(*journal, path=run_dir / "journal.jsonl")
        for agent_id, (label, phase, records) in agents.items():
            self.agent(agent_id, *records, directory=run_dir,
                       meta={"agentType": "workflow-subagent", "description": label, "workflowPhase": phase})

    def launch(self, timestamp, run_id, task_id, name, summary):
        launched = self.result(timestamp, f"Workflow launched in background. Task ID: {task_id}", "launch")
        launched["toolUseResult"] = {"status": "async_launched", "taskId": task_id, "taskType": "local_workflow",
                                     "workflowName": name, "runId": run_id, "summary": summary}
        return launched

    def test_workflow_agents_are_grouped_under_their_run_with_labels_and_state(self):
        self.task(metadata={"agents": ["assigned"]})
        self.append(self.tool("2026-10-15T11:00:00Z", "Workflow", {"description": "Review"}, "launch"),
                    self.launch("2026-10-15T11:00:01Z", "wf_one", "workflow-task", "review-flow", "Review the change"))
        started = [{"type": "started", "key": f"k{index}", "agentId": agent_id, "label": label, "phase": phase}
                   for index, (agent_id, label, phase) in enumerate((
                       ("reviewer", "review:correctness", "Review"), ("measurer", "measure", "Review"),
                       ("verifier", "verify", "Verify"), ("assigned", "review:assigned", "Review")))]
        self.workflow("wf_one", [{"type": "launched"}, *started, {"type": "result", "key": "k0", "agentId": "reviewer", "result": {}}], {
            "reviewer": ("review:correctness", "Review", [self.tool("2026-10-15T11:01:00Z", "Bash", {"description": "Read"})]),
            "measurer": ("measure", "Review", [self.tool("2026-10-15T11:55:00Z", "Bash", {"description": "Measure"})]),
            "verifier": ("verify", "Verify", [self.tool("2026-10-15T11:20:00Z", "StructuredOutput", {}, "out"),
                                              self.hand_back("2026-10-15T11:20:01Z", "out")]),
            "assigned": ("review:assigned", "Review", [self.tool("2026-10-15T11:02:00Z", "Bash", {"description": "Check"})]),
        })
        state = self.state()
        self.assertEqual(state["agents"], [])
        self.assertEqual([agent["id"] for agent in state["tasks"][0]["agents"]], ["assigned"])
        workflow, = state["workflows"]
        self.assertEqual((workflow["id"], workflow["name"], workflow["description"]), ("wf_one", "review-flow", "Review the change"))
        self.assertEqual((workflow["status"], workflow["started"], workflow["ended"]), ("running", "2026-10-15T11:00:01Z", None))
        self.assertEqual([(agent["id"], agent["description"], agent["phase"], agent["status"]) for agent in workflow["agents"]], [
            ("reviewer", "review:correctness", "Review", "completed"),
            ("measurer", "measure", "Review", "running"),
            ("verifier", "verify", "Verify", "completed"),
        ])
        self.assertEqual(workflow["agents"][0]["workflow"], "review-flow")
        self.assertEqual(self.store.lists()[0]["agentCount"], 4)
        self.append({"type": "user", "timestamp": "2026-10-15T11:58:00Z", "message": {"content": self.notification("workflow-task", "killed")}})
        workflow, = self.state()["workflows"]
        self.assertEqual((workflow["status"], workflow["ended"]), ("killed", "2026-10-15T11:58:00Z"))
        self.assertEqual([(agent["status"], agent["ended"]) for agent in workflow["agents"]], [
            ("completed", "2026-10-15T11:01:00Z"), ("killed", "2026-10-15T11:55:00Z"), ("completed", "2026-10-15T11:20:01Z")])

    def test_a_workflow_without_its_launch_record_is_named_by_its_run_and_falls_silent(self):
        self.workflow("wf_two", [{"type": "started", "key": "k", "agentId": "quiet", "label": "survey", "phase": "Scan"}], {
            "quiet": ("survey", "Scan", [self.tool("2026-10-15T11:00:00Z", "Bash", {"description": "Scan"})]),
        })
        workflow, = self.state()["workflows"]
        self.assertEqual((workflow["name"], workflow["status"], workflow["started"]), ("wf_two", "silent", "2026-10-15T11:00:00Z"))
        self.assertEqual(workflow["agents"][0]["status"], "silent")

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
        self.assertEqual(listing, {"id": self.list_id, "provider": "claude", "sourceId": self.list_id,
                                  "title": self.list_id, "prompt": "", "project": "",
                                   "updatedAt": "2026-09-30T12:00:00Z", "recent": False, "active": False,
                                   "chartable": False, "taskCount": 0, "agentCount": 0, "headless": False})

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
        filler = "x" * 1000
        for _ in range(IDENTITY_TAIL_BYTES // len(filler) + 1):
            self.append({"timestamp": "2026-10-15T11:30:00Z", "type": "assistant", "message": {"content": filler}})
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
        unclosed = self.project_dir / "pasted-session.jsonl"
        self.append({"type": "user", "message": {"content": "<pasted_content id=\"2\">\n<b>\nTraceback line\nMore"}},
                    path=unclosed)
        title = next(item["title"] for item in self.store.lists() if item["id"] == "pasted-session")
        self.assertEqual(title, "Traceback line")

    def test_identity_follows_the_latest_title_record_and_working_directory(self):
        self.append({"type": "user", "cwd": "/old/place", "message": {"content": "First prompt"}},
                    {"type": "custom-title", "customTitle": "Early name", "sessionId": self.list_id})
        for _ in range(IDENTITY_SCAN_LINES):
            self.append({"type": "assistant", "cwd": "/old/place", "message": {"content": "work"}})
        self.assertEqual(self.store.lists()[0]["title"], "Early name")
        self.append({"type": "custom-title", "customTitle": "Renamed", "sessionId": self.list_id},
                    {"type": "assistant", "cwd": "/new/place", "message": {"content": "moved"}})
        listing = self.store.lists()[0]
        self.assertEqual((listing["title"], listing["project"]), ("Renamed", "/new/place"))
        state = self.state()
        self.assertEqual((state["title"], state["project"]), ("Renamed", "/new/place"))
        self.append({"type": "custom-title", "customTitle": "Renamed again", "sessionId": self.list_id})
        self.assertEqual(self.store.lists()[0]["title"], "Renamed again")

    def test_index_says_which_sessions_can_draw_a_chart_and_which_are_active(self):
        self.task("1")
        self.task("2")
        self.append({"type": "user", "entrypoint": "cli", "cwd": "/work", "message": {"content": "Plan the release"}},
                    {"type": "custom-title", "customTitle": "release.1015-0900.abcd"})
        agents_session = self.project_dir / "agents-session.jsonl"
        self.append({"type": "user", "entrypoint": "cli", "message": {"content": "Delegate"}}, path=agents_session)
        agents_dir = agents_session.with_suffix("") / "subagents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "agent-a1.jsonl").write_text("")
        (agents_dir / "agent-a1.meta.json").write_text("{}")
        gate = self.project_dir / "gate-run.jsonl"
        self.append({"type": "user", "entrypoint": "sdk-cli", "message": {"content": "Judge the staged diff"}}, path=gate)
        old_plan = self.root / "tasks" / "old-plan"
        old_plan.mkdir()
        (old_plan / "1.json").write_text(json.dumps({"id": "1", "subject": "Old work", "status": "completed"}))
        old_transcript = self.project_dir / "old-plan.jsonl"
        self.append({"type": "user", "cwd": "/old/work", "message": {"content": "Old plan"}}, path=old_transcript)
        recently = (self.now - timedelta(minutes=ACTIVE_SESSION_MINUTES)).timestamp()
        quiet = (self.now - timedelta(minutes=ACTIVE_SESSION_MINUTES + 1)).timestamp()
        long_ago = (self.now - timedelta(days=30)).timestamp()
        for path, moment in ((self.transcript, recently), (self.task_dir / "1.json", quiet), (self.task_dir / "2.json", quiet),
                             (agents_session, quiet), (gate, recently), (old_transcript, long_ago),
                             (old_plan / "1.json", long_ago)):
            os.utime(path, (moment, moment))
        with patch("live_gantt.sources.datetime", wraps=datetime) as clock:
            clock.now.return_value = self.now
            by_id = {listing["id"]: listing for listing in self.store.lists()}

        def summary(list_id):
            listing = by_id[list_id]
            return {key: listing[key] for key in ("chartable", "taskCount", "agentCount", "headless", "active")}

        self.assertEqual(summary(self.list_id), {"chartable": True, "taskCount": 2, "agentCount": 0,
                                                 "headless": False, "active": True})
        self.assertEqual(summary("agents-session"), {"chartable": True, "taskCount": 0, "agentCount": 1,
                                                     "headless": False, "active": False})
        self.assertEqual(summary("gate-run"), {"chartable": False, "taskCount": 0, "agentCount": 0,
                                               "headless": True, "active": True})
        self.assertEqual(by_id[self.list_id]["title"], "release.1015-0900.abcd")
        self.assertEqual(by_id[self.list_id]["prompt"], "Plan the release")
        self.assertEqual(by_id["agents-session"]["prompt"], "")
        self.assertFalse(by_id["old-plan"]["recent"])
        self.assertEqual((by_id["old-plan"]["title"], by_id["old-plan"]["project"]), ("Old plan", "/old/work"))


if __name__ == "__main__":
    unittest.main()
