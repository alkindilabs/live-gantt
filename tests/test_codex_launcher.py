import json
import fcntl
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest.mock import patch

from live_gantt import codex_launcher
from live_gantt.liveness import PS


ROOT = Path(__file__).resolve().parents[1]
SESSION = "12345678-1234-4123-8123-123456789abc"
FORK = "abcdef12-1234-4123-8123-123456789abc"
PROCESS_LISTING_SECONDS = 20
FAKE_CODEX = r'''
from base64 import b64encode
from hashlib import sha1
import json, os, pathlib, socket, struct, subprocess, sys, threading, time, tomllib

def record(kind, **data):
    with open(os.environ["LAUNCHER_TEST_TRACE"], "a") as stream:
        stream.write(json.dumps({"kind": kind, "pid": os.getpid(), **data}) + "\n")

args = sys.argv[1:]
record("process", args=args, executable=sys.argv[0], cwd=os.getcwd(), codex_home=os.environ.get("CODEX_HOME"))
if "--version" in args:
    print("codex-test 1")
    sys.exit(0)
if "app-server" not in args:
    if any(value.startswith("sandbox_workspace_write.writable_roots=") for value in args):
        print("Error: sandbox_workspace_write.writable_roots overrides are not supported with --remote", file=sys.stderr)
        sys.exit(2)
    endpoint = args[args.index("--remote") + 1].removeprefix("unix://")
    record("tui", endpoint=endpoint, exists=pathlib.Path(endpoint).is_socket())
    print("native terminal output", flush=True)
    if os.environ.get("LAUNCHER_TEST_WAIT"):
        time.sleep(60)
    sys.exit(23)
if os.environ.get("LAUNCHER_TEST_SERVER_EXIT"):
    print("private credential must not escape", file=sys.stderr, flush=True)
    sys.exit(8)
if os.environ.get("LAUNCHER_TEST_DESCENDANT"):
    child_code = ("import fcntl,os,signal,time; "
                  "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                  "f=open(os.environ['LAUNCHER_TEST_DESCENDANT'],'w'); "
                  "fcntl.flock(f,fcntl.LOCK_EX); f.write(str(os.getpid())); f.flush(); time.sleep(60)")
    subprocess.Popen([sys.executable, "-c", child_code])
    while not pathlib.Path(os.environ["LAUNCHER_TEST_DESCENDANT"]).exists():
        time.sleep(0.01)
config = {"approval_policy": "on-request", "sandbox_mode": "workspace-write"}
config.update(json.loads(os.environ.get("LAUNCHER_TEST_CONFIG", "{}")))
for index, value in enumerate(args):
    if value == "-c":
        config.update(tomllib.loads(args[index + 1]))
endpoint = args[args.index("--listen") + 1].removeprefix("unix://")
listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
listener.bind(endpoint)
os.chmod(endpoint, 0o600)
listener.listen()
record("server", endpoint=endpoint)

def read(connection, length):
    data = b""
    while len(data) < length:
        block = connection.recv(length - len(data))
        if not block:
            raise EOFError()
        data += block
    return data

def serve(connection):
    with connection:
        try:
            header = b""
            while not header.endswith(b"\r\n\r\n"):
                header += read(connection, 1)
            key = next(line.split(b":", 1)[1].strip() for line in header.split(b"\r\n")
                       if line.lower().startswith(b"sec-websocket-key:"))
            accept = b64encode(sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
            connection.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                               b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n")
            while True:
                first, second = read(connection, 2)
                length = second & 127
                if length == 126:
                    length = struct.unpack("!H", read(connection, 2))[0]
                elif length == 127:
                    length = struct.unpack("!Q", read(connection, 8))[0]
                mask = read(connection, 4)
                body = read(connection, length)
                message = json.loads(bytes(value ^ mask[index % 4] for index, value in enumerate(body)))
                method = message["method"]
                record("request", method=method, params=message.get("params"))
                if "id" not in message:
                    continue
                if method == "initialize":
                    result = {"userAgent": "codex-test/1"}
                elif method == "config/read":
                    result = {"config": config, "layers": json.loads(os.environ.get("LAUNCHER_TEST_LAYERS", "[]"))}
                elif method == "configRequirements/read":
                    result = json.loads(os.environ.get("LAUNCHER_TEST_REQUIREMENTS", '{"requirements": null}'))
                else:
                    thread_id = ("abcdef12-1234-4123-8123-123456789abc" if method == "thread/fork"
                                 else message.get("params", {}).get("threadId", "12345678-1234-4123-8123-123456789abc"))
                    mode = {"read-only": "readOnly", "workspace-write": "workspaceWrite",
                            "danger-full-access": "dangerFullAccess"}[config["sandbox_mode"]]
                    cwd = os.environ.get("LAUNCHER_TEST_SAVED_CWD", os.getcwd())
                    sandbox = {"type": mode}
                    if mode == "readOnly":
                        sandbox["networkAccess"] = False
                    if mode == "workspaceWrite":
                        sandbox.update(writableRoots=[], networkAccess=False, excludeSlashTmp=False,
                                       excludeTmpdirEnvVar=False)
                    result = {"thread": {"id": thread_id, "cwd": cwd}, "cwd": cwd,
                              "approvalPolicy": config["approval_policy"], "approvalsReviewer": "user",
                              "sandbox": sandbox, "activePermissionProfile": None}
                    result.update(json.loads(os.environ.get("LAUNCHER_TEST_POLICY", "{}")))
                payload = json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}).encode()
                prefix = b"\x81" + (bytes([len(payload)]) if len(payload) < 126
                                     else b"\x7e" + struct.pack("!H", len(payload)))
                connection.sendall(prefix + payload)
        except (EOFError, OSError):
            return

while True:
    connection, _ = listener.accept()
    threading.Thread(target=serve, args=(connection,), daemon=True).start()
'''


class CodexLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="launcher test ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.project = self.root / "project with spaces"
        self.project.mkdir()
        self.codex_home = self.root / "codex home"
        self.codex_home.mkdir()
        self.config = self.codex_home / "config.toml"
        self.config.write_text('model = "preserved-model"\n')
        self.trace = self.root / "native trace.jsonl"
        self.binary = self.root / "native codex"
        self.binary.write_text(f"#!{sys.executable}\n" + textwrap.dedent(FAKE_CODEX))
        self.binary.chmod(0o700)
        self.environment = {
            **os.environ, "HOME": str(self.root), "XDG_DATA_HOME": str(self.root / "data"),
            "PYTHONPATH": str(ROOT), "CODEX_HOME": str(self.codex_home),
            "LIVE_GANTT_CODEX_BIN": str(self.binary), "LAUNCHER_TEST_TRACE": str(self.trace),
        }
        self.command = [sys.executable, "-B", "-P", "-m", "live_gantt.codex_launcher"]
        self.addCleanup(self.cleanup_children)

    def run_launcher(self, *args):
        return subprocess.run(self.command + list(args), cwd=self.project, env=self.environment,
                              capture_output=True, text=True, timeout=15)

    def events(self):
        return [json.loads(line) for line in self.trace.read_text().splitlines()] if self.trace.exists() else []

    def running_process_groups(self):
        output = subprocess.check_output([PS, "-axo", "pid=,pgid=,stat="], text=True, timeout=PROCESS_LISTING_SECONDS)
        rows = [line.split() for line in output.splitlines()]
        return {int(pid): int(group) for pid, group, status in rows if not status.startswith("Z")}

    def cleanup_children(self):
        for event in self.events():
            if event["kind"] not in ("server", "tui"):
                continue
            def present():
                processes = self.running_process_groups()
                return (event["pid"] in processes.values() if event["kind"] == "server"
                        else event["pid"] in processes)
            if not present():
                continue
            try:
                if event["kind"] == "server":
                    os.killpg(event["pid"], signal.SIGKILL)
                elif event["kind"] == "tui":
                    os.kill(event["pid"], signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                if present():
                    raise

    def assert_cleaned(self):
        servers = [event for event in self.events() if event["kind"] == "server"]
        for server in servers:
            self.assertFalse(Path(server["endpoint"]).parent.exists())
            self.assertNotIn(server["pid"], self.running_process_groups().values())
        self.assertFalse(list(self.root.rglob("codex-runtimes/*.json")))
        self.assertEqual(self.config.read_text(), 'model = "preserved-model"\n')

    def test_new_session_preserves_prompt_flags_environment_and_exit_status(self):
        result = self.run_launcher("-C", str(self.project), "-m", "selected-model", "-s", "read-only",
                                   "-a", "never", "--search", "--no-alt-screen", "prompt with 'quotes'")
        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertIn("native terminal output", result.stdout)
        events = self.events()
        processes = [event for event in events if event["kind"] == "process"]
        self.assertEqual(len(processes), 2)
        self.assertTrue(all(event["codex_home"] == str(self.codex_home) for event in processes))
        self.assertTrue(all(event["cwd"] == str(self.project) for event in processes))
        self.assertIn('model="selected-model"', processes[0]["args"])
        self.assertIn('sandbox_mode="read-only"', processes[0]["args"])
        self.assertIn('approval_policy="never"', processes[0]["args"])
        self.assertIn("--no-alt-screen", processes[1]["args"])
        self.assertEqual(processes[1]["args"][-1], "prompt with 'quotes'")
        methods = [event["method"] for event in events if event["kind"] == "request"]
        self.assertIn("config/read", methods)
        self.assertIn("configRequirements/read", methods)
        self.assertIn("thread/start", methods)
        self.assertNotIn("turn/start", methods)
        self.assert_cleaned()

    def test_installed_settings_supply_defaults_but_never_replace_explicit_environment(self):
        installed = self.root / "stable installation"
        shutil.copytree(ROOT / "live_gantt", installed / "live_gantt",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        settings = installed / "installation.json"
        content = json.dumps({"LIVE_GANTT_CODEX_BIN": str(self.binary), "CODEX_HOME": str(self.codex_home)})
        settings.write_text(content)
        self.environment["PYTHONPATH"] = str(installed)
        self.environment["PATH"] = ""
        self.environment.pop("LIVE_GANTT_CODEX_BIN")
        self.environment.pop("CODEX_HOME")
        result = self.run_launcher("--version")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "codex-test 1")
        self.assertEqual(self.events()[-1]["codex_home"], str(self.codex_home))
        alternate = self.root / "explicit native codex"
        shutil.copy2(self.binary, alternate)
        self.environment["LIVE_GANTT_CODEX_BIN"] = str(alternate)
        self.environment["CODEX_HOME"] = str(self.root / "explicit codex home")
        result = self.run_launcher("--version")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events()[-1]["executable"], str(alternate))
        self.assertEqual(self.events()[-1]["codex_home"], self.environment["CODEX_HOME"])
        self.assertEqual(settings.read_text(), content)

    def test_native_command_cannot_point_back_to_the_installed_launcher(self):
        shim = self.root / ".local/bin/live-gantt-codex"
        shim.parent.mkdir(parents=True)
        shim.symlink_to(self.binary)
        self.environment["LIVE_GANTT_CODEX_BIN"] = str(shim)
        result = self.run_launcher("--version")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("LIVE_GANTT_CODEX_BIN", result.stderr)
        self.assertEqual(self.events(), [])

    def test_unsupported_flags_and_ambiguous_resumes_never_start_native_process(self):
        for args in (("--profile", "custom"), ("-c", 'sandbox_mode="read-only"'),
                     ("--add-dir", str(self.root)), ("--remote", "unix:///other"), ("--no-daemon",),
                     ("resume",), ("resume", "not-a-uuid"), ("resume", SESSION, "-a", "never"),
                     ("resume", SESSION, "-C", str(self.project))):
            with self.subTest(args=args):
                result = self.run_launcher(*args)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(self.events(), [])

    def test_saved_resume_and_fork_preflight_the_explicit_thread(self):
        for mode in ("resume", "fork"):
            with self.subTest(mode=mode):
                result = self.run_launcher(mode, SESSION, "continue this")
                self.assertEqual(result.returncode, 23, result.stderr)
                events = self.events()
                request = next(event for event in reversed(events)
                               if event.get("method") == f"thread/{mode}")
                self.assertEqual(request["params"]["threadId"], SESSION)
                self.assertNotIn("cwd", request["params"])
                process = next(event for event in reversed(events) if event["kind"] == "process")
                self.assertIn(FORK if mode == "fork" else SESSION, process["args"])
                self.assert_cleaned()

    def test_custom_profiles_and_unsupported_effective_policies_block_terminal(self):
        cases = (("LAUNCHER_TEST_CONFIG", {"profile": "custom"}),
                 ("LAUNCHER_TEST_LAYERS", [{"config": {"profile": "custom"}}]),
                 ("LAUNCHER_TEST_REQUIREMENTS", {}),
                 ("LAUNCHER_TEST_REQUIREMENTS", {"requirements": {"defaultPermissions": "custom"}}),
                 ("LAUNCHER_TEST_POLICY", {"activePermissionProfile": "custom"}),
                 ("LAUNCHER_TEST_POLICY", {"approvalPolicy": "untrusted"}),
                 ("LAUNCHER_TEST_POLICY", {"approvalsReviewer": "unsupported-reviewer"}),
                 ("LAUNCHER_TEST_POLICY", {"sandbox": {"type": "readOnly", "networkAccess": True}}),
                 ("LAUNCHER_TEST_POLICY", {"sandbox": {"type": "workspaceWrite", "writableRoots": "invalid"}}),
                 ("LAUNCHER_TEST_POLICY", {"sandbox": {"type": "workspaceWrite", "writableRoots": [str(self.root)],
                    "networkAccess": False, "excludeSlashTmp": False, "excludeTmpdirEnvVar": False}}))
        for variable, value in cases:
            with self.subTest(value=value):
                self.environment[variable] = json.dumps(value)
                previous_terminals = sum(event["kind"] == "tui" for event in self.events())
                result = self.run_launcher()
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(sum(event["kind"] == "tui" for event in self.events()), previous_terminals)
                self.assertNotIn("private credential", result.stderr)
                self.assert_cleaned()
                del self.environment[variable]

    def test_builtin_permission_profiles_preserve_effective_policy_for_new_resume_and_fork(self):
        cases = (([], "read-only", ":read-only"),
                 (["resume", SESSION], "workspace-write", ":workspace"),
                 (["fork", SESSION], "danger-full-access", ":danger-full-access"))
        for arguments, sandbox, profile in cases:
            with self.subTest(sandbox=sandbox):
                self.environment["LAUNCHER_TEST_CONFIG"] = json.dumps({"sandbox_mode": sandbox})
                self.environment["LAUNCHER_TEST_POLICY"] = json.dumps({
                    "activePermissionProfile": {"id": profile, "extends": None},
                    "approvalPolicy": "never", "approvalsReviewer": "auto_review",
                })
                result = self.run_launcher(*arguments)
                self.assertEqual(result.returncode, 23, result.stderr)
                terminal = next(event for event in reversed(self.events()) if event["kind"] == "process")
                self.assertIn(f'sandbox_mode="{sandbox}"', terminal["args"])
                self.assertIn('approval_policy="never"', terminal["args"])
                self.assertIn('approvals_reviewer="auto_review"', terminal["args"])
                self.assert_cleaned()

    def test_custom_malformed_and_mismatched_builtin_profiles_block_terminal(self):
        profiles = ({"id": "custom", "extends": None},
                    {"id": ":read-only", "extends": None},
                    {"id": ":danger-full-access", "extends": None},
                    {"id": ":workspace", "extends": "custom"},
                    {"id": ":workspace", "extends": False},
                    {"id": ":workspace", "permissions": {}},
                    {"id": [":workspace"]}, {})
        for profile in profiles:
            with self.subTest(profile=profile):
                self.environment["LAUNCHER_TEST_POLICY"] = json.dumps({"activePermissionProfile": profile})
                result = self.run_launcher()
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertFalse(any(event["kind"] == "tui" for event in self.events()))
                self.assert_cleaned()

    def test_saved_session_uses_its_original_directory_and_full_sandbox(self):
        saved = self.root / "original workspace"
        saved.mkdir()
        self.environment["LAUNCHER_TEST_SAVED_CWD"] = str(saved)
        self.environment["LAUNCHER_TEST_POLICY"] = json.dumps({"sandbox": {
            "type": "workspaceWrite", "writableRoots": [], "networkAccess": False,
            "excludeSlashTmp": True, "excludeTmpdirEnvVar": True,
        }})
        result = self.run_launcher("resume", SESSION)
        self.assertEqual(result.returncode, 23, result.stderr)
        process = next(event for event in reversed(self.events()) if event["kind"] == "process")
        self.assertEqual(process["cwd"], str(saved))
        self.assertFalse(any(value.startswith("sandbox_workspace_write.writable_roots=") for value in process["args"]))
        self.assertIn("sandbox_workspace_write.exclude_slash_tmp=true", process["args"])
        self.assertIn("sandbox_workspace_write.exclude_tmpdir_env_var=true", process["args"])
        self.assert_cleaned()

    def test_shutdown_kills_descendants_after_the_server_leader_exits(self):
        lock = self.root / "descendant.lock"
        self.environment["LAUNCHER_TEST_DESCENDANT"] = str(lock)
        result = self.run_launcher()
        self.assertEqual(result.returncode, 23, result.stderr)
        with lock.open() as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assert_cleaned()

    def test_server_failure_is_sanitized_and_leaves_no_runtime(self):
        self.environment["LAUNCHER_TEST_SERVER_EXIT"] = "1"
        result = self.run_launcher()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("server", result.stderr.lower())
        self.assertNotIn("private credential", result.stderr)
        self.assertEqual(len(self.events()), 1)
        self.assert_cleaned()

    def test_termination_reaps_owned_processes_and_runtime(self):
        self.assert_termination_cleans_up(signal.SIGTERM)

    def test_signal_during_terminal_process_creation_still_reaps_the_terminal(self):
        real_popen = subprocess.Popen
        real_killpg = os.killpg
        terminals = []
        self.environment["LAUNCHER_TEST_WAIT"] = "1"

        def signal_group(pid, signum):
            if signum == 0:
                raise PermissionError("Zero-signal process-group probes are unavailable")
            return real_killpg(pid, signum)

        def create_process(command, *args, **kwargs):
            terminal_command = "--remote" in command
            if terminal_command:
                kwargs.update(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            process = real_popen(command, *args, **kwargs)
            if terminal_command:
                terminals.append(process)
                def cleanup():
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=5)
                self.addCleanup(cleanup)
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and not any(event["kind"] == "tui" for event in self.events()):
                    time.sleep(0.01)
                os.kill(os.getpid(), signal.SIGTERM)
            return process

        with patch.dict(os.environ, self.environment, clear=True), \
                patch("live_gantt.codex_launcher.subprocess.Popen", side_effect=create_process), \
                patch("live_gantt.codex_launcher.os.killpg", side_effect=signal_group):
            result = codex_launcher.main([])
        self.assertEqual(result, 128 + signal.SIGTERM)
        self.assertEqual(len(terminals), 1)
        self.assertIsNotNone(terminals[0].poll(), "Terminal created before signal was left running")
        self.assert_cleaned()

    def test_terminal_close_reaps_owned_processes_and_runtime(self):
        self.assert_termination_cleans_up(signal.SIGHUP)

    def assert_termination_cleans_up(self, signum):
        self.environment["LAUNCHER_TEST_WAIT"] = "1"
        process = subprocess.Popen(self.command, cwd=self.project, env=self.environment,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def cleanup():
            self.cleanup_children()
            if process.poll() is None:
                process.kill()
            process.communicate()
        self.addCleanup(cleanup)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and process.poll() is None:
            if any(event["kind"] == "tui" for event in self.events()):
                break
            time.sleep(0.02)
        self.assertTrue(any(event["kind"] == "tui" for event in self.events()))
        process.send_signal(signum)
        stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 128 + signum, stderr)
        self.assert_cleaned()


if __name__ == "__main__":
    unittest.main()
