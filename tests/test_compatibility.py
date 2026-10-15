"""Exercise the real Claude Code file layout through source and projection boundaries."""

from datetime import datetime, timezone
from pathlib import Path
import unittest

from live_gantt.schedule import project
from live_gantt.sources import SourceStore


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "claude-code-2.1.292"
SESSION_ID = "11111111-1111-4111-8111-111111111111"
NOW = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)


class ClaudeCodeCompatibilityTests(unittest.TestCase):
    def test_recorded_file_shapes_preserve_history_agents_and_cross_lane_forecasts(self):
        """Pin the task-result text and file shapes read from Claude Code 2.1.292."""
        store = SourceStore(FIXTURE_ROOT)
        self.assertEqual([item["id"] for item in store.lists()], [SESSION_ID])
        state = store.state(SESSION_ID, NOW)
        self.assertIsNotNone(state)
        self.assertEqual(state["title"], "Prepare the example widget release")
        self.assertEqual(state["project"], "/example/widget")
        self.assertEqual(state["warnings"], [])
        tasks = {task["id"]: task for task in state["tasks"]}
        self.assertEqual(set(tasks), {"1", "2", "3"})
        self.assertEqual(tasks["1"]["created"], "2026-10-15T08:00:02Z")
        self.assertEqual(tasks["1"]["started"], "2026-10-15T09:00:00Z")
        self.assertEqual(tasks["1"]["ended"], "2026-10-15T10:00:00Z")
        self.assertEqual(tasks["1"]["status"], "completed")
        self.assertEqual(tasks["2"]["status"], "in_progress")
        self.assertEqual(tasks["3"]["status"], "pending")
        self.assertEqual(tasks["2"]["history"], [
            {"time": "2026-10-15T08:01:01Z", "event": "created"},
            {"time": "2026-10-15T11:00:00Z", "event": "started"},
            {"time": "2026-10-15T11:30:00Z", "event": "progress", "value": 50},
        ])
        self.assertEqual(tasks["3"]["blockedBy"], ["2"])
        self.assertTrue(all(task["warnings"] == [] for task in tasks.values()))

        assigned, = tasks["1"]["agents"]
        self.assertEqual(assigned["id"], "a1111111111111111")
        self.assertEqual(assigned["description"], "Check widget design")
        self.assertEqual(assigned["agentType"], "general-purpose")
        self.assertEqual(assigned["status"], "completed")
        self.assertEqual(assigned["started"], "2026-10-15T09:05:02Z")
        self.assertEqual(assigned["ended"], "2026-10-15T09:44:00Z")
        self.assertEqual(assigned["activity"], "Validate the widget design")
        detached, = state["agents"]
        self.assertEqual(detached["id"], "a2222222222222222")
        self.assertEqual(detached["status"], "running")
        self.assertIsNone(detached["ended"])
        self.assertEqual(detached["activity"], "Inspect release assets")

        projection = project(state["tasks"], NOW)
        scheduled = {task["id"]: task for task in projection["tasks"]}
        self.assertEqual(projection["warnings"], [])
        self.assertEqual(scheduled["2"]["projectedEnd"], "2026-10-15T13:00:00+00:00")
        self.assertEqual(scheduled["3"]["projectedStart"], scheduled["2"]["projectedEnd"])
        self.assertEqual(scheduled["3"]["projectedEnd"], "2026-10-15T14:00:00+00:00")
        self.assertEqual(scheduled["3"]["waitingFor"], ["2"])
        self.assertTrue(scheduled["3"]["queued"])
        self.assertFalse(scheduled["3"]["blocked"])
        milestone, = projection["milestones"]
        self.assertEqual(milestone["name"], "Release")
        self.assertEqual(milestone["progress"], 50)
        self.assertEqual(milestone["p50"], scheduled["3"]["projectedEnd"])
        self.assertEqual(milestone["p90"], milestone["p50"])
