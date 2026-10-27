from datetime import datetime, timedelta, timezone
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from live_gantt.freshness import STALE_PROGRESS_MINUTES
from live_gantt.server import make_server


POLL_SECONDS = 0.04
HEARTBEAT_SECONDS = 0.12
STALENESS_MARGIN_SECONDS = 1.5
WAIT_SECONDS = 1.5


def wait_until(predicate):
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("Background work did not reach the expected state")


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tasks = self.root / "tasks" / "session-one"
        self.tasks.mkdir(parents=True)
        self.task = self.tasks / "1.json"
        self.write_task("Initial task")
        self.server = make_server(
            self.root, port=0,
            codex_root=self.root / "codex", usage_root=self.root / "usage",
            poll_seconds=POLL_SECONDS, heartbeat_seconds=HEARTBEAT_SECONDS,
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": POLL_SECONDS},
        )
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.port = self.server.server_address[1]

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def write_task(self, subject, status="pending", **metadata):
        self.task.write_text(json.dumps({
            "id": "1", "subject": subject, "description": "Local fixture",
            "status": status, "blockedBy": [], "blocks": [],
            "metadata": {"estimate": {"low": 1, "likely": 2, "high": 3}, **metadata},
        }))

    def test_loopback_startup_never_resolves_a_hostname(self):
        with patch("socket.getfqdn", side_effect=AssertionError("Loopback startup must not use DNS")):
            server = make_server(self.root, port=0,
                                 codex_root=self.root / "codex", usage_root=self.root / "usage")
        self.addCleanup(server.server_close)
        self.assertEqual(server.server_name, "127.0.0.1")
        self.assertEqual(server.server_port, server.server_address[1])

    def open_stream(self, path="/events?list=session-one"):
        connection = self.connection()
        connection.request("GET", path)
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "text/event-stream")

        def next_event():
            kind, data = None, None
            while True:
                line = response.readline().decode().strip()
                if line.startswith("event: "):
                    kind = line.removeprefix("event: ")
                elif line.startswith("data: "):
                    data = json.loads(line.removeprefix("data: "))
                elif not line and kind:
                    return kind, data

        self.addCleanup(response.close)
        next_event.close = lambda: (response.close(), connection.close())
        return next_event

    def test_mixed_provider_identity_and_quota_use_the_public_routes(self):
        transcript = self.root / "codex" / "sessions" / "2026" / "10" / "18" / "rollout-example.jsonl"
        transcript.parent.mkdir(parents=True)
        now = datetime.now(timezone.utc)
        records = [
            {"timestamp": now.isoformat(), "type": "session_meta",
             "payload": {"id": "codex-example", "cwd": "/example/codex"}},
            {"timestamp": now.isoformat(), "type": "event_msg", "payload": {"type": "task_started"}},
            {"timestamp": now.isoformat(), "type": "event_msg", "payload": {
                "type": "token_count", "info": {"total_token_usage": {
                    "input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 10,
                    "reasoning_output_tokens": 5, "total_tokens": 110}},
                "rate_limits": {"limit_id": "codex", "primary": {
                    "used_percent": 25, "window_minutes": 10080,
                    "resets_at": int((now + timedelta(days=1)).timestamp())}}}},
        ]
        transcript.write_text("".join(json.dumps(record) + "\n" for record in records))
        connection = self.connection()
        connection.request("GET", "/api/lists")
        lists = json.load(connection.getresponse())["lists"]
        self.assertEqual({(item["id"], item["provider"]) for item in lists},
                         {("session-one", "claude"), ("codex-codex-example", "codex")})
        connection.request("GET", "/api/state?list=codex-codex-example")
        state = json.load(connection.getresponse())
        self.assertEqual(state["sourceId"], "codex-example")
        self.assertEqual(state["tokenUsage"]["totalTokens"], 110)
        self.assertTrue(state["agents"])
        connection.request("GET", "/api/usage")
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        providers = {item["provider"]: item for item in json.load(response)["providers"]}
        self.assertEqual(providers["codex"]["windows"][0]["usedPercent"], 25)
        self.assertEqual(providers["codex"]["windows"][0]["windowMinutes"], 10080)
        self.assertEqual(providers["claude"]["status"], "unavailable")

    def test_usage_only_stream_has_no_fake_session_and_stops_polling_on_disconnect(self):
        next_event = self.open_stream("/events/usage")
        events = {}
        while "usage" not in events:
            kind, data = next_event()
            events[kind] = data
        self.assertEqual({item["provider"] for item in events["usage"]["providers"]}, {"claude", "codex"})
        self.assertEqual(self.server.monitor.readers, {})
        self.assertEqual(self.server.monitor.usage_readers, 1)
        self.assertIsNotNone(self.server.monitor.worker)
        next_event.close()
        deadline = time.monotonic() + 1
        with self.server.monitor.condition:
            while self.server.monitor.worker is not None and time.monotonic() < deadline:
                self.server.monitor.condition.wait(POLL_SECONDS)
        self.assertEqual(self.server.monitor.usage_readers, 0)
        self.assertIsNone(self.server.monitor.worker)

    def test_codex_plan_updates_reach_stream_and_survive_reconnect(self):
        transcript = self.root / "codex" / "sessions" / "native.jsonl"
        transcript.parent.mkdir(parents=True)

        def append(kind, payload):
            with transcript.open("a") as stream:
                stream.write(json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(),
                                         "type": kind, "payload": payload}) + "\n")

        append("session_meta", {"id": "native", "cwd": "/example"})
        append("event_msg", {"type": "plan_update", "plan": [{"step": "Build", "status": "in_progress"}]})
        next_event = self.open_stream("/events?list=codex-native")
        while True:
            kind, model = next_event()
            if kind == "state":
                break
        self.assertEqual([(task["subject"], task["status"]) for task in model["tasks"]],
                         [("Build", "in_progress")])
        self.assertFalse(model["freshness"]["stale"])
        next_event.close()
        append("event_msg", {"type": "plan_update", "plan": [{"step": "Build", "status": "completed"}]})
        next_event = self.open_stream("/events?list=codex-native")
        while True:
            kind, resumed = next_event()
            if kind == "state":
                break
        self.assertEqual(resumed["tasks"][0]["id"], model["tasks"][0]["id"])
        self.assertEqual(resumed["tasks"][0]["status"], "completed")
        self.assertEqual(resumed["sync"]["status"], "current")

    def connection(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        self.addCleanup(connection.close)
        return connection

    def test_foreign_and_duplicate_hosts_are_refused_on_every_method(self):
        for method in ("GET", "POST", "HEAD"):
            with self.subTest(method=method):
                connection = self.connection()
                connection.request(method, "/api/lists", headers={"Host": "evil.example"})
                self.assertEqual(connection.getresponse().status, 403)
        connection = self.connection()
        connection.putrequest("GET", "/api/lists")
        connection.putheader("Host", "evil.example")
        connection.endheaders()
        self.assertEqual(connection.getresponse().status, 403)

    def test_local_hosts_json_and_missing_list(self):
        for host in (f"127.0.0.1:{self.port}", f"localhost:{self.port}"):
            connection = self.connection()
            connection.request("GET", "/api/state?list=session-one", headers={"Host": host})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIsNone(response.getheader("Access-Control-Allow-Origin"))
            model = json.load(response)
            self.assertEqual(model["tasks"][0]["subject"], "Initial task")
            self.assertIsNotNone(model["tasks"][0]["projectedEnd"])
        for path, status in (("/api/state?list=missing", 404),
                             ("/api/state?list=../outside", 400),
                             ("/api/state", 400), ("/server.py", 404)):
            connection = self.connection()
            connection.request("GET", path)
            self.assertEqual(connection.getresponse().status, status)

    def test_state_reports_freshness_findings_and_task_change_times(self):
        connection = self.connection()
        connection.request("GET", "/api/state?list=session-one")
        model = json.load(connection.getresponse())
        freshness = model["freshness"]
        self.assertTrue(freshness["stale"])
        self.assertFalse(freshness["sessionActive"])
        self.assertEqual([(finding["rule"], finding["taskId"]) for finding in freshness["findings"]],
                         [("missing-schedule", "1")])
        self.assertEqual(freshness["findings"][0]["message"], "Task #1 lacks lane metadata.")
        self.assertIsInstance(freshness["sinceChangeSeconds"], int)
        self.assertIsNotNone(model["tasks"][0]["updatedAt"])

    def test_stream_reports_staleness_that_arrives_with_time_alone(self):
        self.write_task("Long task", status="in_progress", lane="Build", progress=10)
        almost_stale = (datetime.now(timezone.utc) - timedelta(minutes=STALE_PROGRESS_MINUTES)
                        + timedelta(seconds=STALENESS_MARGIN_SECONDS)).timestamp()
        os.utime(self.task, (almost_stale, almost_stale))
        transcript = self.root / "projects" / "-example" / "session-one.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text('{"type":"user","message":{"content":"Work"}}\n')
        next_event = self.open_stream()
        verdicts = []
        deadline = time.monotonic() + STALENESS_MARGIN_SECONDS * 4
        while time.monotonic() < deadline and True not in verdicts:
            kind, data = next_event()
            if kind == "state":
                verdicts.append(data["freshness"]["stale"])
        self.assertEqual(verdicts, [False, True])

    def test_snapshot_uses_one_observation_time_for_freshness_and_monitor(self):
        observed = datetime.now(timezone.utc)
        later = observed + timedelta(seconds=2)
        self.write_task("Boundary task", status="in_progress", lane="Build", progress=10)
        changed = (observed - timedelta(minutes=STALE_PROGRESS_MINUTES)
                   + timedelta(seconds=1)).timestamp()
        os.utime(self.task, (changed, changed))
        transcript = self.root / "projects" / "-example" / "session-one.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text('{"type":"user","message":{"content":"Work"}}\n')

        class ObservationClock:
            calls = 0

            @classmethod
            def now(cls, timezone):
                cls.calls += 1
                return observed if cls.calls == 1 else later

        with patch("live_gantt.server.datetime", ObservationClock):
            connection = self.connection()
            connection.request("GET", "/api/state?list=session-one")
            model = json.load(connection.getresponse())
        self.assertFalse(model["freshness"]["stale"])
        self.assertFalse(any("no recorded plan update" in issue for issue in model["monitor"]["issues"]))

    def test_stream_emits_changed_state_but_not_unchanged_state(self):
        next_event = self.open_stream()
        initial = []
        while not {"state", "usage"}.issubset({kind for kind, _ in initial}):
            initial.append(next_event())
        first = next(data for kind, data in initial if kind == "state")
        self.assertEqual(first["tasks"][0]["subject"], "Initial task")
        self.assertEqual(next_event()[0], "heartbeat")
        self.write_task("Changed task")
        for _ in range(10):
            kind, data = next_event()
            if kind == "state":
                self.assertEqual(data["tasks"][0]["subject"], "Changed task")
                break
        else:
            self.fail("No state event after the task file changed")

    def test_idle_start_does_not_read_sources(self):
        self.assertIsNone(self.server.monitor.worker)
        self.assertEqual(self.server.monitor.models, {})
        self.assertEqual(self.server.monitor.list_model, [])

    def test_opened_session_keeps_syncing_without_a_stream(self):
        connection = self.connection()
        connection.request("GET", "/api/state?list=session-one")
        initial = json.load(connection.getresponse())
        self.assertEqual(initial["sync"]["status"], "current")
        self.write_task("Updated while hidden", status="completed")
        wait_until(lambda: self.server.monitor.models["session-one"][1]["tasks"][0]["subject"]
                   == "Updated while hidden")
        self.assertEqual(self.server.monitor.readers, {})

    def test_opened_session_stops_syncing_once_its_background_window_ends(self):
        with patch("live_gantt.server.BACKGROUND_SESSION_SECONDS", 0.3):
            connection = self.connection()
            connection.request("GET", "/api/state?list=session-one")
            json.load(connection.getresponse())
            self.assertEqual(self.server.monitor.background_session, "session-one")
            wait_until(lambda: self.server.monitor.background_session is None)
            wait_until(lambda: self.server.monitor.worker is None)
            self.write_task("Written after the window", status="completed")
            time.sleep(POLL_SECONDS * 5)
            subject = self.server.monitor.models["session-one"][1]["tasks"][0]["subject"]
            self.assertNotEqual(subject, "Written after the window")

    def test_read_failure_keeps_snapshot_and_recovers_without_reconnecting(self):
        next_event = self.open_stream()
        while True:
            kind, initial = next_event()
            if kind == "state":
                break
        self.assertEqual(initial["sync"]["status"], "current")
        original_open = Path.open

        def interrupted_read(path, *args, **kwargs):
            if path == self.task:
                raise RuntimeError("private diagnostic must not reach browser")
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", interrupted_read):
            for _ in range(20):
                kind, failed = next_event()
                if kind == "state" and failed["sync"]["status"] == "retrying":
                    break
            else:
                self.fail("Failed source reads were not reported")
            self.assertEqual(failed["tasks"], initial["tasks"])
            self.assertEqual(failed["sync"]["checkedAt"], initial["sync"]["checkedAt"])
            self.assertNotIn("private diagnostic", json.dumps(failed))
            self.assertTrue(self.server.monitor.worker.is_alive())
        self.write_task("Recovered", status="completed")
        for _ in range(20):
            kind, recovered = next_event()
            if kind == "state" and recovered["sync"]["status"] == "current":
                self.assertEqual(recovered["tasks"][0]["subject"], "Recovered")
                break
        else:
            self.fail("Source reader did not recover")

    def test_heartbeats_confirm_source_checks_without_reemitting_unchanged_state(self):
        release_worker = threading.Event()
        start_thread = threading.Thread.start

        def delayed_start(thread):
            if thread.name == "live-gantt-sources":
                run = thread.run

                def after_release():
                    release_worker.wait(timeout=3)
                    run()

                thread.run = after_release
            return start_thread(thread)

        self.addCleanup(release_worker.set)
        with patch.object(threading.Thread, "start", delayed_start):
            next_event = self.open_stream()
        while True:
            kind, initial = next_event()
            if kind == "state":
                break
        worker_released = False
        for _ in range(10):
            kind, heartbeat = next_event()
            if kind == "heartbeat":
                self.assertEqual(heartbeat["sync"]["status"], "current")
                self.assertGreaterEqual(heartbeat["sync"]["checkedAt"], initial["sync"]["checkedAt"])
                if not worker_released:
                    self.assertEqual(heartbeat["sync"]["checkedAt"], initial["sync"]["checkedAt"])
                    release_worker.set()
                    worker_released = True
                elif heartbeat["sync"]["checkedAt"] > initial["sync"]["checkedAt"]:
                    break
                continue
            self.assertNotEqual(kind, "state")
        else:
            self.fail("No source-check heartbeat")

    def test_initial_read_failure_is_temporary_unavailability_not_missing_session(self):
        original_open = Path.open

        def interrupted_read(path, *args, **kwargs):
            if path == self.task:
                raise OSError("private file failure")
            return original_open(path, *args, **kwargs)

        connection = self.connection()
        with patch.object(Path, "open", interrupted_read):
            connection.request("GET", "/api/state?list=session-one")
            response = connection.getresponse()
            self.assertEqual(response.status, 503)
            self.assertNotIn(b"private file failure", response.read())
        connection.request("GET", "/api/state?list=session-one")
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(json.load(response)["sync"]["status"], "current")

    def test_harness_liveness_is_independent_of_plan_and_source_sync(self):
        registry = self.root / "sessions" / f"{os.getpid()}.json"
        registry.parent.mkdir()
        registry.write_text(json.dumps({"sessionId": "session-one", "pid": os.getpid(),
                                        "pidDomain": "darwin", "procStart": "native-start"}))
        from subprocess import CompletedProcess
        from live_gantt.liveness import LivenessStore
        identity = ["native-start"]
        clock = [0.0]

        def process_probe(command, **kwargs):
            if Path(command[0]).name == "ps":
                return CompletedProcess(command, 0, identity[0] + "\n", "")
            raise AssertionError("Unexpected runtime command")

        with patch("platform.system", return_value="Darwin"):
            self.server.monitor.source._liveness = LivenessStore(command=process_probe, clock=lambda: clock[0])
            next_event = self.open_stream()
            while True:
                kind, model = next_event()
                if kind == "state":
                    break
            self.assertEqual(model["liveness"]["status"], "alive")
            self.assertTrue(model["freshness"]["stale"])
            self.assertEqual(model["sync"]["status"], "current")
            for _ in range(10):
                kind, heartbeat = next_event()
                if kind == "heartbeat":
                    self.assertEqual(heartbeat["liveness"]["status"], "alive")
                    break
            else:
                self.fail("No liveness heartbeat")
            identity[0] = "different-process-start"
            clock[0] += 3
            for _ in range(20):
                kind, changed = next_event()
                if kind == "state" and changed["liveness"]["status"] == "exited":
                    self.assertEqual(changed["tasks"], model["tasks"])
                    self.assertEqual(changed["sync"]["status"], "current")
                    self.assertTrue(changed["freshness"]["stale"])
                    break
            else:
                self.fail("A harness exit did not update the stream")

    def test_primary_monitor_tracks_native_wait_without_task_changes(self):
        from subprocess import CompletedProcess
        from live_gantt.liveness import LivenessStore

        registry = self.root / "sessions" / f"{os.getpid()}.json"
        registry.parent.mkdir()
        record = {"sessionId": "session-one", "pid": os.getpid(), "pidDomain": "darwin",
                  "procStart": "native-start", "status": "busy",
                  "statusUpdatedAt": int(datetime.now(timezone.utc).timestamp() * 1000)}
        registry.write_text(json.dumps(record))
        clock = [0.0]
        with patch("platform.system", return_value="Darwin"):
            self.server.monitor.source._liveness = LivenessStore(
                command=lambda command, **kwargs: CompletedProcess(command, 0, "native-start\n", ""),
                clock=lambda: clock[0])
            next_event = self.open_stream()
            while True:
                kind, model = next_event()
                if kind == "state":
                    break
            self.assertEqual(model["monitor"]["state"], "working")
            self.assertNotIn("_runtime", model)
            record["status"] = "waiting"
            record["waitingFor"] = "permission prompt"
            registry.write_text(json.dumps(record))
            clock[0] = 3.0
            for _ in range(20):
                kind, changed = next_event()
                if kind == "state" and changed["monitor"]["state"] == "needs_you":
                    self.assertEqual(changed["tasks"], model["tasks"])
                    self.assertIn("permission", changed["monitor"]["summary"].lower())
                    break
            else:
                self.fail("The primary monitor did not reflect the native input wait")

    def test_claude_approval_clearance_tracks_hooks_until_native_idle(self):
        from subprocess import CompletedProcess
        from live_gantt.liveness import LivenessStore

        self.write_task("Finished work", status="completed")
        started = datetime.now(timezone.utc) - timedelta(seconds=10)
        transcript = self.root / "projects" / "example" / "session-one.jsonl"
        transcript.parent.mkdir(parents=True)

        def append(seconds, **record):
            with transcript.open("a") as stream:
                stream.write(json.dumps({"timestamp": (started + timedelta(seconds=seconds)).isoformat(),
                                         **record}) + "\n")

        append(0, type="user", message={"content": "Run the test command."})
        registry = self.root / "sessions" / f"{os.getpid()}.json"
        registry.parent.mkdir()
        native = {"sessionId": "session-one", "pid": os.getpid(), "pidDomain": "darwin",
                  "procStart": "native-start", "status": "waiting", "waitingFor": "permission prompt",
                  "statusUpdatedAt": int((started + timedelta(seconds=1)).timestamp() * 1000)}
        registry.write_text(json.dumps(native))
        clock = [0.0]

        def read():
            connection = self.connection()
            connection.request("GET", "/api/state?list=session-one")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            return json.load(response)

        with patch("platform.system", return_value="Darwin"):
            self.server.monitor.source._liveness = LivenessStore(
                command=lambda command, **kwargs: CompletedProcess(command, 0, "native-start\n", ""),
                clock=lambda: clock[0])
            self.assertEqual(read()["monitor"]["state"], "needs_you")
            native.update(status="busy", statusUpdatedAt=int((started + timedelta(seconds=2)).timestamp() * 1000))
            native.pop("waitingFor")
            registry.write_text(json.dumps(native))
            clock[0] += 3
            append(3, type="assistant", message={"content": [{"type": "text", "text": "Command finished."}],
                                                "stop_reason": "end_turn"})
            response = read()
            self.assertEqual(response["monitor"]["state"], "working")
            self.assertEqual(response["monitor"]["issues"], [])
            self.assertNotIn("_runtime", response)
            append(4, type="system", subtype="turn_duration")
            self.assertEqual(read()["monitor"]["state"], "attention")
            native.update(status="idle", statusUpdatedAt=int((started + timedelta(seconds=5)).timestamp() * 1000))
            registry.write_text(json.dumps(native))
            clock[0] += 3
            self.assertEqual(read()["monitor"]["state"], "plan_complete")

    def capture_quota(self, percent, reset):
        from live_gantt.capture_usage import capture
        self.assertTrue(capture({"session_id": "account-observer", "rate_limits": {
            "five_hour": {"used_percentage": percent, "resets_at": reset.timestamp()}}},
            datetime.now(timezone.utc), self.root / "usage"))

    def test_quota_delays_reach_the_public_task_and_milestone_response(self):
        reset = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=2)
        self.capture_quota(100, reset)
        connection = self.connection()
        connection.request("GET", "/api/state?list=session-one")
        model = json.load(connection.getresponse())
        task, = model["tasks"]
        self.assertEqual(datetime.fromisoformat(task["projectedStart"]), reset)
        self.assertEqual(datetime.fromisoformat(task["projectedEnd"]), reset + timedelta(hours=2))
        self.assertTrue(task["projectionAnchored"])
        self.assertTrue(model["quotaConstrained"])
        self.assertEqual(task["quotaDelays"][0]["provider"], "claude")
        self.assertEqual(datetime.fromisoformat(model["milestones"][0]["p50"]),
                         reset + timedelta(hours=2))

    def test_quota_change_reprojects_stream_without_a_task_file_change(self):
        reset = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=2)
        self.capture_quota(100, reset)
        next_event = self.open_stream()
        while True:
            kind, model = next_event()
            if kind == "state":
                break
        self.assertTrue(model.get("quotaConstrained"))
        modified = self.task.stat().st_mtime_ns
        self.capture_quota(0, reset)
        for _ in range(12):
            kind, model = next_event()
            if kind == "state" and not model.get("quotaConstrained"):
                self.assertEqual(self.task.stat().st_mtime_ns, modified)
                self.assertLess(datetime.fromisoformat(model["tasks"][0]["projectedStart"]), reset)
                break
        else:
            self.fail("Quota recovery did not update the timeline")

    def test_other_provider_and_external_run_keep_their_schedule(self):
        reset = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=2)
        self.capture_quota(100, reset)
        connection = self.connection()
        for metadata in ({"provider": "codex"}, {"runs": [{
                "label": "External build", "started": (reset - timedelta(hours=3)).isoformat(),
                "ended": None, "log": None}]}):
            with self.subTest(metadata=metadata):
                self.write_task("Independent work", **metadata)
                connection.request("GET", "/api/state?list=session-one")
                task = json.load(connection.getresponse())["tasks"][0]
                self.assertLess(datetime.fromisoformat(task["projectedStart"]), reset)
                self.assertEqual(task.get("quotaDelays"), [])
                if metadata.get("runs"):
                    self.assertEqual(datetime.fromisoformat(task["runs"][0]["started"]),
                                     reset - timedelta(hours=3))


if __name__ == "__main__":
    unittest.main()
