"""Boundary contracts for the freshness rules shared by the chart and the hooks."""

from datetime import datetime, timedelta, timezone
import unittest

from live_gantt.freshness import ACTIVE_SESSION_MINUTES, STALE_PROGRESS_MINUTES, assess


NOW = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)
ESTIMATE = {"low": 1, "likely": 2, "high": 3}
ONE_SECOND = timedelta(seconds=1)


def ago(minutes=0, seconds=0):
    return NOW - timedelta(minutes=minutes, seconds=seconds)


def iso(moment):
    return moment.isoformat().replace("+00:00", "Z")


def task(task_id="1", *, status="pending", metadata=None, updated=NOW, **fields):
    metadata = {"estimate": ESTIMATE, "lane": "Build"} if metadata is None else metadata
    item = {
        "id": task_id, "status": status, "metadata": metadata,
        "estimate": metadata.get("estimate"), "readable": True,
        "updatedAt": iso(updated), "runs": [],
    }
    item.update(fields)
    return item


def running(task_id="1", *, updated=NOW, **metadata):
    return task(task_id, status="in_progress", updated=updated,
                metadata={"estimate": ESTIMATE, "lane": "Build", "progress": 10, **metadata})


def rules(tasks, transcript_updated=NOW, now=NOW):
    return [(finding["rule"], finding["taskId"]) for finding in assess(tasks, transcript_updated, now)["findings"]]


class SchedulingMetadataRuleTests(unittest.TestCase):
    def test_unfinished_task_without_estimate_or_lane_is_a_finding(self):
        self.assertEqual(rules([task(metadata={"lane": "Build"})]), [("missing-schedule", "1")])
        self.assertEqual(rules([task(metadata={"estimate": ESTIMATE})]), [("missing-schedule", "1")])
        self.assertEqual(rules([task(metadata={"estimate": ESTIMATE, "lane": ""})]), [("missing-schedule", "1")])
        self.assertEqual(rules([task()]), [])

    def test_message_names_the_task_and_each_missing_field(self):
        finding, = assess([task("7", metadata={})], NOW, NOW)["findings"]
        self.assertEqual(finding["message"], "Task #7 lacks estimate and lane metadata.")

    def test_invalid_estimate_counts_as_missing(self):
        invalid = task(metadata={"estimate": {"likely": 2}, "lane": "Build"}, estimate=None)
        self.assertEqual(rules([invalid]), [("missing-schedule", "1")])

    def test_completed_deleted_and_unreadable_tasks_are_ignored(self):
        self.assertEqual(rules([
            task("1", status="completed", metadata={}),
            task("2", status="deleted", metadata={}),
            task("3", metadata={}, readable=False),
        ]), [])


class ProgressRuleTests(unittest.TestCase):
    def test_progress_untouched_for_exactly_the_limit_while_session_active_is_stale(self):
        limit = timedelta(minutes=STALE_PROGRESS_MINUTES)
        self.assertEqual(rules([running(updated=NOW - limit)]), [("stale-progress", "1")])
        self.assertEqual(rules([running(updated=NOW - limit + ONE_SECOND)]), [])

    def test_stale_progress_message_gives_whole_minutes(self):
        finding, = assess([running("4", updated=ago(23, 30))], NOW, NOW)["findings"]
        self.assertEqual(finding["message"], "Task #4 is in progress but has not been updated for 23 minutes.")

    def test_inactive_session_never_reports_stale_progress(self):
        window = timedelta(minutes=ACTIVE_SESSION_MINUTES)
        old_task = running(updated=ago(60))
        self.assertEqual(rules([old_task], transcript_updated=NOW - window), [("stale-progress", "1")])
        self.assertEqual(rules([old_task], transcript_updated=NOW - window - ONE_SECOND), [])
        self.assertEqual(rules([old_task], transcript_updated=None), [])

    def test_in_progress_task_needs_numeric_progress(self):
        for value in (None, "50", True, float("nan")):
            with self.subTest(progress=value):
                metadata = {"estimate": ESTIMATE, "lane": "Build"}
                if value is not None:
                    metadata["progress"] = value
                self.assertEqual(rules([task(status="in_progress", metadata=metadata)]),
                                 [("missing-progress", "1")])
        self.assertEqual(rules([running(progress=0)]), [])
        self.assertEqual(rules([task(status="pending")]), [])


class SummaryTests(unittest.TestCase):
    def test_summary_reports_activity_last_plan_change_and_staleness(self):
        summary = assess([task("1", updated=ago(3)), task("2", updated=ago(9))], ago(1), NOW)
        self.assertEqual(summary, {"stale": False, "findings": [],
                                   "sinceChangeSeconds": 180, "sessionActive": True})
        summary = assess([task("1", metadata={})], None, NOW)
        self.assertTrue(summary["stale"])
        self.assertFalse(summary["sessionActive"])
        self.assertIsNone(assess([], NOW, NOW)["sinceChangeSeconds"])


if __name__ == "__main__":
    unittest.main()
