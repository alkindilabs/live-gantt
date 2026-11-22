"""Behavioral contracts for command timing summaries."""

from datetime import datetime, timedelta, timezone
import unittest

from live_gantt.command_timings import CommandTimings, summarize_command_timings


NOW = datetime(2026, 11, 22, tzinfo=timezone.utc)


class CommandTimingTests(unittest.TestCase):
    def test_groups_fixed_labels_across_transcripts_without_exposing_arguments(self):
        caller = CommandTimings()
        caller.start("git", "cd -- '/private/worktree' && git -C /private/checkout status > /private/log 2>&1", NOW)
        caller.finish("git", NOW + timedelta(seconds=2))
        caller.start("tests", "MODE=local python3.13 -m unittest tests.private_suite", NOW)
        caller.finish("tests", NOW + timedelta(milliseconds=1500))
        child = CommandTimings()
        child.start("git", "env TIMEOUT=10 /usr/bin/git status --porcelain", NOW)
        child.finish("git", NOW + timedelta(seconds=1))
        child.start("pr", "gh pr checks 42 --repo secret/checkout", NOW)
        child.finish("pr", NOW + timedelta(milliseconds=1500))

        self.assertEqual(summarize_command_timings([caller, child]), {
            "operations": [
                {"label": "git status", "calls": 2, "totalMs": 3000, "longestMs": 2000},
                {"label": "gh pr checks", "calls": 1, "totalMs": 1500, "longestMs": 1500},
                {"label": "python -m unittest", "calls": 1, "totalMs": 1500, "longestMs": 1500},
            ],
            "unmeasured": 0,
        })

    def test_keeps_opaque_background_and_invalid_calls_unmeasured_once(self):
        calls = CommandTimings()
        calls.finish("unknown", NOW)
        for call_id, command in enumerate((
            "forge test | tee private",
            "echo \u0022$(secret_command)\u0022",
            "git status && git diff",
            "./private/script",
            "git status\ngit diff",
            "bash -c 'git status'",
            "cargo test &",
        )):
            calls.start(call_id, command, NOW)
            calls.finish(call_id, NOW + timedelta(minutes=1))
        calls.start("unfinished", "cargo check", NOW)
        calls.start("background", "cargo test", NOW)
        calls.finish("background", NOW, background=True)
        calls.start("negative", "npm ci", NOW)
        calls.finish("negative", NOW - timedelta(milliseconds=1))
        calls.unmeasured("opaque")
        calls.unmeasured("opaque")

        self.assertEqual(summarize_command_timings([calls]), {"operations": [], "unmeasured": 11})

    def test_snapshot_tracks_active_calls_and_deduplicates_results(self):
        calls = CommandTimings()
        calls.start("call", "printf '%s' 'text; with shell punctuation'", NOW)
        calls.start("call", "git status", NOW + timedelta(seconds=1))
        calls.start("opaque", "forge test | tee /private/log", NOW)
        self.assertTrue(calls.awaiting("call"))
        self.assertFalse(calls.awaiting("opaque"))
        self.assertEqual(summarize_command_timings([calls]), {"operations": [], "unmeasured": 2})

        calls.finish("call", NOW + timedelta(milliseconds=1001, microseconds=99))
        calls.finish("call", NOW + timedelta(seconds=12))
        self.assertFalse(calls.awaiting("call"))
        self.assertEqual(summarize_command_timings([calls]), {
            "operations": [{"label": "printf", "calls": 1, "totalMs": 1001, "longestMs": 1001}],
            "unmeasured": 1,
        })

    def test_labels_reviewed_scripts_by_basename(self):
        calls = CommandTimings()
        calls.start("install", "./scripts/install.sh --prefix /private/directory", NOW)
        calls.finish("install", NOW + timedelta(seconds=1))

        self.assertEqual(summarize_command_timings([calls]), {
            "operations": [{"label": "install.sh", "calls": 1, "totalMs": 1000, "longestMs": 1000}],
            "unmeasured": 0,
        })
