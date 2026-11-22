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
            "leaderDetail": {
                "label": "git status",
                "slowestCalls": [
                    {"elapsedMs": 2000, "outcome": "unknown", "reviewProvider": None},
                    {"elapsedMs": 1000, "outcome": "unknown", "reviewProvider": None},
                ],
                "outcomes": [{"outcome": "unknown", "calls": 2, "totalMs": 3000}],
                "reviews": [],
                "processTiming": None,
            },
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

        self.assertEqual(summarize_command_timings([calls]), {
            "operations": [], "unmeasured": 11, "leaderDetail": None,
        })

    def test_snapshot_tracks_active_calls_and_deduplicates_results(self):
        calls = CommandTimings()
        calls.start("call", "printf '%s' 'text; with shell punctuation'", NOW)
        calls.start("call", "git status", NOW + timedelta(seconds=1))
        calls.start("opaque", "forge test | tee /private/log", NOW)
        self.assertTrue(calls.awaiting("call"))
        self.assertFalse(calls.awaiting("opaque"))
        self.assertEqual(summarize_command_timings([calls]), {
            "operations": [], "unmeasured": 2, "leaderDetail": None,
        })

        calls.finish("call", NOW + timedelta(milliseconds=1001, microseconds=99))
        calls.finish("call", NOW + timedelta(seconds=12))
        self.assertFalse(calls.awaiting("call"))
        summary = summarize_command_timings([calls])
        self.assertEqual(summary["operations"], [{"label": "printf", "calls": 1, "totalMs": 1001, "longestMs": 1001}])
        self.assertEqual(summary["unmeasured"], 1)

    def test_labels_reviewed_scripts_by_basename(self):
        calls = CommandTimings()
        calls.start("install", "./scripts/install.sh --prefix /private/directory", NOW)
        calls.finish("install", NOW + timedelta(seconds=1))

        summary = summarize_command_timings([calls])
        self.assertEqual(summary["operations"], [{"label": "install.sh", "calls": 1, "totalMs": 1000, "longestMs": 1000}])
        self.assertEqual(summary["unmeasured"], 0)

    def test_slowest_five_belong_to_the_cumulative_leader_and_merge_deterministically(self):
        first = CommandTimings()
        first.start("single", "git status", NOW)
        first.finish("single", NOW + timedelta(seconds=20))
        second = CommandTimings()
        for transcript, calls in (
            (first, ((9, "completed"), (8, "failed"), (7, "failed"), (6, "failed"), (5, "failed"), (4, "failed"))),
            (second, ((10, "completed"), (8, "completed"), (7, "denied"))),
        ):
            for call_id, (seconds, outcome) in enumerate(calls):
                transcript.start(call_id, "git commit -m 'private message'", NOW)
                transcript.finish(call_id, NOW + timedelta(seconds=seconds), outcome=outcome)

        summary = summarize_command_timings([first, second])
        self.assertEqual(summary, summarize_command_timings([second, first]))
        self.assertEqual(summary["operations"], [
            {"label": "git commit", "calls": 9, "totalMs": 64000, "longestMs": 10000},
            {"label": "git status", "calls": 1, "totalMs": 20000, "longestMs": 20000},
        ])
        self.assertEqual(summary["leaderDetail"], {
            "label": "git commit",
            "slowestCalls": [
                {"elapsedMs": 10000, "outcome": "completed", "reviewProvider": None},
                {"elapsedMs": 9000, "outcome": "completed", "reviewProvider": None},
                {"elapsedMs": 8000, "outcome": "completed", "reviewProvider": None},
                {"elapsedMs": 8000, "outcome": "failed", "reviewProvider": None},
                {"elapsedMs": 7000, "outcome": "denied", "reviewProvider": None},
            ],
            "outcomes": [
                {"outcome": "completed", "calls": 3, "totalMs": 27000},
                {"outcome": "failed", "calls": 5, "totalMs": 30000},
                {"outcome": "denied", "calls": 1, "totalMs": 7000},
            ],
            "reviews": [],
            "processTiming": None,
        })

    def test_review_breakdown_reports_only_exact_same_call_git_commit_evidence(self):
        caller = CommandTimings()
        child = CommandTimings()
        claude_start = "principles-gate: reviewing staged changes with claude"
        claude_rejection = "principles-gate: claude review did not pass; principles could not be verified"
        codex_start = "principles-gate: reviewing staged changes with codex"
        codex_rejection = "principles-gate: codex review did not pass; principles could not be verified"
        for transcript, duration, outcome, output in (
            (caller, 5000, "failed", (f"secret path\n{claude_start}\n{claude_rejection}\n",)),
            (caller, 4000, "completed", (codex_start,)),
            (caller, 3000, "failed", (codex_start, claude_rejection)),
            (child, 2000, "completed", (codex_start, codex_rejection)),
            (child, 1000, "failed", (claude_start, codex_start, claude_rejection)),
            (child, 900, "failed", (claude_rejection,)),
            (child, 800, "failed", (f"prefix {claude_start}", f"{claude_rejection} suffix")),
            (child, 700, None, (claude_start, claude_rejection)),
        ):
            transcript.start(duration, "git commit -m 'secret message'", NOW)
            transcript.finish(duration, NOW + timedelta(milliseconds=duration), outcome=outcome, output=output)

        detail = summarize_command_timings([caller, child])["leaderDetail"]
        self.assertEqual(detail, {
            "label": "git commit",
            "slowestCalls": [
                {"elapsedMs": 5000, "outcome": "reviewRejected", "reviewProvider": "claude"},
                {"elapsedMs": 4000, "outcome": "completed", "reviewProvider": "codex"},
                {"elapsedMs": 3000, "outcome": "failed", "reviewProvider": None},
                {"elapsedMs": 2000, "outcome": "completed", "reviewProvider": None},
                {"elapsedMs": 1000, "outcome": "failed", "reviewProvider": None},
            ],
            "outcomes": [
                {"outcome": "completed", "calls": 2, "totalMs": 6000},
                {"outcome": "failed", "calls": 4, "totalMs": 5700},
                {"outcome": "reviewRejected", "calls": 1, "totalMs": 5000},
                {"outcome": "unknown", "calls": 1, "totalMs": 700},
            ],
            "reviews": [
                {"provider": "claude", "calls": 2, "totalMs": 5700, "rejectedCalls": 1, "rejectedMs": 5000},
                {"provider": "codex", "calls": 1, "totalMs": 4000, "rejectedCalls": 0, "rejectedMs": 0},
                {"provider": None, "calls": 5, "totalMs": 7700, "rejectedCalls": 0, "rejectedMs": 0},
            ],
            "processTiming": None,
        })

    def test_process_timing_uses_only_valid_measurements_and_non_commit_output_is_opaque(self):
        first = CommandTimings()
        second = CommandTimings()
        output = (
            "principles-gate: reviewing staged changes with codex",
            "principles-gate: codex review did not pass; principles could not be verified",
        )
        for transcript, elapsed_ms, process_ms in (
            (first, 3000, 1000), (first, 2000, 2000), (first, 1100, 0),
            (first, 600, 601), (first, 500, -1), (second, 400, None),
            (second, 300, True), (second, 200, 1.0), (second, 100, 10),
        ):
            transcript.start(elapsed_ms, "git status", NOW)
            transcript.finish(elapsed_ms, NOW + timedelta(milliseconds=elapsed_ms), outcome="failed",
                              output=output, process_ms=process_ms)

        detail = summarize_command_timings([first, second])["leaderDetail"]
        self.assertEqual(detail["processTiming"], {"calls": 4, "observedMs": 6200, "processMs": 3010})
        self.assertEqual(detail["reviews"], [])
        self.assertEqual(detail["outcomes"], [{"outcome": "failed", "calls": 9, "totalMs": 8200}])
