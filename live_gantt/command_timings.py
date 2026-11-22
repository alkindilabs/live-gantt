"""Group observed command timings by a fixed vocabulary of operations."""

from collections import defaultdict
from collections.abc import Hashable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import PurePosixPath
import re
import shlex


_MILLISECOND = timedelta(milliseconds=1)
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_PYTHON = re.compile(r"python(?:3(?:\.[0-9]+)?)?")
_FILE_DESCRIPTOR = re.compile(r"(?:[0-9]+|-)(?=$|[ \t<>])")
_BIN_DIRECTORIES = {"/bin", "/usr/bin", "/usr/local/bin", "/opt/homebrew/bin"}
_SCRIPTS = {"scripts/install.sh", "scripts/uninstall.sh"}
_TOOLS = frozenset("""
    cat cd chmod cmp cp curl diff du echo find head jq ls make mkdir mv node pwd
    printf readlink realpath rg rm sed sort stat tail tar tee test touch tr uname uniq wc
""".split())
_OPERATIONS = {
    "cargo": "add bench build check clean clippy doc fmt install metadata run test tree update",
    "codex": "exec review",
    "docker": "build compose inspect logs ps run",
    "forge": "bind build clean coverage create fmt inspect script snapshot test tree",
    "gh": "api auth issue pr release repo run workflow",
    "gh issue": "comment create edit list status view",
    "gh pr": "checks checkout comment create diff edit list merge review status view",
    "gh run": "cancel download list rerun view watch",
    "git": (
        "add apply blame branch cat-file checkout cherry-pick clean clone commit config diff fetch "
        "grep log ls-files ls-remote merge merge-base mv pull push rebase remote reset restore rev-parse "
        "rm show stash status switch tag worktree"
    ),
    "go": "build clean fmt list mod run test vet",
    "kontrol": "build clean foundry-up init load-state prove show version",
    "npm": "audit ci exec install list run test uninstall update view",
    "npm run": "build check dev format lint test typecheck",
    "pnpm": "add build dev exec install lint run test typecheck",
    "python": "-m",
    "python -m": "compileall http.server pip pytest unittest",
    "uv": "build lock pip run sync tool",
}
_OPTIONS_WITH_VALUE = {
    "git": {"-C", "-c", "--git-dir", "--namespace", "--work-tree"},
    "npm": {"--prefix", "--workspace", "-w"},
    "pnpm": {"--dir", "--filter", "-C", "-F"},
}
_OPTIONS_WITHOUT_VALUE = {
    "git": {"--no-optional-locks", "--no-pager", "--paginate", "-p"},
    "python": {"-B", "-I", "-O", "-OO", "-q", "-s", "-S", "-u", "-v"},
}


def _command_after_shell_prefix(command: str) -> str | None:
    quote = ""
    escaped = False
    separator = None
    for index, char in enumerate(command):
        if escaped:
            if char in "\r\n":
                return None
            escaped = False
            continue
        if quote == "'":
            if char == quote:
                quote = ""
            continue
        if char == "\\":
            escaped = True
            continue
        if char == "`" or command.startswith("$(", index):
            return None
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == "&" and index > 0 and command[index - 1] in "<>" and _FILE_DESCRIPTOR.match(command, index + 1):
            continue
        elif char == "&" and separator == index - 1:
            continue
        elif char == "&" and separator is None and command.startswith("&&", index):
            separator = index
        elif char in ";&|()\r\n":
            return None
    if separator is None:
        return command
    prefix = shlex.split(command[:separator])
    if prefix[:2] == ["cd", "--"]:
        prefix.pop(1)
    if len(prefix) == 2 and prefix[0] == "cd":
        return command[separator + len("&&"):]
    return None


def _invocation(words: list[str]) -> list[str]:
    index = 0
    while index < len(words) and _ASSIGNMENT.match(words[index]):
        index += 1
    if index < len(words) and words[index] in {"env", "/usr/bin/env"}:
        index += 1
        while index < len(words) and _ASSIGNMENT.match(words[index]):
            index += 1
    return words[index:]


