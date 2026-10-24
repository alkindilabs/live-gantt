"""Run a native Codex terminal session with a private observable runtime."""

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid

from .installation import CODEX_COMMAND_VARIABLE, CODEX_LAUNCHER_PATH, INSTALLATION_SETTINGS_FILENAME
from .isolated import _register_runtime, runtime_registry_root
from .liveness import PS, run_command
from .providers import _CodexSocket


_START_SECONDS = 10
_POLL_SECONDS = 0.05
_EXIT_SECONDS = 2
_SIGNAL_EXIT_OFFSET = 128
_SESSION_MODES = ("resume", "fork")
_PROMPT_LIMIT = 1
_TERMINATION_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
_SANDBOX_POLICIES = {
    "readOnly": {"mode": "read-only", "profile": ":read-only"},
    "workspaceWrite": {"mode": "workspace-write", "profile": ":workspace"},
    "dangerFullAccess": {"mode": "danger-full-access", "profile": ":danger-full-access"},
}
_APPROVAL_POLICIES = ("on-request", "never")
_REVIEWERS = {"user": "user", "auto_review": "auto_review", "guardian_subagent": "auto_review"}
_VALUE_ARGUMENTS = (
    (("--cd", "-C"), {"type": Path}),
    (("--model", "-m"), {}),
    (("--sandbox", "-s"), {"choices": tuple(policy["mode"] for policy in _SANDBOX_POLICIES.values())}),
    (("--ask-for-approval", "-a"), {"choices": _APPROVAL_POLICIES}),
)
_VERSION_FLAGS = ("--version", "-V")
_INTERACTIVE_SWITCH_FLAGS = ("--no-alt-screen", "--search")


class _LauncherError(ValueError):
    pass


class _LauncherInterrupted(Exception):
    def __init__(self, signum):
        self.signum = signum


def _arguments(argv):
    parser = argparse.ArgumentParser(
        prog="live-gantt-codex", description=__doc__, allow_abbrev=False,
        epilog="Use command codex for profiles, custom configuration, additional directories, or other commands.",
    )
    parser.add_argument(*_VERSION_FLAGS, action="store_true")
    for flags, options in _VALUE_ARGUMENTS:
        parser.add_argument(*flags, **options)
    for flag in _INTERACTIVE_SWITCH_FLAGS:
        parser.add_argument(flag, action="store_true")
    parser.add_argument("arguments", nargs="*", metavar="[PROMPT | resume/fork UUID [PROMPT]]")
    argv = list(sys.argv[1:] if argv is None else argv)
    separator = argv.index("--") if "--" in argv else len(argv)
    args = parser.parse_intermixed_args(argv[:separator])
    literal_prompt = separator < len(argv) and not args.arguments
    args.arguments.extend(argv[separator + 1:])
    args.mode, args.thread_id, args.prompt = "new", None, None
    if args.arguments and not literal_prompt and args.arguments[0] in _SESSION_MODES:
        args.mode, *session_arguments = args.arguments
        thread_argument, *prompts = session_arguments or [None]
        if thread_argument is None or len(prompts) > _PROMPT_LIMIT:
            parser.error("resume and fork require one explicit thread UUID and an optional prompt")
        try:
            args.thread_id = str(uuid.UUID(thread_argument))
        except ValueError:
            parser.error("resume and fork require an explicit thread UUID")
        if (args.sandbox is not None or args.ask_for_approval is not None or args.cd is not None
                or args.model is not None or args.search):
            parser.error("resume and fork preserve saved settings; omit --sandbox, --ask-for-approval, --cd, --model, and --search")
        args.prompt = prompts[0] if prompts else None
    elif len(args.arguments) > _PROMPT_LIMIT:
        parser.error("provide one quoted prompt, or resume/fork with an explicit thread UUID")
    elif args.arguments:
        args.prompt = args.arguments[0]
    return args


