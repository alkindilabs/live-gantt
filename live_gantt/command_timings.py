"""Group observed command timings by a fixed vocabulary of operations."""

from collections import defaultdict
from collections.abc import Hashable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from heapq import nsmallest
from pathlib import PurePosixPath
import re
import shlex


COMMAND_COMPLETED = "completed"
COMMAND_FAILED = "failed"
COMMAND_DENIED = "denied"

_MILLISECOND = timedelta(milliseconds=1)
_SLOWEST_CALL_COUNT = 5
_REPORTED_OUTCOMES = (COMMAND_COMPLETED, COMMAND_FAILED, COMMAND_DENIED)
_REVIEW_REJECTED = "reviewRejected"
_UNKNOWN_OUTCOME = "unknown"
_OUTCOMES = (*_REPORTED_OUTCOMES, _REVIEW_REJECTED, _UNKNOWN_OUTCOME)
_GIT_COMMIT_SUBCOMMAND = "commit"
_GIT_COMMIT_LABEL = f"git {_GIT_COMMIT_SUBCOMMAND}"
_REVIEW_PROVIDERS = ("claude", "codex")
_REVIEW_PROVIDER_PATTERN = "|".join(_REVIEW_PROVIDERS)
_REVIEW_LINE = re.compile(
    r"^principles-gate: (?:"
    rf"reviewing staged changes with (?P<started>{_REVIEW_PROVIDER_PATTERN})|"
    rf"(?P<rejected>{_REVIEW_PROVIDER_PATTERN}) review did not pass; principles could not be verified)\r?$",
    re.MULTILINE,
)
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
        f"add apply blame branch cat-file checkout cherry-pick clean clone {_GIT_COMMIT_SUBCOMMAND} config diff fetch "
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


@dataclass(frozen=True)
class _Call:
    elapsed_ms: int
    outcome: str
    review_provider: str | None

    def sort_key(self) -> tuple[int, str, str]:
        return -self.elapsed_ms, self.outcome, self.review_provider or ""


def _slowest_calls(calls: Iterable[_Call]) -> list[_Call]:
    return nsmallest(_SLOWEST_CALL_COUNT, calls, key=_Call.sort_key)


def _call_outcome(label: str, outcome: str | None, output: Iterable[str]) -> tuple[str, str | None]:
    if outcome is None or outcome not in _REPORTED_OUTCOMES:
        outcome = _UNKNOWN_OUTCOME
    if label != _GIT_COMMIT_LABEL:
        return outcome, None

    started = set()
    rejected = set()
    for fragment in output:
        for match in _REVIEW_LINE.finditer(fragment):
            if match["started"]:
                started.add(match["started"])
            else:
                rejected.add(match["rejected"])
    conflicting_result = rejected and outcome not in (COMMAND_FAILED, _UNKNOWN_OUTCOME)
    if len(started) != 1 or rejected - started or conflicting_result:
        return outcome, None
    if rejected and outcome == COMMAND_FAILED:
        outcome = _REVIEW_REJECTED
    return outcome, next(iter(started))


@dataclass
class _Duration:
    calls: int = 0
    total_ms: int = 0

    def add(self, calls: int, total_ms: int) -> None:
        self.calls += calls
        self.total_ms += total_ms


@dataclass
class _Timing:
    groups: dict[tuple[str, str | None], _Duration] = field(default_factory=lambda: defaultdict(_Duration))
    slowest_calls: list[_Call] = field(default_factory=list)
    process_observed: _Duration = field(default_factory=_Duration)
    process_ms: int = 0

    def add(self, call: _Call, process_ms: int | None) -> None:
        self.groups[call.outcome, call.review_provider].add(1, call.elapsed_ms)
        self.slowest_calls = _slowest_calls((*self.slowest_calls, call))
        if type(process_ms) is int and 0 <= process_ms <= call.elapsed_ms:
            self.process_observed.add(1, call.elapsed_ms)
            self.process_ms += process_ms


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

    def finish(self, call_id: Hashable, at: datetime, *, background: bool = False,
               outcome: str | None = None, output: Iterable[str] = (), process_ms: int | None = None) -> None:
        """Close a call once, recording its synchronous duration and any validated result evidence."""
        pending = self._pending.pop(call_id, None)
        if pending is None or background or not isinstance(at, datetime) or at.utcoffset() is None:
            return
        label, started = pending
        if at < started:
            return
        elapsed_ms = (at - started) // _MILLISECOND
        outcome, provider = _call_outcome(label, outcome, output)
        self._operations[label].add(_Call(elapsed_ms, outcome, provider), process_ms)
        self._unmeasured -= 1

    def unmeasured(self, call_id: Hashable) -> None:
        """Count an opaque invocation once and discard any incomplete measurement for it."""
        if call_id not in self._ids:
            self._ids.add(call_id)
            self._unmeasured += 1
        self._pending.pop(call_id, None)


def _leader_detail(label: str, timings: list[_Timing]) -> dict:
    outcomes = defaultdict(_Duration)
    reviews = defaultdict(_Duration)
    rejections = defaultdict(_Duration)
    process_observed = _Duration()
    process_ms = 0
    for timing in timings:
        for (outcome, provider), observed in timing.groups.items():
            outcomes[outcome].add(observed.calls, observed.total_ms)
            reviews[provider].add(observed.calls, observed.total_ms)
            if outcome == _REVIEW_REJECTED:
                rejections[provider].add(observed.calls, observed.total_ms)
        process_observed.add(timing.process_observed.calls, timing.process_observed.total_ms)
        process_ms += timing.process_ms

    slowest = _slowest_calls(call for timing in timings for call in timing.slowest_calls)
    has_reviews = any(provider in reviews for provider in _REVIEW_PROVIDERS)
    return {
        "label": label,
        "slowestCalls": [
            {"elapsedMs": call.elapsed_ms, "outcome": call.outcome, "reviewProvider": call.review_provider}
            for call in slowest
        ],
        "outcomes": [
            {"outcome": outcome, "calls": outcomes[outcome].calls, "totalMs": outcomes[outcome].total_ms}
            for outcome in _OUTCOMES if outcome in outcomes
        ],
        "reviews": [
            {"provider": provider, "calls": reviews[provider].calls, "totalMs": reviews[provider].total_ms,
             "rejectedCalls": rejections[provider].calls, "rejectedMs": rejections[provider].total_ms}
            for provider in (*_REVIEW_PROVIDERS, None) if provider in reviews
        ] if has_reviews else [],
        "processTiming": {
            "calls": process_observed.calls, "observedMs": process_observed.total_ms, "processMs": process_ms,
        } if process_observed.calls else None,
    }


def summarize_command_timings(transcripts: Iterable[CommandTimings]) -> dict:
    """Merge transcript timings, ordered by descending total duration and then label."""
    by_label = defaultdict(list)
    unmeasured = 0
    for transcript in transcripts:
        unmeasured += transcript._unmeasured
        for label, timing in transcript._operations.items():
            by_label[label].append(timing)
    operations = []
    for label, timings in by_label.items():
        groups = [group for timing in timings for group in timing.groups.values()]
        operations.append({
            "label": label,
            "calls": sum(group.calls for group in groups),
            "totalMs": sum(group.total_ms for group in groups),
            "longestMs": max(timing.slowest_calls[0].elapsed_ms for timing in timings),
        })
    operations.sort(key=lambda item: (-item["totalMs"], item["label"]))
    leader = operations[0]["label"] if operations else None
    return {
        "operations": operations,
        "unmeasured": unmeasured,
        "leaderDetail": _leader_detail(leader, by_label[leader]) if leader is not None else None,
    }