def _tool_name(word: str) -> str | None:
    path = PurePosixPath(word)
    if str(path) in _SCRIPTS:
        return path.name
    if "/" in word and str(path.parent) not in _BIN_DIRECTORIES:
        return None
    if _PYTHON.fullmatch(path.name):
        return "python"
    return path.name if path.name in _TOOLS or path.name in _OPERATIONS else None


def _operation_label(words: list[str]) -> str | None:
    if not words:
        return None
    tool = _tool_name(words[0])
    if tool is None:
        return None
    if tool == "find" and {"-exec", "-execdir", "-ok", "-okdir"}.intersection(words):
        return None
    index = 1
    with_value = _OPTIONS_WITH_VALUE.get(tool, ())
    without_value = _OPTIONS_WITHOUT_VALUE.get(tool, ())
    while index < len(words):
        word = words[index]
        if word in with_value:
            index += 2
        elif word.partition("=")[0] in with_value or word in without_value:
            index += 1
        else:
            break
    label = tool
    for word in words[index:]:
        if word not in _OPERATIONS.get(label, "").split():
            break
        label = f"{label} {word}"
    return "python" if label == "python -m" else label


def _command_label(command: str) -> str | None:
    if not isinstance(command, str):
        return None
    try:
        invocation = _command_after_shell_prefix(command.strip())
        if invocation is None:
            return None
        lexer = shlex.shlex(invocation, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        return _operation_label(_invocation(list(lexer)))
    except ValueError:
        return None


@dataclass
class _Timing:
    calls: int = 0
    total_ms: int = 0
    longest_ms: int = 0

    def add(self, calls: int, total_ms: int, longest_ms: int) -> None:
        self.calls += calls
        self.total_ms += total_ms
        self.longest_ms = max(self.longest_ms, longest_ms)


class CommandTimings:
    """Incrementally aggregate one transcript's synchronous command calls by label."""

    def __init__(self) -> None:
        self._ids: set[Hashable] = set()
        self._pending: dict[Hashable, tuple[str, datetime]] = {}
        self._operations: dict[str, _Timing] = defaultdict(_Timing)
        self._unmeasured = 0

    def start(self, call_id: Hashable, command: str, at: datetime, *, background: bool = False) -> None:
        """Record one invocation and retain only its recognized operation and start time."""
        if call_id in self._ids:
            return
        self.unmeasured(call_id)
        label = _command_label(command) if not background else None
        if label is not None and isinstance(at, datetime) and at.utcoffset() is not None:
            self._pending[call_id] = (label, at)

    def awaiting(self, call_id: Hashable) -> bool:
        """Return whether a result may provide a timing for this call."""
        return call_id in self._pending

    def finish(self, call_id: Hashable, at: datetime, *, background: bool = False) -> None:
        """Close a previously observed call once, recording a valid synchronous duration."""
        pending = self._pending.pop(call_id, None)
        if pending is None or background or not isinstance(at, datetime) or at.utcoffset() is None:
            return
        label, started = pending
        if at < started:
            return
        elapsed_ms = (at - started) // _MILLISECOND
        self._operations[label].add(1, elapsed_ms, elapsed_ms)
        self._unmeasured -= 1

    def unmeasured(self, call_id: Hashable) -> None:
        """Count an opaque invocation once and discard any incomplete measurement for it."""
        if call_id not in self._ids:
            self._ids.add(call_id)
            self._unmeasured += 1
        self._pending.pop(call_id, None)


def summarize_command_timings(transcripts: Iterable[CommandTimings]) -> dict:
    """Merge transcript counts, ordered by descending total duration and then label."""
    totals = defaultdict(_Timing)
    unmeasured = 0
    for transcript in transcripts:
        unmeasured += transcript._unmeasured
        for label, timing in transcript._operations.items():
            totals[label].add(timing.calls, timing.total_ms, timing.longest_ms)
    return {
        "operations": [
            {"label": label, "calls": timing.calls, "totalMs": timing.total_ms, "longestMs": timing.longest_ms}
            for label, timing in sorted(totals.items(), key=lambda item: (-item[1].total_ms, item[0]))
        ],
        "unmeasured": unmeasured,
    }
