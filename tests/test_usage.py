"""Quota history and forecasting contracts through the public store boundary."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from live_gantt.capture_usage import capture
from live_gantt.usage import UsageStore


NOW = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)


def iso(value):
    return value.isoformat().replace("+00:00", "Z")


def snapshot(percent, *, minutes=0, source="session-a", bucket="codex", role="primary",
             duration=300, reset=None):
    return {"sourceId": source, "bucketId": bucket, "capturedAt": iso(NOW + timedelta(minutes=minutes)),
            "windows": [{"id": role, "windowMinutes": duration, "usedPercent": percent,
                         "resetsAt": iso(reset or NOW + timedelta(hours=2))}]}


def provider(store, snapshots, now=NOW):
    return next(row for row in store.state(snapshots, now)["providers"] if row["provider"] == "codex")


class UsageTests(unittest.TestCase):
    def test_status_line_captures_match_a_reset_exactly(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for minutes, percent, shift in ((-10, 40, 0), (0, 60, 0.5)):
                capture({"session_id": "session-a", "rate_limits": {"five_hour": {
                    "used_percentage": percent,
                    "resets_at": (NOW + timedelta(hours=2, seconds=shift)).timestamp(),
                }}}, NOW + timedelta(minutes=minutes), root)
            state = UsageStore(root).state([], NOW)["providers"][0]
            self.assertEqual(state["windows"][0]["forecast"]["status"], "insufficient_history")

    def test_forecast_uses_percentage_points_and_recorded_changes(self):
        result = provider(UsageStore(), [snapshot(40, minutes=-10), snapshot(60)])
        window, = result["windows"]
        self.assertEqual(result["status"], "available")
        self.assertEqual(window["forecast"], {"status": "predicted", "exhaustsAt": "2026-10-18T12:20:00Z",
                                             "basis": "recorded_changes", "sampleSeconds": 600.0})
        self.assertEqual(window["freshnessBasis"], "recorded_change")
        self.assertIsNone(window["measuredAt"])

    def test_cached_samples_do_not_refresh_observation_or_create_pace(self):
        store = UsageStore()
        provider(store, [snapshot(40)], NOW)
        result = provider(store, [snapshot(40, minutes=6)], NOW + timedelta(minutes=6))
        window, = result["windows"]
        self.assertEqual(result["status"], "stale")
        self.assertEqual(window["capturedAt"], iso(NOW + timedelta(minutes=6)))
        self.assertEqual(window["observedAt"], iso(NOW))
        self.assertEqual(window["forecast"]["status"], "stale")
        self.assertEqual(window["freshnessBasis"], "unknown")

    def test_reset_regression_source_and_duration_break_history(self):
        changes = [dict(reset=NOW + timedelta(hours=3)), dict(source="session-b"),
                   dict(duration=10080), dict(bucket="other"), dict()]
        for fields in changes:
            with self.subTest(fields=fields):
                earlier = snapshot(70, minutes=-10)
                latest = snapshot(20, **fields)
                result = provider(UsageStore(), [earlier, latest])
                current = next(row for row in result["windows"] if row["bucketId"] == latest["bucketId"])
                self.assertEqual(current["forecast"]["status"], "insufficient_history")

    def test_reached_and_reset_first_and_reset_expiry(self):
        for percent, reset, expected in [(100, NOW + timedelta(hours=1), "reached"),
                                          (60, NOW + timedelta(minutes=15), "resets_first"),
                                          (100, NOW, "stale")]:
            with self.subTest(expected=expected):
                result = provider(UsageStore(), [snapshot(40, minutes=-10, reset=reset),
                                                 snapshot(percent, reset=reset)])
                forecast = result["windows"][0]["forecast"]
                self.assertEqual(forecast["status"], expected)
                self.assertEqual(forecast["exhaustsAt"], iso(NOW) if expected == "reached" else None)

    def test_minimum_span_horizon_and_mixed_staleness(self):
        short = provider(UsageStore(), [snapshot(40, minutes=-4), snapshot(60)])
        self.assertEqual(short["windows"][0]["forecast"]["status"], "insufficient_history")
        old = provider(UsageStore(), [snapshot(40, minutes=-31), snapshot(60)])
        self.assertEqual(old["windows"][0]["forecast"]["status"], "insufficient_history")
        long_window = snapshot(25, minutes=-10, bucket="weekly", duration=10080)
        mixed = provider(UsageStore(), [long_window, snapshot(0)])
        self.assertEqual(mixed["status"], "available")
        self.assertEqual([row["stale"] for row in mixed["windows"]], [False, True])
        self.assertEqual(mixed["windows"][1]["label"], "7 days")

    def test_new_complete_snapshot_missing_window_does_not_keep_old_quota(self):
        newest = snapshot(0)
        newest["windows"] = []
        result = provider(UsageStore(), [snapshot(60, minutes=-1), newest])
        self.assertEqual(result["status"], "unavailable")
        self.assertIsNone(result["windows"][0]["usedPercent"])
        self.assertEqual(result["windows"][0]["forecast"]["status"], "unavailable")

    def test_invalid_numbers_are_unavailable_and_never_sum_sessions(self):
        for invalid in (True, -1, 101, float("nan"), float("inf"), "20"):
            with self.subTest(invalid=invalid):
                result = provider(UsageStore(), [snapshot(invalid)])
                self.assertEqual(result["status"], "unavailable")
        latest = provider(UsageStore(), [snapshot(60, minutes=-1), snapshot(70, source="session-b")])
        self.assertEqual(latest["sourceId"], "session-b")
        self.assertEqual(latest["windows"][0]["usedPercent"], 70)

    def test_native_measurement_time_is_preserved_only_when_valid(self):
        sample = snapshot(40)
        sample["windows"][0]["measuredAt"] = iso(NOW - timedelta(minutes=6))
        result = provider(UsageStore(), [sample])
        self.assertEqual(result["status"], "stale")
        self.assertEqual(result["windows"][0]["freshnessBasis"], "provider_measurement")
        self.assertEqual(result["windows"][0]["measuredAt"], "2026-10-18T11:54:00Z")

    def test_absent_sources_return_two_explicit_unavailable_providers(self):
        result = UsageStore().state([], NOW)
        self.assertEqual([(row["provider"], row["status"]) for row in result["providers"]],
                         [("claude", "unavailable"), ("codex", "unavailable")])

    def test_huge_numbers_are_rejected_without_overflow(self):
        result = provider(UsageStore(), [snapshot(10 ** 1000)])
        self.assertEqual(result["status"], "unavailable")

    def test_newest_snapshot_is_chosen_before_input_history_is_bounded(self):
        newest = snapshot(90, source="newest")
        old = [snapshot(20, source=f"old-{index}", minutes=-20) for index in range(600)]
        result = provider(UsageStore(), [newest, *old])
        self.assertEqual(result["sourceId"], "newest")
        self.assertEqual(result["windows"][0]["usedPercent"], 90)

    def test_freshness_boundary_is_five_minutes_and_changes_are_not_native_measurements(self):
        sample = snapshot(0, minutes=-5)
        store = UsageStore()
        self.assertEqual(provider(store, [sample])["status"], "available")
        self.assertEqual(provider(store, [sample], NOW + timedelta(microseconds=1))["status"], "stale")
        self.assertEqual(provider(UsageStore(), [sample])["windows"][0]["freshnessBasis"], "unknown")

    def test_timestamp_ordering_handles_fractional_seconds(self):
        earlier = snapshot(20, source="earlier")
        later = snapshot(40, source="later")
        later["capturedAt"] = "2026-10-18T12:00:00.000001Z"
        result = provider(UsageStore(), [later, earlier], NOW + timedelta(seconds=1))
        self.assertEqual(result["sourceId"], "later")
        self.assertEqual(result["windows"][0]["usedPercent"], 40)


    def test_replaying_history_beyond_capacity_preserves_forecast(self):
        history = [snapshot(40, minutes=-10), snapshot(60)]
        store = UsageStore()
        for _ in range(600):
            current = provider(store, history)
        self.assertEqual(current["windows"][0]["forecast"],
                         {"status": "predicted", "exhaustsAt": "2026-10-18T12:20:00Z",
                          "basis": "recorded_changes", "sampleSeconds": 600.0})

    def test_future_input_cannot_evict_earlier_valid_observations(self):
        future = [snapshot(40, minutes=20, source=f"future-{index}") for index in range(600)]
        result = provider(UsageStore(), [snapshot(20), *future])
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["windows"][0]["usedPercent"], 20)


if __name__ == "__main__":
    unittest.main()
