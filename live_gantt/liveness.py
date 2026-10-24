"""Read native session ownership without changing providers or their locks."""

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
from itertools import islice
import json
import math
import os
from pathlib import Path
import platform
import selectors
import signal
import stat
import struct
import subprocess
import time

from .sources import LIST_ID


CACHE_SECONDS = 2.0
MAX_CACHE_ENTRIES = 256
MAX_REGISTRY_FILES = 256
MAX_REGISTRY_BYTES = 16 * 1024
MAX_COMMAND_BYTES = 64 * 1024
INSPECTION_SECONDS = 1.0
PROCESS_EXIT_SECONDS = 0.15
READ_BYTES = 8192
DARWIN_FLOCK = struct.Struct("@qqihh")
PS = "/bin/ps"
LSOF = "/usr/sbin/lsof"
UNKNOWN_DETAIL = "Session liveness could not be verified."
ALIVE_DETAIL = "Native session ownership is verified; this does not prove responsiveness or progress."
EXITED_DETAIL = "The process previously associated with this session has exited or its identity changed."
MILLISECONDS_PER_SECOND = 1000
CLAUDE_PHASES = {"busy": "working", "shell": "working", "waiting": "waiting", "idle": "idle"}
CLAUDE_WAITING_DETAILS = {
    "permission prompt": "Permission is required.",
    "input needed": "Input is required.",
    "dialog open": "A dialog needs attention.",
    "worker request": "A worker request needs attention.",
    "sandbox request": "A sandbox request needs attention.",
}


@dataclass(frozen=True)
class _Binding:
    pid: int
    started: str


@dataclass
class _Entry:
    checked: float
    result: dict
    binding: _Binding | None
    runtime: dict


def _runtime(phase="unknown", last_activity=None, detail=None):
    return {"phase": phase, "lastActivityAt": last_activity, "detail": detail}


def _claude_runtime(record, now):
    status = record.get("status")
    phase = CLAUDE_PHASES.get(status, "unknown") if isinstance(status, str) else "unknown"
    if phase == "unknown":
        return _runtime()
    changed = record.get("statusUpdatedAt")
    last_activity = None
    if type(changed) in (int, float) and math.isfinite(changed) and 0 < changed <= now.timestamp() * MILLISECONDS_PER_SECOND:
        try:
            last_activity = datetime.fromtimestamp(changed / MILLISECONDS_PER_SECOND, timezone.utc).isoformat().replace("+00:00", "Z")
        except (OverflowError, OSError, ValueError):
            pass
    reason = record.get("waitingFor")
    detail = None
    if phase == "waiting":
        detail = CLAUDE_WAITING_DETAILS.get(reason) if isinstance(reason, str) else None
        detail = detail or "Claude is waiting for attention."
    return _runtime(phase, last_activity, detail)


def _result(status, now, evidence=None, detail=UNKNOWN_DETAIL):
    return {"status": status, "checkedAt": now.isoformat(), "evidence": evidence, "detail": detail}


def unknown_liveness(now, detail=UNKNOWN_DETAIL):
    """Return a liveness observation without claiming process ownership."""
    return _result("unknown", now, detail=detail)


def _terminate(process):
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=PROCESS_EXIT_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=PROCESS_EXIT_SECONDS)


def run_command(command, *, environment, deadline):
    """Run a process-inspection command with bounded output and a monotonic deadline.

    The returned CompletedProcess contains byte stdout; stderr is discarded. Only
    this invocation's process group is cleaned up on timeout or excess output.
    """
    if deadline <= time.monotonic():
        raise subprocess.TimeoutExpired(command, INSPECTION_SECONDS)
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, env=environment, start_new_session=True)
    output = bytearray()
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, INSPECTION_SECONDS)
                for key, _ in selector.select(remaining):
                    block = os.read(key.fd, min(READ_BYTES, MAX_COMMAND_BYTES - len(output) + 1))
                    if not block:
                        selector.unregister(key.fileobj)
                        continue
                    output.extend(block)
                    if len(output) > MAX_COMMAND_BYTES:
                        raise ValueError("Process inspection output exceeded its limit")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(command, INSPECTION_SECONDS)
        code = process.wait(timeout=remaining)
        return subprocess.CompletedProcess(command, code, stdout=bytes(output))
    finally:
        _terminate(process)
        process.stdout.close()


