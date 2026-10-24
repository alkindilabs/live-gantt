from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import platform
import select
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from live_gantt.liveness import CACHE_SECONDS, LivenessStore, MAX_REGISTRY_FILES, run_command


NOW = datetime(2026, 10, 18, tzinfo=timezone.utc)
SESSION = "example-session"
SIGNATURE = "Sun Oct 18 00:00:00 2026"
REAL_INSPECTION_ATTEMPTS = 20


class LivenessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.clock = [0.0]
        self.calls = []
        self.signature = SIGNATURE
        self.openers = [os.getpid()]
        self.store = LivenessStore(command=self.command, clock=lambda: self.clock[0])

    def command(self, command, *, environment, deadline):
        self.calls.append(command)
        self.assertNotIn("command=", command)
        if command[0].endswith("ps"):
            self.assertEqual(environment["TZ"], "UTC")
            self.assertEqual(environment["LC_ALL"], "C")
            output = self.signature + "\n"
        else:
            info = (self.root / "thread-writer-locks" / (SESSION + ".lock")).stat()
            output = "".join(f"p{pid}\0\nf9\0i{info.st_ino}\0D0x{info.st_dev:x}\0\n" for pid in self.openers)
        return subprocess.CompletedProcess(command, 0, stdout=output)

    def assertReadsEventually(self, expected):
        for _ in range(REAL_INSPECTION_ATTEMPTS):
            self.clock[0] += CACHE_SECONDS
            status = self.read("codex")["status"]
            if status == expected:
                return
            self.assertEqual(status, "unknown")
        self.assertEqual(status, expected)

    def registry(self, *, pid=None, signature=SIGNATURE, domain="darwin", session=SESSION, **runtime):
        directory = self.root / "sessions"
        directory.mkdir(exist_ok=True)
        path = directory / f"{os.getpid() if pid is None else pid}.json"
        path.write_text(json.dumps({"pid": os.getpid() if pid is None else pid,
                                    "sessionId": session, "procStart": signature,
                                    "pidDomain": domain, "status": "idle", **runtime}))
        return path

    def read(self, provider="claude"):
        with patch("platform.system", return_value="Darwin"):
            return self.store.state(provider, SESSION, self.root, NOW + timedelta(seconds=self.clock[0]))

    def runtime(self, provider="claude"):
        with patch("platform.system", return_value="Darwin"):
            return self.store.runtime(provider, SESSION, self.root, NOW + timedelta(seconds=self.clock[0]))

    def test_verified_runtime_reuses_liveness_observation_and_native_activity_time(self):
        native_time = NOW.timestamp() * 1000
        for status, phase in (("busy", "working"), ("shell", "working"), ("waiting", "waiting"), ("idle", "idle")):
            with self.subTest(status=status):
                self.registry(status=status, statusUpdatedAt=native_time, waitingFor="permission prompt")
                self.clock[0] += 3
                self.assertEqual(self.read()["status"], "alive")
                count = len(self.calls)
                runtime = self.runtime()
                self.assertEqual(runtime["phase"], phase)
                self.assertEqual(runtime["lastActivityAt"], "2026-10-18T00:00:00Z")
                self.assertEqual(set(runtime), {"phase", "lastActivityAt", "detail"})
                self.assertEqual(len(self.calls), count)
                runtime["phase"] = "changed"
                self.assertEqual(self.runtime()["phase"], phase)
                self.assertEqual(set(self.read()), {"status", "checkedAt", "evidence", "detail"})

    def test_waiting_reason_is_allowlisted_and_requires_verified_binding(self):
        self.registry(status="waiting", waitingFor="permission prompt")
        self.assertEqual(self.runtime()["detail"], "Permission is required.")
        count = len(self.calls)
        self.assertEqual(self.read()["status"], "alive")
        self.assertEqual(len(self.calls), count)
        self.registry(status="waiting", waitingFor="private prompt or token", statusUpdatedAt=True)
        self.clock[0] += 3
        runtime = self.runtime()
        self.assertEqual(runtime["phase"], "waiting")
        self.assertIsNone(runtime["lastActivityAt"])
        self.assertNotIn("private", str(runtime))
        self.registry(status="waiting", signature="reused-process", waitingFor="permission prompt")
        self.clock[0] += 3
        self.assertEqual(self.runtime(), {"phase": "unknown", "lastActivityAt": None, "detail": None})

    def test_runtime_is_unknown_when_registry_changes_during_identity_check(self):
        self.registry(status="busy")
        original = self.command
        def changed(command, **kwargs):
            result = original(command, **kwargs)
            self.registry(status="waiting", signature="replacement")
            return result
        self.store = LivenessStore(command=changed)
        self.assertEqual(self.runtime()["phase"], "unknown")

    def test_exact_registry_process_is_alive_and_cached_without_exposing_identity(self):
        self.registry()
        first = self.read()
        self.assertEqual(first["status"], "alive")
        self.assertEqual(first["evidence"], "native_registry_process")
        self.assertNotIn(str(os.getpid()), json.dumps(first))
        self.assertNotIn(str(self.root), json.dumps(first))
        self.assertNotIn(SIGNATURE, json.dumps(first))
        count = len(self.calls)
        first["status"] = "changed"
        self.assertEqual(self.read()["status"], "alive")
        self.assertEqual(len(self.calls), count)
        self.clock[0] = 3
        self.assertEqual(self.read()["status"], "alive")
        self.assertGreater(len(self.calls), count)

    def test_pid_reuse_and_reaped_process_are_exited_not_alive(self):
        self.registry(signature="old-start")
        self.assertEqual(self.read()["status"], "exited")
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait(timeout=2)
        self.registry(pid=child.pid, session="reaped")
        with patch("platform.system", return_value="Darwin"):
            result = self.store.state("claude", "reaped", self.root, NOW)
        self.assertEqual(result["status"], "exited")

    def test_missing_malformed_foreign_domain_and_permissions_are_unknown(self):
        self.assertEqual(self.read()["status"], "unknown")
        self.assertEqual(self.runtime()["phase"], "unknown")
        path = self.registry(domain="linux")
        self.clock[0] += 3
        self.assertEqual(self.read()["status"], "unknown")
        path.write_text("not json")
        self.clock[0] += 3
        self.assertEqual(self.read()["status"], "unknown")
        self.registry()
        original_open = os.open

        def denied(path, *args, **kwargs):
            if Path(path).suffix == ".json":
                raise PermissionError("private path or credentials")
            return original_open(path, *args, **kwargs)

        self.clock[0] += 3
        with patch("os.open", denied):
            result = self.read()
        self.assertEqual(result["status"], "unknown")
        self.assertNotIn("credentials", str(result))
        self.assertEqual(self.runtime()["phase"], "unknown")

    def test_registry_identity_change_during_inspection_is_unknown(self):
        path = self.registry()
        command = self.command

        def changing(command_line, **kwargs):
            result = command(command_line, **kwargs)
            self.registry(signature="replacement")
            return result

        self.store = LivenessStore(command=changing)
        self.assertEqual(self.read()["status"], "unknown")
        self.assertTrue(path.exists())

    @unittest.skipUnless(platform.system() == "Darwin", "Darwin flock query contract")
    def test_codex_owned_writer_lock_is_alive_but_an_unloaded_live_process_is_unknown(self):
        path = self.root / "thread-writer-locks" / (SESSION + ".lock")
        path.parent.mkdir()
        with path.open("w") as owned:
            fcntl.flock(owned, fcntl.LOCK_EX | fcntl.LOCK_NB)
            first = self.read("codex")
            self.assertEqual(first["status"], "alive")
            self.assertEqual(first["evidence"], "session_writer_lock")
        self.clock[0] += 3
        self.assertEqual(self.read("codex")["status"], "unknown")
        self.signature = "different-process-start"
        self.clock[0] += 3
        self.assertEqual(self.read("codex")["status"], "exited")

    @unittest.skipUnless(platform.system() == "Darwin", "Darwin flock query contract")
    def test_codex_multiple_openers_and_replaced_inode_are_unknown(self):
        path = self.root / "thread-writer-locks" / (SESSION + ".lock")
        path.parent.mkdir()
        with path.open("w") as owned:
            fcntl.flock(owned, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.openers.append(os.getpid() + 1)
            self.assertEqual(self.read("codex")["status"], "unknown")
            self.openers.pop()
            original_command = self.command

            def replaced(command, **kwargs):
                result = original_command(command, **kwargs)
                if command[0].endswith("lsof"):
                    replacement = path.with_suffix(".replacement")
                    replacement.write_text("")
                    replacement.replace(path)
                return result

            self.store = LivenessStore(command=replaced)
            self.assertEqual(self.read("codex")["status"], "unknown")

    def test_unsupported_platform_and_invalid_identity_are_unknown(self):
        with patch("platform.system", return_value="Unsupported"):
            result = self.store.state("claude", SESSION, self.root, NOW)
            self.assertEqual(self.store.runtime("claude", SESSION, self.root, NOW)["phase"], "unknown")
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(self.store.state("codex", "../outside", self.root, NOW)["status"], "unknown")
        self.assertEqual(self.store.state("codex", SESSION, None, NOW)["status"], "unknown")
        self.assertEqual(self.calls, [])

    def test_command_failure_and_excess_output_are_unknown(self):
        self.registry()
        for failure in (subprocess.TimeoutExpired("ps", 0.1), PermissionError("private error")):
            with self.subTest(kind=type(failure).__name__):
                def failed(*args, **kwargs):
                    raise failure
                self.store = LivenessStore(command=failed)
                result = self.read()
                self.assertEqual(result["status"], "unknown")
                self.assertNotIn("private", str(result))
        self.store = LivenessStore(command=lambda command, **kwargs: subprocess.CompletedProcess(command, 0, stdout=b"x" * 100000))
        self.assertEqual(self.read()["status"], "unknown")

    def test_bounded_runner_stops_owned_commands_on_timeout_or_output_limit(self):
        import time
        for script in ("import time; time.sleep(3)", "import os; os.write(1, b'x' * 100000)"):
            with self.subTest(script=script):
                self.registry()
                def mapped(command, *, environment, deadline):
                    return run_command([sys.executable, "-c", script], environment=environment,
                                       deadline=min(deadline, time.monotonic() + 0.1))
                self.store = LivenessStore(command=mapped)
                self.assertEqual(self.read()["status"], "unknown")

    def test_truncated_registry_scan_cannot_claim_unique_ownership(self):
        path = self.registry()
        entries = [path] + [path.with_name(f"unread-{index}.json") for index in range(MAX_REGISTRY_FILES)]
        with patch.object(Path, "iterdir", return_value=iter(entries)):
            self.assertEqual(self.read()["status"], "unknown")

    @unittest.skipUnless(platform.system() == "Darwin", "Darwin flock query contract")
    def test_failed_lsof_and_changed_lock_evidence_are_unknown(self):
        path = self.root / "thread-writer-locks" / (SESSION + ".lock")
        path.parent.mkdir()
        with path.open("w") as owned:
            fcntl.flock(owned, fcntl.LOCK_EX | fcntl.LOCK_NB)
            original = self.command
            for defect in ("failure", "inode", "device", "released"):
                with self.subTest(defect=defect):
                    def changed(command, **kwargs):
                        result = original(command, **kwargs)
                        if command[0].endswith("lsof"):
                            if defect == "failure":
                                result.returncode = 1
                            elif defect == "inode":
                                result.stdout = result.stdout.replace(f"i{path.stat().st_ino}", "i0")
                            elif defect == "device":
                                result.stdout = result.stdout.replace(f"D0x{path.stat().st_dev:x}", "D0x0")
                            else:
                                fcntl.flock(owned, fcntl.LOCK_UN)
                        return result
                    self.store = LivenessStore(command=changed)
                    self.assertEqual(self.read("codex")["status"], "unknown")

    @unittest.skipUnless(platform.system() == "Darwin", "Real Darwin process ownership")
    def test_real_owned_child_lock_is_alive_then_exited(self):
        path = self.root / "thread-writer-locks" / (SESSION + ".lock")
        path.parent.mkdir()
        script = ("import fcntl,sys; f=open(sys.argv[1],'w'); "
                  "fcntl.flock(f,fcntl.LOCK_EX); print('ready',flush=True); sys.stdin.read()")
        child = subprocess.Popen([sys.executable, "-c", script, str(path)], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            self.assertTrue(select.select([child.stdout], [], [], 1)[0])
            self.assertEqual(child.stdout.readline().strip(), b"ready")
            self.store = LivenessStore(clock=lambda: self.clock[0])
            self.assertReadsEventually("alive")
            child.stdin.close()
            child.wait(timeout=1)
            self.assertReadsEventually("exited")
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=1)
            child.stdout.close()
            if not child.stdin.closed:
                child.stdin.close()

    def test_successful_runner_does_not_signal_a_reaped_process(self):
        import time
        with patch("os.killpg") as signal_group:
            result = run_command([sys.executable, "-c", "print('ready')"], environment=dict(os.environ),
                                 deadline=time.monotonic() + 1)
        self.assertEqual(result.stdout, b"ready\n")
        signal_group.assert_not_called()
