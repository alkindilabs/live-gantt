"""Opt-in quota capture must preserve its pipeline and never persist raw input."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from live_gantt.capture_usage import capture
from live_gantt.usage import UsageStore


NOW = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)


def document(percent=20, session="claude-session"):
    return {"session_id": session, "rate_limits": {
        "five_hour": {"used_percentage": percent, "resets_at": (NOW + timedelta(hours=1)).timestamp()},
        "spend_limit": {"used_percentage": 40, "used_usd": 300}},
        "prompt": "PRIVATE PROMPT", "credentials": "PRIVATE TOKEN"}


def claude(root, now=NOW):
    return UsageStore(root).state([], now)["providers"][0]


class CaptureTests(unittest.TestCase):
    def test_capture_writes_only_normalized_quota_and_supports_zero(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.assertTrue(capture(document(0), NOW, root))
            result = claude(root)
            self.assertEqual(result["windows"][0]["usedPercent"], 0)
            self.assertEqual(result["windows"][0]["windowMinutes"], 300)
            self.assertEqual(result["windows"][0]["resetsAt"], "2026-10-18T13:00:00Z")
            for path in root.iterdir():
                text = path.read_text()
                self.assertNotIn("PRIVATE", text)
                self.assertNotIn("spend_limit", text)
                self.assertNotIn("used_usd", text)

    def test_repeated_capture_never_reages_quota_and_absence_invalidates_it(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            capture(document(), NOW, root)
            capture(document(), NOW + timedelta(minutes=6), root)
            self.assertEqual(claude(root, NOW + timedelta(minutes=6))["status"], "stale")
            missing = document()
            missing["rate_limits"] = {}
            self.assertTrue(capture(missing, NOW + timedelta(minutes=7), root))
            self.assertEqual(claude(root, NOW + timedelta(minutes=7))["status"], "unavailable")

    def test_malformed_files_and_invalid_percentages_are_unavailable(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "quota-history.json").write_text('{"unfinished"')
            self.assertEqual(claude(root)["status"], "unavailable")
            for value in (True, float("inf"), -1, 101, "50"):
                self.assertTrue(capture(document(value), NOW, root))
                self.assertEqual(claude(root)["status"], "unavailable")
            self.assertFalse(capture([], NOW, root))
            self.assertFalse(capture({"session_id": "../other"}, NOW, root))

    def test_capture_is_atomic_across_concurrent_processes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            def run(index):
                payload = json.dumps(document(10 + index, session=f"session-{index}"))
                return subprocess.run([sys.executable, "-B", "-m", "live_gantt.capture_usage",
                                       "--directory", str(root)], input=payload.encode(),
                                      capture_output=True, timeout=10)
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(run, range(8)))
            self.assertTrue(all(result.returncode == 0 for result in results))
            history = json.loads((root / "quota-history.json").read_text())
            self.assertEqual(len({row["sourceId"] for row in history}), 8)

    def test_cli_preserves_original_bytes_on_success_malformed_and_write_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            blocked = root / "not-a-directory"
            blocked.write_text("retain")
            inputs = [(json.dumps(document()).encode() + b"\n", root / "capture"),
                      (b"\xffnot json\n", root / "capture"),
                      (json.dumps(document()).encode(), blocked)]
            for payload, destination in inputs:
                result = subprocess.run([sys.executable, "-B", "-m", "live_gantt.capture_usage",
                                         "--directory", str(destination)], input=payload,
                                        capture_output=True, timeout=10)
                self.assertEqual(result.stdout, payload)
                self.assertEqual(result.returncode, 0)
            self.assertEqual(blocked.read_text(), "retain")

    def test_writer_preserves_another_writer_with_a_later_capture_timestamp(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.assertTrue(capture(document(session="later"), NOW + timedelta(microseconds=1), root))
            self.assertTrue(capture(document(session="earlier"), NOW, root))
            history = json.loads((root / "quota-history.json").read_text())
            self.assertEqual({row["sourceId"] for row in history}, {"earlier", "later"})

    def test_cached_capture_beyond_history_capacity_preserves_real_change_samples(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.assertTrue(capture(document(20), NOW - timedelta(minutes=10), root))
            self.assertTrue(capture(document(40), NOW, root))
            for milliseconds in range(1, 514):
                self.assertTrue(capture(document(40), NOW + timedelta(milliseconds=milliseconds), root))
            window = claude(root, NOW + timedelta(seconds=1))["windows"][0]
            self.assertEqual(window["forecast"],
                             {"status": "predicted", "exhaustsAt": "2026-10-18T12:30:00Z",
                              "basis": "recorded_changes", "sampleSeconds": 600.0})
            self.assertEqual(window["observedAt"], "2026-10-18T12:00:00Z")
            self.assertEqual(window["capturedAt"], "2026-10-18T12:00:00.513000Z")
