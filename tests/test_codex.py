import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from live_gantt.codex import CodexSourceStore, USAGE_SNAPSHOT_LIMIT, USAGE_TAIL_BYTES, USAGE_TRANSCRIPT_LIMIT


class CodexSourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.now = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
        self.store = CodexSourceStore(self.root)

    def record(self, kind, payload, at="2026-10-18T11:59:00Z"):
        return {"timestamp": at, "type": kind, "payload": payload}

    def session(self, identifier, parent=None, archived=False):
        path = self.root / ("archived_sessions" if archived else "sessions/2026/10/18") / f"rollout-{identifier}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        metadata = {"id": identifier, "cwd": "/example/project", "source": "cli"}
        if parent:
            metadata["source"] = {"subagent": {"thread_spawn": {"parent_thread_id": parent}}}
        self.append(path, self.record("session_meta", metadata))
        return path

    def append(self, path, *records):
        with path.open("a") as stream:
            for record in records:
                stream.write(json.dumps(record) + "\n")

    def tokens(self, total, at="2026-10-18T11:59:00Z", percent=20):
        return self.record("event_msg", {"type": "token_count", "info": {"total_token_usage": {
            "input_tokens": total - 20, "cached_input_tokens": 10, "output_tokens": 20,
            "reasoning_output_tokens": 5, "total_tokens": total}}, "rate_limits": {
                "limit_id": "codex", "primary": {"used_percent": percent, "window_minutes": 60,
                                                      "resets_at": 1792332000}}}, at)

    def plan(self, steps, at="2026-10-18T11:59:00Z"):
        return self.record("event_msg", {"type": "plan_update", "explanation": "Plan changed",
                                        "plan": [{"step": text, "status": status} for text, status in steps]}, at)

    def test_native_plan_replaces_current_steps_and_keeps_ids_across_reorder(self):
        path = self.session("plan")
        self.append(path, self.plan([("Inspect", "completed"), ("Implement", "in_progress")]))
        state = self.store.state("plan", self.now)
        first = {task["subject"]: task for task in state["tasks"]}
        self.assertEqual({key: task["status"] for key, task in first.items()},
                         {"Inspect": "completed", "Implement": "in_progress"})
        self.assertTrue(all(task["estimate"] is None and not task["blockedBy"] for task in first.values()))
        self.assertTrue(all(task["lane"] == "Native plan" for task in first.values()))
        self.append(path, self.plan([("Implement", "completed"), ("Inspect", "completed"),
                                     ("Verify", "in_progress")], "2026-10-18T11:59:20Z"))
        second = {task["subject"]: task for task in self.store.state("plan", self.now)["tasks"]}
        self.assertEqual(first["Implement"]["id"], second["Implement"]["id"])
        self.assertEqual(second["Implement"]["status"], "completed")
        self.assertEqual(self.store.lists()[0]["taskCount"], 3)
        second["Implement"]["metadata"]["lane"] = "mutated"
        self.assertEqual(self.store.state("plan", self.now)["tasks"][0]["metadata"]["lane"], "Native plan")

    def test_plan_tool_requires_successful_matching_result(self):
        path = self.session("tool-plan")
        call = self.record("response_item", {"type": "function_call", "name": "update_plan",
            "call_id": "plan-call", "arguments": json.dumps({"plan": [{"step": "Inspect", "status": "pending"}]})})
        self.append(path, call)
        self.assertEqual(self.store.state("tool-plan", self.now)["tasks"], [])
        self.append(path, self.record("response_item", {"type": "function_call_output",
            "call_id": "unrelated", "output": "Plan updated"}))
        self.assertEqual(self.store.state("tool-plan", self.now)["tasks"], [])
        self.append(path, self.record("response_item", {"type": "function_call_output",
            "call_id": "plan-call", "output": "Error: rejected"}))
        self.assertEqual(self.store.state("tool-plan", self.now)["tasks"], [])
        self.append(path, call, self.record("response_item", {"type": "function_call_output",
            "call_id": "plan-call", "output": "Plan updated"}))
        self.assertEqual(self.store.state("tool-plan", self.now)["tasks"][0]["subject"], "Inspect")

    def test_plan_events_win_over_older_acknowledgments_and_do_not_repeat_history(self):
        path = self.session("plan-order")
        self.append(path, self.record("response_item", {"type": "function_call", "name": "update_plan",
            "call_id": "older", "arguments": json.dumps({"plan": [{"step": "Old", "status": "pending"}]})}),
            self.plan([("Current", "in_progress")]),
            self.record("response_item", {"type": "function_call_output", "call_id": "older", "output": "Plan updated"}))
        tasks = self.store.state("plan-order", self.now)["tasks"]
        self.assertEqual([task["subject"] for task in tasks], ["Current"])
        self.append(path, self.record("response_item", {"type": "function_call", "name": "update_plan",
            "call_id": "current", "arguments": json.dumps({"plan": [{"step": "Current", "status": "in_progress"}]})}),
            self.plan([("Current", "in_progress")], "2026-10-18T11:59:10Z"),
            self.record("response_item", {"type": "function_call_output", "call_id": "current", "output": "Plan updated"}))
        current = self.store.state("plan-order", self.now)["tasks"][0]
        self.assertEqual(current["history"], tasks[0]["history"])

    def test_plan_survives_turn_completion_and_matching_abort_stops_activity(self):
        path = self.session("turns")
        self.append(path, self.record("event_msg", {"type": "task_started", "turn_id": "one"}),
                    self.plan([("Implement", "in_progress")]),
                    self.record("event_msg", {"type": "task_complete", "turn_id": "one"}))
        self.assertEqual(self.store.state("turns", self.now)["tasks"][0]["status"], "in_progress")
        self.append(path, self.record("event_msg", {"type": "task_started", "turn_id": "two"}),
                    self.record("event_msg", {"type": "turn_aborted", "turn_id": "one", "reason": "interrupted"}))
        self.assertEqual(self.store.state("turns", self.now)["agents"][0]["status"], "running")
        self.append(path, self.record("event_msg", {"type": "turn_aborted", "turn_id": "two", "reason": "interrupted"}))
        stopped = self.store.state("turns", self.now)
        self.assertEqual(stopped["agents"][0]["status"], "stopped")
        self.assertEqual(stopped["tasks"][0]["status"], "in_progress")
        self.append(path, self.record("event_msg", {"type": "task_started", "turn_id": "three"}))
        self.assertEqual(self.store.state("turns", self.now)["agents"][0]["status"], "running")
        self.append(path, self.record("event_msg", {"type": "task_complete", "turn_id": "three"}),
                    self.record("event_msg", {"type": "turn_aborted", "turn_id": "two", "reason": "interrupted"}))
        self.assertEqual(self.store.state("turns", self.now)["agents"][0]["status"], "idle")

    def test_runtime_failure_is_preserved_without_raw_error_and_new_turn_clears_it(self):
        path = self.session("runtime-failure")
        self.append(path, self.record("event_msg", {"type": "task_started", "turn_id": "one"}),
                    self.record("event_msg", {"type": "task_complete", "turn_id": "one",
                        "error": {"message": "private secret", "codex_error_info": "usage_limit_exceeded"}}))
        state = self.store.state("runtime-failure", self.now)
        self.assertEqual(state["_runtime"]["phase"], "failed")
        self.assertEqual(state["agents"][0]["status"], "failed")
        self.assertNotIn("private secret", json.dumps(state))
        self.append(path, self.record("event_msg", {"type": "task_started", "turn_id": "two"},
                                     "2026-10-18T11:59:10Z"))
        runtime = self.store.state("runtime-failure", self.now)["_runtime"]
        self.assertEqual(runtime["phase"], "working")
        self.assertIsNone(runtime["detail"])

    def test_runtime_meaningful_activity_excludes_usage_and_configuration(self):
        path = self.session("runtime-activity")
        self.assertIsNone(self.store.state("runtime-activity", self.now)["_runtime"]["lastActivityAt"])
        self.append(path, self.record("event_msg", {"type": "task_started", "turn_id": "one"}),
                    self.tokens(100, "2026-10-18T11:59:20Z"),
                    self.record("turn_context", {"model": "fixture"}, "2026-10-18T11:59:30Z"))
        state = self.store.state("runtime-activity", self.now)
        self.assertEqual(state["_runtime"]["lastActivityAt"], "2026-10-18T11:59:00Z")
        self.assertEqual(state["agents"][0]["lastActivityAt"], "2026-10-18T11:59:00Z")
        for index, (kind, payload) in enumerate((("response_item", {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Working"}]}),
                              ("response_item", {"type": "function_call", "name": "exec_command", "call_id": "tool", "arguments": "{}"}),
                              ("response_item", {"type": "function_call_output", "call_id": "tool", "output": "ok"}),
                              ("event_msg", {"type": "user_message", "message": "fixture"}))):
            at = f"2026-10-18T11:59:{40 + index:02d}Z"
            self.append(path, self.record(kind, payload, at))
            self.assertEqual(self.store.state("runtime-activity", self.now)["_runtime"]["lastActivityAt"],
                             at)

    def test_empty_messages_and_incomplete_tool_records_do_not_refresh_activity(self):
        cases = (
            ("response_item", {"type": "message", "role": "assistant", "content": []}),
            ("response_item", {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "  "}]}),
            ("response_item", {"type": "agent_message", "content": []}),
            ("event_msg", {"type": "user_message", "message": ""}),
            ("event_msg", {"type": "agent_message", "message": "  "}),
            ("event_msg", {"type": "user_message", "message": None, "images": None}),
            ("response_item", {"type": "function_call"}),
            ("response_item", {"type": "function_call", "call_id": "tool", "name": "exec_command"}),
            ("response_item", {"type": "function_call", "name": "exec_command", "arguments": "{}"}),
            ("response_item", {"type": "function_call_output", "call_id": "tool", "output": ""}),
            ("response_item", {"type": "function_call_output", "output": "ok"}),
            ("response_item", {"type": "custom_tool_call", "call_id": "tool", "name": "apply_patch", "input": "  "}),
            ("response_item", {"type": "custom_tool_call_output", "call_id": "tool", "output": []}),
        )
        for index, (kind, payload) in enumerate(cases):
            with self.subTest(payload=payload):
                identifier = f"empty-runtime-{index}"
                path = self.session(identifier)
                self.append(path, self.record("event_msg", {"type": "task_started", "turn_id": "one"},
                                              "2026-10-18T11:00:00Z"),
                            self.record(kind, payload, "2026-10-18T11:59:50Z"))
                state = self.store.state(identifier, self.now)
                self.assertEqual(state["_runtime"]["lastActivityAt"], "2026-10-18T11:00:00Z")
                self.assertEqual(state["agents"][0]["status"], "silent")

    def test_runtime_abort_matches_turn_and_budget_abort_is_failure(self):
        path = self.session("runtime-abort")
        self.append(path, self.record("event_msg", {"type": "task_started", "turn_id": "current"}),
                    self.record("event_msg", {"type": "turn_aborted", "turn_id": "older", "reason": "budget_limited"},
                                "2026-10-18T11:59:20Z"))
        runtime = self.store.state("runtime-abort", self.now)["_runtime"]
        self.assertEqual(runtime["phase"], "working")
        self.assertEqual(runtime["lastActivityAt"], "2026-10-18T11:59:00Z")
        self.append(path, self.record("event_msg", {"type": "task_complete"}, "2026-10-18T11:59:30Z"))
        self.assertEqual(self.store.state("runtime-abort", self.now)["_runtime"]["phase"], "working")
        self.append(path, self.record("event_msg", {"type": "turn_aborted", "turn_id": "current", "reason": "budget_limited"}))
        self.assertEqual(self.store.state("runtime-abort", self.now)["_runtime"]["phase"], "failed")
        self.append(path, self.record("event_msg", {"type": "task_started", "turn_id": "next"}),
                    self.record("event_msg", {"type": "turn_aborted", "turn_id": "next", "reason": "interrupted"}))
        self.assertEqual(self.store.state("runtime-abort", self.now)["_runtime"]["phase"], "interrupted")

    def test_runtime_input_request_is_unverified_and_matching_result_clears_it(self):
        path = self.session("runtime-input")
        def call(arguments, call_id="input"):
            return self.record("response_item", {"type": "function_call", "name": "request_user_input",
                                                 "call_id": call_id, "arguments": arguments})
        arguments = json.dumps({"questions": [{"id": "choice", "header": "Choice", "question": "Select?"}]})
        self.append(path, self.record("event_msg", {"type": "task_started", "turn_id": "one",
                                                    "collaboration_mode_kind": "default"}), call(arguments))
        self.assertEqual(self.store.state("runtime-input", self.now)["_runtime"]["phase"], "working")
        self.append(path, self.record("event_msg", {"type": "task_started", "turn_id": "two",
                                                    "collaboration_mode_kind": "plan"}), call("not-json"))
        self.assertEqual(self.store.state("runtime-input", self.now)["_runtime"]["phase"], "working")
        self.append(path, call(arguments))
        runtime = self.store.state("runtime-input", self.now)["_runtime"]
        self.assertEqual(runtime["phase"], "unknown")
        self.assertEqual(runtime["detail"], "Recorded input request has no recorded response")
        self.append(path, self.record("response_item", {"type": "function_call_output", "call_id": "other", "output": "ok"}))
        self.assertEqual(self.store.state("runtime-input", self.now)["_runtime"]["phase"], "unknown")
        self.append(path, self.record("response_item", {"type": "function_call_output", "call_id": "input",
                                                        "output": "request_user_input was cancelled"}))
        self.assertEqual(self.store.state("runtime-input", self.now)["_runtime"]["phase"], "working")
        self.append(path, call(arguments, "new-input"), self.record("event_msg", {"type": "task_complete", "turn_id": "two"}))
        self.assertEqual(self.store.state("runtime-input", self.now)["_runtime"]["phase"], "idle")

    def test_larger_same_inode_rewrite_replaces_plan_and_token_history(self):
        path = self.session("rewrite-plan")
        self.append(path, self.plan([("Old step", "completed")]), self.tokens(300))
        self.assertEqual(self.store.state("rewrite-plan", self.now)["tasks"][0]["subject"], "Old step")
        replacement = [self.record("session_meta", {"id": "rewrite-plan"}),
                       self.plan([("New step", "pending")]), self.tokens(100),
                       self.record("ignored", {"padding": "x" * 1000})]
        original_size = path.stat().st_size
        path.write_text("".join(json.dumps(record) + "\n" for record in replacement))
        self.assertGreater(path.stat().st_size, original_size)
        state = self.store.state("rewrite-plan", self.now)
        self.assertEqual(state["tasks"][0]["subject"], "New step")
        self.assertEqual(state["tokenUsage"]["totalTokens"], 100)

    def test_invalid_plan_is_ignored_and_empty_plan_clears_it(self):
        path = self.session("invalid-plan")
        self.append(path, self.plan([("Inspect", "pending")]))
        self.append(path, self.plan([("Changed", "not-a-status")]))
        self.assertEqual(self.store.state("invalid-plan", self.now)["tasks"][0]["subject"], "Inspect")
        self.append(path, self.plan([]))
        self.assertEqual(self.store.state("invalid-plan", self.now)["tasks"], [])

    def test_permission_errors_during_discovery_propagate_while_absence_is_empty(self):
        self.assertEqual(self.store.lists(), [])
        self.session("walk-error")
        scan = os.scandir
        def denied(path):
            if Path(path) == self.root / "sessions":
                raise PermissionError("fixture denial")
            return scan(path)
        with patch("live_gantt.codex.os.scandir", side_effect=denied):
            with self.assertRaises(PermissionError):
                self.store.lists()

    def test_unreadable_required_identity_and_selected_transcript_propagate(self):
        path = self.session("unreadable")
        original_open = Path.open
        def denied(target, *args, **kwargs):
            if target == path:
                raise PermissionError("fixture denial")
            return original_open(target, *args, **kwargs)
        with patch.object(Path, "open", denied):
            with self.assertRaises(PermissionError):
                self.store.lists()
        self.store.lists()
        with patch.object(Path, "open", denied):
            with self.assertRaises(PermissionError):
                self.store.state("unreadable", self.now)

    def test_parent_child_orphan_identity_and_turn_resume(self):
        root = self.session("root")
        child = self.session("child", "root")
        self.session("orphan", "missing")
        self.append(root, self.record("event_msg", {"type": "task_started", "turn_id": "one"}),
                    self.record("event_msg", {"type": "task_complete", "turn_id": "one"}))
        self.append(child, self.record("event_msg", {"type": "task_started", "turn_id": "child-turn"}))
        self.assertEqual({item["id"] for item in self.store.lists()}, {"root", "orphan"})
        state = self.store.state("root", self.now)
        self.assertEqual(state["provider"], "codex")
        self.assertEqual(state["sourceId"], "root")
        self.assertEqual(state["project"], "/example/project")
        self.assertEqual(state["tasks"], [])
        agents = {agent["id"]: agent for agent in state["agents"]}
        self.assertEqual(agents["root"]["agentType"], "session")
        self.assertEqual(agents["root"]["status"], "idle")
        self.assertEqual(agents["child"]["status"], "running")
        self.append(root, self.record("event_msg", {"type": "task_started", "turn_id": "two"}),
                    self.record("event_msg", {"type": "task_complete", "turn_id": "one"}))
        self.assertEqual(self.store.state("root", self.now)["agents"][0]["status"], "running")
        self.assertEqual(self.store.state("root", self.now + timedelta(hours=1))["agents"][0]["status"], "silent")

    def test_newest_cumulative_tokens_and_partial_append(self):
        path = self.session("tokens")
        self.append(path, self.tokens(100), self.tokens(100), self.tokens(300, "2026-10-18T11:59:20Z"),
                    self.tokens(200, "2026-10-18T11:58:00Z"))
        self.assertEqual(self.store.state("tokens", self.now)["tokenUsage"]["totalTokens"], 300)
        partial = json.dumps(self.record("token_usage_record", {"thread_token_usage": {
            "input_tokens": 480, "output_tokens": 20, "total_tokens": 500}}, "2026-10-18T11:59:30Z"))
        with path.open("a") as stream:
            stream.write("not-json\n" + partial[:40])
        self.assertEqual(self.store.state("tokens", self.now)["tokenUsage"]["totalTokens"], 300)
        with path.open("a") as stream:
            stream.write(partial[40:])
        state = self.store.state("tokens", self.now)
        self.assertEqual(state["tokenUsage"]["totalTokens"], 500)
        self.assertIsNone(state["tokenUsage"]["cacheWriteTokens"])

    def test_file_replacement_and_truncation_reset_caches(self):
        path = self.session("replace")
        self.append(path, self.tokens(300))
        self.assertEqual(self.store.state("replace", self.now)["tokenUsage"]["totalTokens"], 300)
        path.write_text(json.dumps(self.record("session_meta", {"id": "replace"})) + "\n")
        self.assertIsNone(self.store.state("replace", self.now)["tokenUsage"]["totalTokens"])
        replacement = path.with_suffix(".new")
        replacement.write_text(json.dumps(self.record("session_meta", {"id": "new"})) + "\n")
        replacement.replace(path)
        self.assertIsNone(self.store.state("replace", self.now))
        self.assertIsNotNone(self.store.state("new", self.now))

    def test_growing_in_place_replacement_cannot_reuse_another_session_identity(self):
        path = self.session("original")
        self.append(path, self.record("event_msg", {"type": "user_message", "message": "Original title"}),
                    self.plan([("Original work", "in_progress")]), self.tokens(100))
        self.assertEqual(self.store.state("original", self.now)["tasks"][0]["subject"], "Original work")
        previous = path.stat()
        replacement = [
            self.record("session_meta", {"id": "replacement", "cwd": "/replacement/project"}),
            self.record("event_msg", {"type": "user_message", "message": "Replacement session title"}),
            self.plan([("Replacement session work with a longer description", "pending")]),
            self.tokens(900),
        ]
        path.write_text("".join(json.dumps(record) + "\n" for record in replacement))
        self.assertEqual(path.stat().st_ino, previous.st_ino)
        self.assertGreater(path.stat().st_size, previous.st_size)

        listing = self.store.lists()
        self.assertEqual([(item["id"], item["title"]) for item in listing],
                         [("replacement", "Replacement session title")])
        self.assertIsNone(self.store.state("original", self.now))
        state = self.store.state("replacement", self.now)
        self.assertEqual(state["project"], "/replacement/project")
        self.assertEqual(state["tasks"][0]["subject"], "Replacement session work with a longer description")
        self.assertEqual(state["tokenUsage"]["totalTokens"], 900)

    def test_quota_native_role_snapshot_missing_window_and_validation(self):
        path = self.session("quota")
        self.append(path, self.tokens(100), self.tokens(100), self.tokens(120, "2026-10-18T11:59:10Z", 25),
                    self.record("event_msg", {"type": "token_count", "rate_limits": {
                        "limit_id": "codex", "secondary": {"used_percent": 0, "window_minutes": 10080,
                                                               "resets_at": 1792332000}}}, "2026-10-18T11:59:20Z"))
        snapshots = self.store.usage_snapshots()
        self.assertEqual(len(snapshots), 3)
        self.assertEqual(snapshots[0]["windows"][0]["id"], "primary")
        self.assertEqual(snapshots[0]["windows"][0]["windowMinutes"], 60)
        self.assertEqual(snapshots[-1]["windows"][0]["usedPercent"], 0)
        self.assertEqual(snapshots[-1]["windows"][0]["id"], "secondary")
        self.append(path, self.record("event_msg", {"type": "token_count", "rate_limits": {
            "primary": {"used_percent": float("nan"), "window_minutes": -1, "resets_at": "bad"}}},
            "2026-10-18T11:59:30Z"))
        self.assertEqual(self.store.usage_snapshots()[-1]["windows"], [])

    def test_usage_bootstrap_is_bounded_and_archived_is_on_selection_only(self):
        archive = self.session("archived", archived=True)
        self.append(archive, self.tokens(999, percent=99))
        for index in range(USAGE_TRANSCRIPT_LIMIT + 1):
            path = self.session(f"bounded-{index}")
            if index == USAGE_TRANSCRIPT_LIMIT:
                self.append(path, self.tokens(20, percent=1))
                with path.open("a") as stream:
                    stream.write(json.dumps({"type": "ignored", "payload": "x" * USAGE_TAIL_BYTES}) + "\n")
            self.append(path, self.tokens(100, percent=2))
            os.utime(path, (index + 100, index + 100))
        snapshots = self.store.usage_snapshots()
        self.assertEqual(len(snapshots), USAGE_TRANSCRIPT_LIMIT)
        self.assertNotIn("bounded-0", {value["sourceId"] for value in snapshots})
        self.assertTrue(all(value["windows"][0]["usedPercent"] == 2 for value in snapshots))
        self.assertNotIn("archived", {value["sourceId"] for value in snapshots})
        self.assertIsNotNone(self.store.state("archived", self.now))

    def test_missing_invalid_ids_and_source_mtime(self):
        self.assertEqual(self.store.lists(), [])
        self.assertEqual(self.store.usage_snapshots(), [])
        self.assertIsNone(self.store.state("../outside", self.now))
        self.session("bad/id")
        path = self.session("mtime")
        os.utime(path, (self.now.timestamp(), self.now.timestamp()))
        state = self.store.state("mtime", self.now)
        self.assertEqual(state["transcriptUpdatedAt"], "2026-10-18T12:00:00Z")
        self.assertTrue(state["active"])
        self.assertNotIn("bad/id", {item["id"] for item in self.store.lists()})

    def test_repeated_quota_records_preserve_change_history(self):
        path = self.session("repeated")
        self.append(path, self.tokens(100, percent=10), self.tokens(110, "2026-10-18T11:59:10Z", 20))
        self.store.usage_snapshots()
        self.append(path, *(self.tokens(110, "2026-10-18T11:59:20Z", 20)
                            for _ in range(USAGE_SNAPSHOT_LIMIT + 1)))
        snapshots = self.store.usage_snapshots()
        self.assertEqual([value["windows"][0]["usedPercent"] for value in snapshots], [10, 20])
        self.assertEqual(snapshots[-1]["capturedAt"], "2026-10-18T11:59:10Z")

    def test_same_size_rewrite_invalidates_usage_cache(self):
        path = self.session("rewrite")
        self.append(path, self.tokens(100, percent=10))
        self.assertEqual(self.store.usage_snapshots()[0]["windows"][0]["usedPercent"], 10)
        before = path.stat()
        path.write_text(path.read_text().replace('"used_percent": 10', '"used_percent": 20'))
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1))
        self.assertEqual(path.stat().st_size, before.st_size)
        snapshots = self.store.usage_snapshots()
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0]["windows"][0]["usedPercent"], 20)

    def test_archived_filename_cannot_select_another_session_identity(self):
        path = self.session("archive", archived=True)
        path.write_text(json.dumps(self.record("session_meta", {"id": "another"})) + "\n")
        self.assertIsNone(self.store.state("archive", self.now))

    def test_native_title_index_and_agent_nickname(self):
        self.session("named")
        child = self.session("nicknamed", "named")
        records = child.read_text().splitlines()
        metadata = json.loads(records[0])
        metadata["payload"]["agent_nickname"] = "Worker"
        child.write_text(json.dumps(metadata) + "\n")
        index = self.root / "session_index.jsonl"
        self.append(index, {"id": "named", "thread_name": "Repair queue ordering", "updated_at": "2026-10-18T12:00:00Z"})
        self.assertEqual(self.store.lists()[0]["title"], "Repair queue ordering")
        state = self.store.state("named", self.now)
        self.assertEqual(state["agents"][1]["description"], "Worker")
        self.append(index, {"id": "named", "thread_name": "Updated queue title", "updated_at": "2026-10-18T12:01:00Z"})
        self.assertEqual(self.store.state("named", self.now)["title"], "Updated queue title")

    def test_actual_user_message_supplies_bounded_title_fallback(self):
        path = self.session("prompt")
        self.append(path, self.record("event_msg", {"type": "user_message", "message": "Fix the queue\nMore details"}))
        self.assertEqual(self.store.lists()[0]["title"], "Fix the queue")

    def test_verified_thread_token_usage_cache_write_field(self):
        path = self.session("cache")
        self.append(path, self.record("token_usage_record", {"thread_token_usage": {
            "input_tokens": 50, "cached_input_tokens": 20, "cache_write_input_tokens": 10,
            "output_tokens": 5, "reasoning_output_tokens": 2, "total_tokens": 55}}))
        self.assertEqual(self.store.state("cache", self.now)["tokenUsage"]["cacheWriteTokens"], 10)

    def test_malformed_record_discriminators_do_not_break_state_or_quota(self):
        for kind, payload in (("event_msg", {"type": []}), ({}, {"type": "token_count"})):
            for boundary in ("state", "usage_snapshots"):
                with self.subTest(kind=kind, boundary=boundary):
                    path = self.session("malformed-types")
                    metadata = json.dumps(self.record("session_meta", {"id": "malformed-types"}))
                    path.write_text(metadata + "\n")
                    self.append(path, self.record(kind, payload), self.tokens(100))
                    store = CodexSourceStore(self.root)
                    if boundary == "state":
                        self.assertEqual(store.state("malformed-types", self.now)["tokenUsage"]["totalTokens"], 100)
                    else:
                        self.assertEqual(len(store.usage_snapshots()), 1)


if __name__ == "__main__":
    unittest.main()