def _read_document(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("Registry entry is not a regular file")
        contents = stream.read(MAX_REGISTRY_BYTES + 1)
    if len(contents) > MAX_REGISTRY_BYTES:
        raise ValueError("Registry entry exceeded its limit")
    value = json.loads(contents)
    if not isinstance(value, dict):
        raise ValueError("Registry entry is not an object")
    return value


def _registry_record(root, source_id):
    directory = root / "sessions"
    try:
        paths = list(islice(directory.iterdir(), MAX_REGISTRY_FILES + 1))
    except FileNotFoundError:
        return None
    if len(paths) > MAX_REGISTRY_FILES:
        raise ValueError("Registry scan exceeded its limit")
    matching = []
    for path in paths:
        if path.suffix != ".json":
            continue
        try:
            record = _read_document(path)
        except (FileNotFoundError, ValueError, UnicodeError):
            continue
        if record.get("sessionId") == source_id:
            pid = record.get("pid")
            started = record.get("procStart")
            if (type(pid) is not int or pid <= 0 or path.stem != str(pid)
                    or record.get("pidDomain") != "darwin" or not isinstance(started, str)
                    or not started.strip()):
                raise ValueError("Unverifiable session registry identity")
            matching.append((path, _Binding(pid, started.strip())))
    if len(matching) > 1:
        raise ValueError("Ambiguous session registry identity")
    return matching[0] if matching else None


def _writer_lock(path):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Writer lock is not a regular file")
        request = DARWIN_FLOCK.pack(0, 0, 0, fcntl.F_WRLCK, os.SEEK_SET)
        response = fcntl.fcntl(stream.fileno(), fcntl.F_GETLK, request)
        start, length, _, kind, _ = DARWIN_FLOCK.unpack(response)
    if kind not in (fcntl.F_UNLCK, fcntl.F_WRLCK) or start != 0 or length != 0:
        raise ValueError("Writer ownership is not an exclusive whole-file lock")
    return (info.st_dev, info.st_ino), kind == fcntl.F_WRLCK


def _opener_pids(output, identity):
    pids = set()
    pid = None
    fields = {}

    def finish():
        if (pid is not None and fields.get("f", "").isdigit()
                and int(fields.get("i", "-1")) == identity[1]
                and int(fields.get("D", "-1"), 16) == identity[0]):
            pids.add(pid)

    for field in output.replace("\n", "\0").split("\0"):
        if not field:
            continue
        kind, value = field[0], field[1:]
        if kind == "p":
            finish()
            pid = int(value)
            fields = {}
        elif kind == "f":
            finish()
            fields = {"f": value}
        elif kind in ("i", "D"):
            fields[kind] = value
    finish()
    return pids


class LivenessStore:
    """Cache verified process bindings separately from transcript activity.

    state(provider, source_id, root, now) returns alive, exited or unknown. An
    inspection failure never proves exit. Darwin native metadata is supported;
    other platforms remain explicitly unknown until their bindings are verified.
    command is an external-process adapter returning CompletedProcess stdout as
    bytes or text, called with environment and an absolute monotonic deadline.
    """

    def __init__(self, *, command=None, clock=None):
        self._command = run_command if command is None else command
        self._clock = time.monotonic if clock is None else clock
        self._entries = OrderedDict()

    def _output(self, command, deadline):
        if time.monotonic() >= deadline:
            raise subprocess.TimeoutExpired(command, INSPECTION_SECONDS)
        environment = dict(os.environ)
        environment.update(LC_ALL="C", TZ="UTC")
        result = self._command(command, environment=environment, deadline=deadline)
        output = result.stdout
        if not isinstance(output, (bytes, str)) or len(output) > MAX_COMMAND_BYTES:
            raise ValueError("Unverifiable process inspection output")
        return result.returncode, output.decode("utf-8", errors="strict") if isinstance(output, bytes) else output

    def _process_start(self, pid, deadline):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return None
        code, output = self._output([PS, "-o", "lstart=", "-p", str(pid)], deadline)
        rows = output.strip().splitlines()
        if code != 0 or len(rows) != 1 or not rows[0].strip():
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return None
            raise ValueError("Process identity is unavailable")
        return rows[0].strip()

    def _previous_exit(self, previous, now, deadline):
        if previous is not None and self._process_start(previous.pid, deadline) != previous.started:
            return _result("exited", now, detail=EXITED_DETAIL)
        return unknown_liveness(now)

    def _claude(self, root, source_id, previous, now, deadline):
        found = _registry_record(root, source_id)
        if found is None:
            return self._previous_exit(previous, now, deadline), previous, _runtime()
        path, binding = found
        if self._process_start(binding.pid, deadline) != binding.started:
            return _result("exited", now, "native_registry_process", EXITED_DETAIL), binding, _runtime()
        record = _read_document(path)
        if (record.get("sessionId") != source_id or record.get("pid") != binding.pid
                or record.get("procStart") != binding.started or record.get("pidDomain") != "darwin"
                or self._process_start(binding.pid, deadline) != binding.started):
            return unknown_liveness(now), previous, _runtime()
        return _result("alive", now, "native_registry_process", ALIVE_DETAIL), binding, _claude_runtime(record, now)

    def _codex(self, root, source_id, previous, now, deadline):
        path = root / "thread-writer-locks" / (source_id + ".lock")
        before = _writer_lock(path)
        if before is None or not before[1]:
            return self._previous_exit(previous, now, deadline), previous
        code, output = self._output([LSOF, "-nP", "-F0pfiD", "--", str(path)], deadline)
        if code != 0:
            return unknown_liveness(now), previous
        owners = _opener_pids(output, before[0])
        if len(owners) != 1:
            return unknown_liveness(now), previous
        pid = next(iter(owners))
        if pid <= 0:
            return unknown_liveness(now), previous
        started = self._process_start(pid, deadline)
        if started is None:
            return unknown_liveness(now), previous
        after = _writer_lock(path)
        if after != before or self._process_start(pid, deadline) != started:
            return unknown_liveness(now), previous
        return _result("alive", now, "session_writer_lock", ALIVE_DETAIL), _Binding(pid, started)

    def state(self, provider, source_id, root, now):
        """Observe one exact local session without exposing process IDs or paths."""
        return dict(self._observation(provider, source_id, root, now).result)

    def runtime(self, provider, source_id, root, now):
        """Return phase, native activity time and safe detail from the cached ownership observation.

        Unverified ownership or an unsupported native activity format yields unknown.
        The returned mapping is independent of the cache and contains no process identity.
        """
        return dict(self._observation(provider, source_id, root, now).runtime)

    def _observation(self, provider, source_id, root, now):
        if (provider not in ("claude", "codex") or not isinstance(source_id, str)
                or not LIST_ID.fullmatch(source_id) or root is None):
            return _Entry(0, unknown_liveness(now), None, _runtime())
        try:
            directory = Path(root).expanduser().resolve()
        except (OSError, ValueError, RuntimeError):
            return _Entry(0, unknown_liveness(now), None, _runtime())
        key = provider, source_id, str(directory)
        entry = self._entries.get(key)
        checked = self._clock()
        if entry is not None and checked - entry.checked < CACHE_SECONDS:
            self._entries.move_to_end(key)
            return entry
        previous = entry.binding if entry else None
        result, binding = unknown_liveness(now), previous
        runtime = _runtime()
        try:
            if platform.system() == "Darwin":
                deadline = time.monotonic() + INSPECTION_SECONDS
                if provider == "claude":
                    result, binding, runtime = self._claude(directory, source_id, previous, now, deadline)
                else:
                    result, binding = self._codex(directory, source_id, previous, now, deadline)
            else:
                result = unknown_liveness(now, "Native process ownership is not verified on this platform.")
        except Exception:
            result = unknown_liveness(now)
            runtime = _runtime()
        self._entries[key] = _Entry(checked, result, binding, runtime)
        self._entries.move_to_end(key)
        while len(self._entries) > MAX_CACHE_ENTRIES:
            self._entries.popitem(last=False)
        return self._entries[key]