def _environment():
    environment = dict(os.environ)
    settings_path = Path(__file__).resolve().parents[1] / INSTALLATION_SETTINGS_FILENAME
    try:
        settings = json.loads(settings_path.read_text())
    except FileNotFoundError:
        settings = {}
    if not isinstance(settings, dict):
        raise _LauncherError("Installed configuration must be an object")
    for variable in (CODEX_COMMAND_VARIABLE, "CODEX_HOME"):
        value = settings.get(variable)
        if variable not in environment and isinstance(value, str) and value:
            environment[variable] = value
    return environment


def _native_binary(environment):
    command = environment.get(CODEX_COMMAND_VARIABLE) or shutil.which("codex", path=environment.get("PATH"))
    if not command:
        raise _LauncherError("Codex is unavailable; install it or set LIVE_GANTT_CODEX_BIN")
    executable = Path(command).expanduser().absolute()
    if executable.resolve() == (Path.home() / CODEX_LAUNCHER_PATH).resolve():
        raise _LauncherError("LIVE_GANTT_CODEX_BIN must point to native codex, not live-gantt-codex")
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise _LauncherError("Codex is unavailable; check LIVE_GANTT_CODEX_BIN")
    return str(executable)


def _config_arguments(values):
    return [item for key, value in values.items() if value is not None
            for item in ("-c", f"{key}={json.dumps(value, separators=(',', ':'))}")]


@contextmanager
def _termination_signals():
    requested_signal = None

    def interrupted(signum, _frame):
        nonlocal requested_signal
        requested_signal = signum
        _ignore_termination_signals()

    def check():
        if requested_signal is not None:
            raise _LauncherInterrupted(requested_signal)

    previous = {signum: signal.signal(signum, interrupted) for signum in _TERMINATION_SIGNALS}
    try:
        yield check
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _ignore_termination_signals():
    for signum in _TERMINATION_SIGNALS:
        signal.signal(signum, signal.SIG_IGN)


def _process_group_running(group):
    result = run_command([PS, "-axo", "pid=,pgid=,uid=,stat="],
                         environment={**os.environ, "LC_ALL": "C"},
                         deadline=time.monotonic() + _EXIT_SECONDS)
    if result.returncode:
        raise _LauncherError("Native Codex process-group membership could not be verified")
    running = False
    for line in result.stdout.decode("utf-8").splitlines():
        pid, process_group, owner, status = line.split()
        if int(process_group) != group:
            continue
        if int(owner) != os.getuid():
            raise _LauncherError("Native Codex process-group ownership could not be verified")
        running = running or not status.startswith("Z")
    return running


def _stop_child(process, *, group=False):
    if process is None:
        return
    try:
        try:
            if group and _process_group_running(process.pid):
                os.killpg(process.pid, signal.SIGTERM)
            elif process.poll() is None:
                process.terminate()
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + _EXIT_SECONDS
        while time.monotonic() < deadline:
            running = _process_group_running(process.pid) if group else process.poll() is None
            if not running:
                break
            time.sleep(_POLL_SECONDS)
        else:
            try:
                if group:
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except ProcessLookupError:
                pass
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=_EXIT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def _wait_for_socket(server, path, check):
    deadline = time.monotonic() + _START_SECONDS
    while time.monotonic() < deadline:
        check()
        if server.poll() is not None:
            raise _LauncherError("Native Codex server exited before startup; run command codex to inspect its configuration")
        if path.exists():
            return
        time.sleep(_POLL_SECONDS)
    raise _LauncherError("Native Codex server startup timed out")


