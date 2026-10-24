from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest

from live_gantt.monitoring import summarize


NOW = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)


def task(status="in_progress", minutes=1, **changes):
    return {"id": "1", "subject": "Build", "status": status,
            "updatedAt": (NOW - timedelta(minutes=minutes)).isoformat(),
            "quotaDelays": [], "runs": [], "agents": [], **changes}


class MonitoringTests(unittest.TestCase):
    def setUp(self):
        self.model = {"provider": "codex", "tasks": [task()], "agents": [], "workflows": [],
                      "sync": {"status": "current"}, "liveness": {"status": "alive"},
                      "freshness": {"stale": False, "findings": []}}
        self.runtime = {"phase": "working", "lastActivityAt": NOW.isoformat(), "detail": None}
        self.native = {"phase": "unknown", "lastActivityAt": None, "detail": None}

    def read(self):
        return summarize(self.model, NOW, runtime=self.runtime, native=self.native)

    def test_verified_recent_work_is_working(self):
        result = self.read()
        self.assertEqual(result["state"], "working")
        self.assertEqual(result["lastActivityAt"], NOW.isoformat())

    def test_quiet_work_never_becomes_healthy_from_fresh_file_or_quota_reads(self):
        self.runtime["lastActivityAt"] = (NOW - timedelta(minutes=5)).isoformat()
        self.model["transcriptUpdatedAt"] = NOW.isoformat()
        self.model["observedAt"] = NOW.isoformat()
        result = self.read()
        self.assertEqual(result["state"], "unverified")
        self.assertIn("execution", " ".join(result["issues"]).lower())

    def test_current_native_wait_is_actionable_even_after_long_silence(self):
        self.native = {"phase": "waiting", "lastActivityAt": (NOW - timedelta(hours=1)).isoformat(),
                       "detail": "The harness is waiting for permission."}
        result = self.read()
        self.assertEqual(result["state"], "needs_you")
        self.assertIn("permission", result["summary"].lower())

    def test_live_codex_approval_is_immediate_and_clears_on_resolution(self):
        self.native.update(phase="waiting", detail="Codex needs approval to run a command.")
        result = self.read()
        self.assertEqual(result["state"], "needs_you")
        self.assertIn("approval", result["summary"])
        self.native.update(phase="unknown", detail=None)
        self.assertEqual(self.read()["state"], "working")

    def test_lost_approval_stream_does_not_leave_recent_work_healthy(self):
        self.native.update(phase="unknown", detail="Live Codex approval monitoring is unavailable.")
        result = self.read()
        self.assertEqual(result["state"], "unverified")
        self.assertIn("approval", result["summary"])

    def test_current_native_approval_takes_priority_over_recorded_failure(self):
        self.runtime.update(phase="failed", detail="The previous recorded turn failed.")
        self.native.update(phase="waiting", detail="Codex is waiting for permission approval.")
        result = self.read()
        self.assertEqual(result["state"], "needs_you")
        self.assertIn("The previous recorded turn failed.", result["issues"])

    def test_read_failure_does_not_hide_recorded_failure(self):
        self.model["sync"]["status"] = "retrying"
        self.runtime.update(phase="failed", detail="The turn ended with an error.")
        result = self.read()
        self.assertEqual(result["state"], "unverified")
        self.assertIn("error", " ".join(result["issues"]))
        self.model["sync"]["status"] = "current"
        self.assertEqual(self.read()["state"], "attention")

    def test_failure_and_interruption_cannot_be_plan_complete(self):
        self.model["tasks"] = [task("completed")]
        for phase in ("failed", "interrupted"):
            with self.subTest(phase=phase):
                self.runtime.update(phase=phase, detail="The turn did not finish successfully.")
                self.assertEqual(self.read()["state"], "attention")

    def test_idle_does_not_complete_unfinished_plan(self):
        self.native["phase"] = "idle"
        self.assertEqual(self.read()["state"], "attention")
        self.model["tasks"] = [task("pending")]
        self.assertEqual(self.read()["state"], "idle")
        self.model["tasks"] = [task("completed")]
        self.assertEqual(self.read()["state"], "plan_complete")
        self.model["tasks"] = []
        self.assertEqual(self.read()["state"], "idle")

    def test_unresolved_input_without_native_confirmation_is_attention(self):
        self.runtime.update(phase="unknown", detail="Recorded input request has no recorded response.")
        self.assertEqual(self.read()["state"], "attention")

    def test_confirmed_quota_wait_but_not_forecast_becomes_waiting(self):
        self.native["phase"] = "idle"
        delay = {"start": (NOW - timedelta(minutes=1)).isoformat(),
                 "end": (NOW + timedelta(hours=1)).isoformat(), "provider": "codex", "reason": "reached"}
        self.model["tasks"] = [task("pending", quotaDelays=[delay])]
        self.assertEqual(self.read()["state"], "waiting_for_quota")
        delay["reason"] = "predicted"
        self.assertEqual(self.read()["state"], "idle")

    def test_old_plan_is_flagged_even_when_transcript_is_quiet(self):
        self.model["tasks"] = [task(minutes=15)]
        self.model["freshness"] = {"stale": False, "findings": []}
        result = self.read()
        self.assertEqual(result["state"], "attention")
        self.assertIn("15", " ".join(result["issues"]))

    def test_unknown_owner_and_exited_unfinished_work_never_show_working(self):
        self.model["liveness"]["status"] = "unknown"
        self.assertEqual(self.read()["state"], "unverified")
        self.model["liveness"]["status"] = "exited"
        self.assertEqual(self.read()["state"], "attention")

    def test_active_external_work_prevents_plan_complete_or_provider_wait(self):
        self.native["phase"] = "idle"
        run = {"label": "Build", "ended": None, "pidAlive": True, "lastActivityAt": NOW.isoformat()}
        self.model["tasks"] = [task("completed", runs=[run])]
        self.assertEqual(self.read()["state"], "working")
        run["lastActivityAt"] = (NOW - timedelta(minutes=5)).isoformat()
        self.assertEqual(self.read()["state"], "unverified")

    def test_a_recent_external_log_outweighs_the_launching_process_exit(self):
        self.native["phase"] = "idle"
        run = {"label": "Build", "ended": None, "pidAlive": False,
               "log": "build.log", "logUpdatedAt": NOW.isoformat(), "lastActivityAt": NOW.isoformat()}
        self.model["tasks"] = [task("completed", runs=[run])]
        self.assertEqual(self.read()["state"], "working")

    def test_codex_idle_parent_keeps_recent_background_execution_visible(self):
        self.runtime["phase"] = "idle"
        self.model["tasks"] = [task("completed", runs=[{
            "label": "Build", "ended": None, "pidAlive": True, "lastActivityAt": NOW.isoformat()}])]
        self.assertEqual(self.read()["state"], "working")

    def test_failed_child_or_workflow_prevents_green_plan_completion(self):
        self.runtime["phase"] = "idle"
        self.model["tasks"] = [task("completed")]
        for field, value in (("agents", {"status": "failed", "agentType": "subagent"}),
                             ("workflows", {"status": "failed", "agents": []})):
            with self.subTest(field=field):
                self.model[field] = [value]
                self.assertEqual(self.read()["state"], "attention")
                self.model[field] = []

    def test_exited_harness_does_not_hide_failed_background_work(self):
        self.runtime["phase"] = "idle"
        self.model["liveness"]["status"] = "exited"
        self.model["tasks"] = [task("completed")]
        self.model["workflows"] = [{"status": "failed", "agents": []}]
        self.assertEqual(self.read()["state"], "attention")

    def test_old_native_phase_cannot_override_newer_conflicting_execution(self):
        self.model["tasks"] = [task("completed")]
        self.native["lastActivityAt"] = (NOW - timedelta(hours=1)).isoformat()
        for native_phase, recorded_phase in (("idle", "working"), ("working", "idle")):
            with self.subTest(native_phase=native_phase):
                self.native["phase"] = native_phase
                self.runtime["phase"] = recorded_phase
                result = self.read()
                self.assertEqual(result["state"], "attention")
                self.assertIn("conflict", " ".join(result["issues"]).lower())

    def test_claude_observation_predating_transcript_transition_is_not_a_conflict(self):
        self.model.update(provider="claude", tasks=[])
        self.model["liveness"].update(evidence="native_registry_process")
        self.native["lastActivityAt"] = (NOW - timedelta(seconds=5)).isoformat()
        for native_phase, recorded_phase in (("working", "idle"), ("idle", "working")):
            with self.subTest(native_phase=native_phase):
                self.native["phase"] = native_phase
                self.runtime["phase"] = recorded_phase
                self.model["liveness"]["checkedAt"] = (NOW - timedelta(seconds=1)).isoformat()
                result = self.read()
                self.assertEqual(result["state"], recorded_phase)
                self.assertEqual(result["issues"], [])
                self.model["liveness"]["checkedAt"] = NOW.isoformat()
                self.assertEqual(self.read()["state"], "attention")

    def test_quota_does_not_hide_recently_recorded_execution(self):
        self.model["tasks"][0]["quotaDelays"] = [{"start": NOW.isoformat(),
            "end": (NOW + timedelta(hours=1)).isoformat(), "provider": "codex", "reason": "reached"}]
        self.assertNotEqual(self.read()["state"], "waiting_for_quota")

    def test_declared_run_start_without_process_or_log_evidence_is_unverified(self):
        self.native["phase"] = "idle"
        self.model["tasks"] = [task("completed", runs=[{
            "label": "Build", "ended": None, "pidAlive": None, "logUpdatedAt": None,
            "lastActivityAt": NOW.isoformat()}])]
        self.assertEqual(self.read()["state"], "unverified")

    def test_input_not_mutated_and_untrusted_detail_is_not_used_as_working_claim(self):
        snapshot = deepcopy((self.model, self.runtime, self.native))
        self.runtime["detail"] = "Working forever"
        self.runtime["phase"] = "unknown"
        result = self.read()
        self.assertEqual(result["state"], "attention")
        self.assertEqual(self.model, snapshot[0])
        self.assertEqual(self.native, snapshot[2])
