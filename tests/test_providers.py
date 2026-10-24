from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from live_gantt.providers import ProviderStore
from live_gantt.capture_usage import capture


class ProviderTests(unittest.TestCase):
    def test_failed_quota_scan_ages_history_while_other_provider_refreshes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
            later = now + timedelta(minutes=6)
            transcript = root / "codex" / "sessions" / "example.jsonl"
            transcript.parent.mkdir(parents=True)
            transcript.write_text("\n".join(json.dumps(record) for record in (
                {"type": "session_meta", "payload": {"id": "native"}},
                {"timestamp": now.isoformat(), "type": "event_msg", "payload": {
                    "type": "token_count", "rate_limits": {"limit_id": "codex", "primary": {
                        "used_percent": 100, "window_minutes": 300,
                        "resets_at": (now + timedelta(hours=2)).timestamp()}}}},
            )) + "\n")
            store = ProviderStore(root / "claude", codex_root=root / "codex", usage_root=root / "usage")
            self.addCleanup(store.close)
            initial = store.usage(now)["providers"][1]
            self.assertEqual(initial["status"], "available")
            self.assertTrue(capture({"session_id": "claude-session", "rate_limits": {
                "five_hour": {"used_percentage": 20,
                              "resets_at": (later + timedelta(hours=1)).timestamp()}}},
                later, root / "usage"))
            transcript.parent.chmod(0)
            try:
                claude, codex = store.usage(later)["providers"]
                self.assertEqual((codex["status"], codex["windows"][0]["stale"]), ("stale", True))
                self.assertEqual(codex["windows"][0]["usedPercent"], 100)
                self.assertEqual(codex["windows"][0]["forecast"]["status"], "stale")
                self.assertIn("could not be refreshed", codex["message"])
                self.assertEqual((claude["status"], claude["windows"][0]["usedPercent"]), ("available", 20))
            finally:
                transcript.parent.chmod(0o700)
            recovered = store.usage(later)["providers"][1]
            self.assertEqual(recovered["message"], "Recorded quota is stale.")

    def test_discovery_orders_fractional_timestamps_across_providers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = root / "claude" / "tasks" / "legacy" / "1.json"
            task.parent.mkdir(parents=True)
            task.write_text('{"id":"1","subject":"Example","status":"pending"}')
            transcript = root / "codex" / "sessions" / "example.jsonl"
            transcript.parent.mkdir(parents=True)
            transcript.write_text(json.dumps({"type": "session_meta", "payload": {"id": "native"}}) + "\n")
            epoch = datetime(2026, 10, 18, tzinfo=timezone.utc).timestamp()
            os.utime(transcript, (epoch, epoch))
            os.utime(task, (epoch + 0.5, epoch + 0.5))
            store = ProviderStore(root / "claude", codex_root=root / "codex")
            self.assertEqual([item["id"] for item in store.lists()], ["legacy", "codex-native"])

    def test_failed_discovery_preserves_last_listing_while_other_provider_updates(self):
        for failing_provider in ("claude", "codex"):
            with self.subTest(provider=failing_provider), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                claude_task = root / "claude" / "tasks" / "legacy" / "1.json"
                claude_task.parent.mkdir(parents=True)
                claude_task.write_text('{"id":"1","subject":"Claude","status":"pending"}')
                codex_session = root / "codex" / "sessions" / "example.jsonl"
                codex_session.parent.mkdir(parents=True)
                codex_session.write_text(json.dumps({"type": "session_meta", "payload": {"id": "native"}}) + "\n")
                store = ProviderStore(root / "claude", codex_root=root / "codex")
                initial = store.lists()
                self.assertEqual({item["id"] for item in initial}, {"legacy", "codex-native"})
                initial[0]["title"] = "caller mutation"
                if failing_provider == "claude":
                    claude_task.unlink()
                    claude_task.parent.rmdir()
                    added = codex_session.with_name("second.jsonl")
                    added.write_text(json.dumps({"type": "session_meta", "payload": {"id": "second"}}) + "\n")
                else:
                    codex_session.unlink()
                    added = claude_task.parent.parent / "second" / "1.json"
                    added.parent.mkdir()
                    added.write_text('{"id":"1","subject":"Second","status":"pending"}')
                original_iterdir = Path.iterdir
                original_scandir = os.scandir

                def external_iterdir(path):
                    if failing_provider == "claude" and path == root / "claude" / "tasks":
                        raise PermissionError("private filesystem diagnostic")
                    return original_iterdir(path)

                def external_scandir(path):
                    if failing_provider == "codex" and Path(path) == root / "codex" / "sessions":
                        raise PermissionError("private filesystem diagnostic")
                    return original_scandir(path)

                with patch.object(Path, "iterdir", external_iterdir), patch("os.scandir", external_scandir):
                    retained = store.lists()
                    expected = {"legacy", "codex-native", "codex-second" if failing_provider == "claude" else "second"}
                    self.assertEqual({item["id"] for item in retained}, expected)
                    self.assertNotIn("caller mutation", [item["title"] for item in retained])
                    self.assertEqual(set(store.discovery_errors), {failing_provider})
                    self.assertNotIn("private", str(store.discovery_errors))
                    errors = store.discovery_errors
                    errors.clear()
                    self.assertEqual(set(store.discovery_errors), {failing_provider})
                recovered = store.lists()
                removed = "legacy" if failing_provider == "claude" else "codex-native"
                self.assertNotIn(removed, {item["id"] for item in recovered})
                self.assertEqual(store.discovery_errors, {})
                self.assertIsNone(store.state(removed, datetime.now(timezone.utc)))

    def test_unknown_codex_id_never_falls_back_to_claude(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = root / "claude" / "tasks" / "codex-missing" / "1.json"
            task.parent.mkdir(parents=True)
            task.write_text('{"id":"1","subject":"Claude collision","status":"pending"}')
            for codex_root in (None, root / "codex"):
                with self.subTest(codex_root=codex_root):
                    store = ProviderStore(root / "claude", codex_root=codex_root)
                    self.assertIsNone(store.state("codex-missing", datetime.now(timezone.utc)))