def _validate_configuration(response, requirements):
    if not isinstance(response, dict) or not isinstance(response.get("config"), dict):
        raise _LauncherError("Native Codex configuration could not be verified")
    config = response["config"]
    layers = response.get("layers") or []
    if not isinstance(layers, list):
        raise _LauncherError("Native Codex configuration layers could not be verified")
    if any(not isinstance(layer, dict) or not isinstance(layer.get("config", {}), dict) for layer in layers):
        raise _LauncherError("Native Codex configuration layers could not be verified")
    configurations = [config] + [layer.get("config", {}) for layer in layers]
    if any(configuration.get(key) is not None for configuration in configurations
           for key in ("profile", "permissions", "default_permissions", "permission_profile")):
        raise _LauncherError("Custom Codex permission profiles are unsupported; run command codex")
    if not isinstance(requirements, dict) or "requirements" not in requirements or not (
            requirements.get("requirements") is None or isinstance(requirements["requirements"], dict)):
        raise _LauncherError("Native Codex permission requirements could not be verified")
    constraints = requirements.get("requirements") or {}
    if any(constraints.get(key) is not None for key in ("defaultPermissions", "allowedPermissionProfiles")):
        raise _LauncherError("Custom Codex permission requirements are unsupported; run command codex")


def _sandbox_settings(sandbox):
    mode = sandbox.get("type")
    settings = {"sandbox_mode": _SANDBOX_POLICIES[mode]["mode"]}
    if mode == "workspaceWrite":
        keys = {"writableRoots": "writable_roots", "networkAccess": "network_access",
                "excludeSlashTmp": "exclude_slash_tmp", "excludeTmpdirEnvVar": "exclude_tmpdir_env_var"}
        roots = sandbox.get("writableRoots")
        if (set(sandbox) != {"type", *keys}
                or not isinstance(roots, list)
                or any(not isinstance(root, str) or not Path(root).is_absolute() for root in roots)
                or any(type(sandbox.get(key)) is not bool for key in keys if key != "writableRoots")):
            raise _LauncherError("Native Codex sandbox details are unsupported; run command codex")
        if roots:
            raise _LauncherError("Additional Codex writable roots are unsupported in remote sessions; run command codex")
        settings.update({f"sandbox_workspace_write.{setting}": sandbox[key] for key, setting in keys.items()
                         if key != "writableRoots"})
    elif (set(sandbox) != {"type"}
          and not (mode == "readOnly" and (
              sandbox == {"type": mode, "access": {"type": "fullAccess"}}
              or set(sandbox) == {"type", "networkAccess"} and sandbox["networkAccess"] is False))):
        raise _LauncherError("Custom Codex filesystem access is unsupported; run command codex")
    return settings


def _validate_policy(response):
    if not isinstance(response, dict) or "activePermissionProfile" not in response:
        raise _LauncherError("Custom Codex permission profiles are unsupported; run command codex")
    approval = response.get("approvalPolicy")
    reviewer = response.get("approvalsReviewer")
    sandbox = response.get("sandbox")
    if (approval not in _APPROVAL_POLICIES or reviewer not in _REVIEWERS
            or not isinstance(sandbox, dict) or sandbox.get("type") not in _SANDBOX_POLICIES):
        raise _LauncherError("Native Codex permission policy is unsupported; run command codex")
    profile = response["activePermissionProfile"]
    if profile is not None and (
            not isinstance(profile, dict) or set(profile) - {"id", "extends"}
            or profile.get("id") != _SANDBOX_POLICIES[sandbox["type"]]["profile"]
            or profile.get("extends") is not None):
        raise _LauncherError("Custom Codex permission profiles are unsupported; run command codex")
    thread = response.get("thread")
    try:
        thread_id = str(uuid.UUID(thread["id"])) if isinstance(thread, dict) else None
    except (ValueError, KeyError, TypeError):
        thread_id = None
    if thread_id is None:
        raise _LauncherError("Native Codex returned an invalid thread identifier")
    return thread_id, {"approval_policy": approval, "approvals_reviewer": _REVIEWERS[reviewer],
                       **_sandbox_settings(sandbox)}


def _working_directory(value):
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise _LauncherError("Native Codex returned an unverifiable working directory")
    directory = Path(value).resolve(strict=True)
    if not directory.is_dir():
        raise _LauncherError("The working directory must be a directory")
    return directory


