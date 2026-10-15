"""Behavioral contracts for task projections."""

from datetime import datetime, timedelta, timezone
import math
import unittest

from live_gantt.schedule import project


NOW = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)


def timestamp(hours=0):
    return (NOW + timedelta(hours=hours)).isoformat()


def task(task_id, hours=1, *, lane="Main", status="pending", **fields):
    estimate = None if hours is None else {"low": hours, "likely": hours, "high": hours}
    item = {
        "id": str(task_id), "subject": f"Task {task_id}", "description": "",
        "activeForm": "", "status": status, "blocks": [], "blockedBy": [],
        "metadata": {}, "weight": 1.0, "milestone": "", "lane": lane,
        "progress": 0, "liveProgress": 0, "estimate": estimate,
        "created": None, "started": None, "ended": None, "history": [],
        "warnings": [], "agents": [], "runs": [],
    }
    item.update(fields)
    return item


def projected_tasks(items):
    return {item["id"]: item for item in project(items, NOW)["tasks"]}


class ProjectionTests(unittest.TestCase):
    def test_lane_chain_uses_natural_id_order(self):
        items = [task(10), task(2, 2), task(1)]
        actual = projected_tasks(items)
        self.assertEqual(actual["1"]["projectedStart"], timestamp())
        self.assertEqual(actual["2"]["projectedStart"], timestamp(1))
        self.assertEqual(actual["10"]["projectedStart"], timestamp(3))
        self.assertEqual(actual["10"]["projectedEnd"], timestamp(4))
        self.assertNotIn("projectedEnd", items[0])

    def test_blocker_in_another_lane_delays_the_lane_chain(self):
        actual = projected_tasks([
            task(1, 2, lane="Design"),
            task(2, lane="Build", blockedBy=["1"]),
            task(3, lane="Build"),
        ])
        self.assertEqual(actual["2"]["projectedStart"], timestamp(2))
        self.assertEqual(actual["3"]["projectedStart"], timestamp(3))
        self.assertFalse(actual["2"]["blocked"])
        self.assertEqual(actual["2"].get("queued"), True)
        self.assertEqual(actual["2"].get("waitingFor"), ["1"])
        self.assertEqual(actual["3"].get("waitingFor"), ["2"])

    def test_unestimated_task_leaves_followers_unscheduled(self):
        actual = projected_tasks([
            task(1, None), task(2), task(3, lane="Other", blockedBy=["1"]),
        ])
        self.assertIsNone(actual["1"]["projectedEnd"])
        self.assertIn("unestimated", actual["1"]["markers"])
        self.assertIsNone(actual["2"]["projectedStart"])
        self.assertIn("after an unestimated task", actual["2"]["markers"])
        self.assertIsNone(actual["3"]["projectedStart"])
        for item in actual.values():
            self.assertFalse(item["blocked"])
            self.assertEqual(item.get("queued"), False)
            self.assertEqual(item.get("waitingFor"), [])
        self.assertIsNone(project([task(1, None), task(2)], NOW)["milestones"][0]["p50"])

    def test_full_live_progress_has_no_remaining_duration(self):
        actual = projected_tasks([
            task(1, 2, status="in_progress", started=timestamp(-3), progress=75, liveProgress=100),
        ])["1"]
        self.assertEqual(actual["projectedEnd"], timestamp())
        self.assertEqual(actual["effectiveProgress"], 100)
        self.assertFalse(actual["overrun"])

    def test_past_mean_without_progress_is_overrunning(self):
        actual = projected_tasks([
            task(1, 2, status="in_progress", started=timestamp(-3)),
        ])["1"]
        self.assertTrue(actual["overrun"])
        self.assertEqual(actual["projectedStart"], timestamp())
        self.assertEqual(actual["projectedEnd"], timestamp(2))

    def test_p90_sums_remaining_variance_along_lane(self):
        estimate = {"low": 1, "likely": 2, "high": 3}
        model = project([
            task(1, estimate=estimate, status="in_progress", progress=50, started=timestamp(-1)),
            task(2, estimate=estimate),
        ], NOW)
        milestone = model["milestones"][0]
        self.assertEqual(milestone["p50"], timestamp(3))
        expected_hours = 3 + 1.2816 * math.sqrt((1 / 6) ** 2 + (1 / 3) ** 2)
        self.assertEqual(milestone["p90"], timestamp(expected_hours))

    def test_weighted_completion_uses_status_and_explicit_progress(self):
        model = project([
            task(1, status="completed", weight=2, started=timestamp(-2), ended=timestamp(-1)),
            task(2, status="in_progress", weight=2, progress=50, liveProgress=100),
            task(3, weight=2, progress=100),
        ], NOW)
        self.assertEqual(model["milestones"][0]["progress"], 50)
        self.assertTrue(model["milestones"][0]["hasRemaining"])

    def test_completed_without_history_does_not_block_following_task(self):
        actual = projected_tasks([task(1, None, status="completed"), task(2)])
        self.assertIn("no history", actual["1"]["markers"])
        self.assertEqual(actual["2"]["projectedStart"], timestamp())

    def test_cycles_are_reported_without_guessing_projection(self):
        model = project([
            task(1, lane="A", blockedBy=["2"]),
            task(2, lane="B", blockedBy=["1"]),
            task(3, lane="A"),
        ], NOW)
        self.assertTrue(model["warnings"])
        self.assertTrue(all(item["projectedEnd"] is None for item in model["tasks"]))
        self.assertTrue(all(item["warnings"] for item in model["tasks"]))
        self.assertTrue(all(item["blocked"] for item in model["tasks"]))
        self.assertTrue(all(item.get("queued") is False for item in model["tasks"]))
        self.assertTrue(all(item.get("waitingFor") == [] for item in model["tasks"]))

    def test_missing_blocker_is_reported_without_guessing_projection(self):
        model = project([task(1, blockedBy=["gone"]), task(2)], NOW)
        self.assertTrue(all(item["projectedEnd"] is None for item in model["tasks"]))
        self.assertTrue(all(item["blocked"] for item in model["tasks"]))
        self.assertTrue(all(item.get("queued") is False for item in model["tasks"]))
        self.assertTrue(all(item.get("waitingFor") == [] for item in model["tasks"]))
        self.assertTrue(model["warnings"])

    def test_completed_milestone_retains_actual_finish(self):
        model = project([task(1, status="completed", started=timestamp(-2), ended=timestamp(-1))], NOW)
        self.assertEqual(model["milestones"][0]["p50"], timestamp(-1))
        self.assertEqual(model["milestones"][0]["p90"], timestamp(-1))
        self.assertFalse(model["milestones"][0]["hasRemaining"])

    def test_milestone_estimate_flag_covers_only_remaining_work(self):
        model = project([
            task(1, status="completed", milestone="Unknown", lane="Unknown"),
            task(2, None, milestone="Unknown", lane="Unknown"),
            task(3, None, milestone="Mixed", lane="Mixed"),
            task(4, milestone="Mixed", lane="Mixed"),
            task(5, status="completed", milestone="Done", lane="Done"),
        ], NOW)
        milestones = {item["name"]: item for item in model["milestones"]}
        self.assertEqual(milestones["Unknown"].get("hasEstimate"), False)
        self.assertEqual(milestones["Mixed"].get("hasEstimate"), True)
        self.assertEqual(milestones["Done"].get("hasEstimate"), False)

    def test_completed_task_does_not_cut_the_pending_lane_chain(self):
        actual = projected_tasks([
            task(1, 2),
            task(2, status="completed", started=timestamp(-2), ended=timestamp(-1)),
            task(3),
        ])
        self.assertEqual(actual["3"]["projectedStart"], timestamp(2))

    def test_pending_work_waits_for_running_task_with_a_later_id(self):
        actual = projected_tasks([
            task(1), task(2, 2, status="in_progress", started=timestamp(-1)), task(3),
        ])
        self.assertEqual(actual["1"]["projectedStart"], timestamp(2))
        self.assertEqual(actual["3"]["projectedStart"], timestamp(3))
        self.assertFalse(actual["1"]["blocked"])
        self.assertEqual(actual["1"].get("queued"), True)
        self.assertEqual(actual["1"].get("waitingFor"), ["2"])

    def test_queue_excludes_prerequisites_available_now(self):
        actual = projected_tasks([
            task(1, lane="Done", status="completed", ended=timestamp(-1)),
            task(2, 0, lane="Zero", status="in_progress", started=timestamp(-1)),
            task(3, lane="Build", blockedBy=["1", "2"]),
        ])["3"]
        self.assertEqual(actual["projectedStart"], timestamp())
        self.assertFalse(actual["blocked"])
        self.assertEqual(actual.get("queued"), False)
        self.assertEqual(actual.get("waitingFor"), [])

    def test_multiple_running_tasks_are_retained_and_warned(self):
        model = project([
            task(1), task(2, 2, status="in_progress", started=timestamp(-1)),
            task(3, 3, status="in_progress", started=timestamp(-1)),
        ], NOW)
        actual = {item["id"]: item for item in model["tasks"]}
        self.assertEqual(actual["1"]["projectedStart"], timestamp(3))
        self.assertEqual(actual["2"]["projectedEnd"], timestamp(2))
        self.assertEqual(actual["3"]["projectedEnd"], timestamp(3))
        self.assertTrue(model["warnings"])

    def test_unrepresentable_estimate_warns_and_leaves_followers_unknown(self):
        model = project([task(1, 1e308), task(2)], NOW)
        self.assertIsNone(model["tasks"][0]["projectedEnd"])
        self.assertIsNone(model["tasks"][1]["projectedEnd"])
        self.assertTrue(model["tasks"][0]["warnings"])
        self.assertTrue(model["warnings"])
        self.assertTrue(math.isfinite(model["tasks"][0]["estimateMean"]))

    def test_unrepresentable_uncertainty_warns_without_losing_mean(self):
        near_limit = datetime(9999, 12, 31, tzinfo=timezone.utc)
        model = project([task(1, estimate={"low": 0, "likely": 0, "high": 120})], near_limit)
        self.assertIsNotNone(model["milestones"][0]["p50"])
        self.assertIsNone(model["milestones"][0]["p90"])
        self.assertTrue(model["warnings"])

    def test_large_finite_weights_preserve_the_weighted_share(self):
        model = project([
            task(1, status="completed", weight=1e308, started=timestamp(-2), ended=timestamp(-1)),
            task(2, weight=1e308),
        ], NOW)
        self.assertEqual(model["milestones"][0]["progress"], 50)

    def test_overrun_deadline_preserves_the_strict_time_boundary(self):
        running = task(1, 2, status="in_progress", started=timestamp(-2))
        at_deadline = project([running], NOW)["tasks"][0]
        self.assertEqual(at_deadline["overrunAt"], timestamp())
        self.assertFalse(at_deadline["overrun"])
        past_deadline = project([running], NOW + timedelta(microseconds=1))["tasks"][0]
        self.assertTrue(past_deadline["overrun"])
        for item in (
            task(2, 2, status="in_progress", started=timestamp(-2), progress=1),
            task(3, 2, status="in_progress"),
            task(4, 1e308, status="in_progress", started=timestamp(-2)),
            task(5, 2),
        ):
            with self.subTest(task_id=item["id"]):
                self.assertIsNone(project([item], NOW)["tasks"][0]["overrunAt"])

    def test_unusual_task_ids_cannot_crash_natural_ordering(self):
        for unusual_id in ("²", "9" * 5000):
            with self.subTest(id_kind="unicode" if len(unusual_id) == 1 else "long decimal"):
                model = project([task(unusual_id), task(2)], NOW)
                self.assertEqual({item["id"] for item in model["tasks"]}, {unusual_id, "2"})
                self.assertEqual(model["tasks"][0]["id"], "2")

    def test_dependency_uncertainty_follows_the_controlling_path(self):
        estimate = {"low": 1, "likely": 2, "high": 3}
        model = project([
            task(1, estimate=estimate, lane="A"),
            task(2, estimate=estimate, lane="B", blockedBy=["1"]),
            task(3, estimate=estimate, lane="B", blockedBy=["1"]),
        ], NOW)
        expected_hours = 6 + 1.2816 * math.sqrt(3 * (1 / 3) ** 2)
        self.assertEqual(model["milestones"][0]["p90"], timestamp(expected_hours))


if __name__ == "__main__":
    unittest.main()