def _preflight(path, args, cwd, check):
    with _CodexSocket(path, private=True) as connection:
        check()
        if args.mode != "new":
            saved = connection.request("thread/read", {"threadId": args.thread_id, "includeTurns": False})
            check()
            thread = saved.get("thread") if isinstance(saved, dict) else None
            if not isinstance(thread, dict) or thread.get("id") != args.thread_id:
                raise _LauncherError("Native Codex could not read the requested saved thread")
            cwd = _working_directory(thread.get("cwd"))
        config = connection.request("config/read", {"cwd": str(cwd), "includeLayers": True})
        check()
        requirements = connection.request("configRequirements/read", {})
        check()
        _validate_configuration(config, requirements)
        parameters = {}
        if args.mode == "new":
            parameters.update(cwd=str(cwd), ephemeral=True)
            if args.sandbox is not None:
                parameters["sandbox"] = args.sandbox
            if args.ask_for_approval is not None:
                parameters["approvalPolicy"] = args.ask_for_approval
            method = "thread/start"
        else:
            parameters["threadId"] = args.thread_id
            method = f"thread/{args.mode}"
        response = connection.request(method, parameters)
        check()
        thread_id, policy = _validate_policy(response)
        if args.mode == "resume" and thread_id != args.thread_id:
            raise _LauncherError("Native Codex resumed a different thread")
        if _working_directory(response.get("cwd")) != cwd:
            raise _LauncherError("Native Codex changed the session working directory")
        return thread_id, policy, cwd


def _launch(args, binary, environment):
    cwd = (args.cd or Path.cwd()).expanduser().resolve(strict=True)
    if not cwd.is_dir():
        raise _LauncherError("The working directory must be a directory")
    codex_root = Path(environment.get("CODEX_HOME") or Path.home() / ".codex").expanduser().resolve()
    settings = {"model": args.model, "sandbox_mode": args.sandbox,
                "approval_policy": args.ask_for_approval, "web_search": "live" if args.search else None}
    with _termination_signals() as check, tempfile.TemporaryDirectory(prefix="lg-codex-", dir="/tmp") as temporary:
        path = Path(temporary).resolve() / "runtime.sock"
        endpoint = "unix://" + str(path)
        server = terminal = None
        try:
            server = subprocess.Popen([binary, *_config_arguments(settings), "app-server", "--listen", endpoint],
                                      cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            check()
            _wait_for_socket(server, path, check)
            path = path.resolve(strict=True)
            endpoint = "unix://" + str(path)
            thread_id, policy, terminal_cwd = _preflight(path, args, cwd, check)
            command = [binary, *_config_arguments({**settings, **policy}), "--remote", endpoint]
            if args.no_alt_screen:
                command.append("--no-alt-screen")
            if args.mode != "new":
                command.extend(["resume", thread_id])
            if args.prompt is not None:
                command.extend(["--", args.prompt])
            with _register_runtime(runtime_registry_root(None), codex_root, path, server.pid):
                check()
                terminal = subprocess.Popen(command, cwd=terminal_cwd, env=environment)
                check()
                while terminal.poll() is None:
                    check()
                    if server.poll() is not None:
                        raise _LauncherError("Native Codex server exited during the terminal session")
                    time.sleep(_POLL_SECONDS)
                check()
                return terminal.returncode if terminal.returncode >= 0 else _SIGNAL_EXIT_OFFSET - terminal.returncode
        except Exception:
            check()
            raise
        finally:
            _ignore_termination_signals()
            _stop_child(terminal)
            _stop_child(server, group=True)


def main(argv=None):
    """Launch the supported native command without altering saved Codex configuration."""
    args = _arguments(argv)
    try:
        environment = _environment()
        binary = _native_binary(environment)
        if args.version:
            return subprocess.run([binary, "--version"], env=environment, check=False).returncode
        return _launch(args, binary, environment)
    except _LauncherInterrupted as error:
        return _SIGNAL_EXIT_OFFSET + error.signum
    except _LauncherError as error:
        print(f"live-gantt-codex: {error}", file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        print("live-gantt-codex: Native Codex server or permission configuration could not be verified. "
              "Check the supported options, or run command codex for diagnostics.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
