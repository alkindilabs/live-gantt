"""Read Claude task lists and incremental transcript history without writing sources."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import threading
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .freshness import session_active
from .usage import empty_token_usage


UTC = timezone.utc
SILENT_AFTER = timedelta(minutes=30)
RECENT_SESSION_WINDOW = timedelta(days=14)
IDENTITY_SCAN_BYTES = 1024 * 1024
IDENTITY_SCAN_LINES = 128
IDENTITY_TAIL_BYTES = 256 * 1024
APPEND_CHECK_BYTES = 256
PROMPT_TITLE_CHARACTERS = 120
TASK_STATUSES = frozenset({"pending", "in_progress", "completed"})
LIVE_AGENT_STATUSES = frozenset({"running", "in_progress", "pending"})
TIMESTAMP_PATTERN = re.compile(rb'"timestamp"\s*:\s*"([^"\\]+)"')
CREATION_PATTERN = re.compile(r"^Task #(\S+) created successfully(?:\b|:)")
NOTIFICATION_PATTERN = re.compile(r"<task-notification>(.*?)</task-notification>", re.DOTALL)
RESULT_LINE_PATTERN = re.compile(rb'"type"\s*:\s*"result"')
AGENT_ID_PATTERN = re.compile(r'(?:agentId|agent_id)\s*[":=]+\s*["\s]*([\w-]+)')
TASK_ID_PATTERN = re.compile(r'(?:task_id|taskId)\s*[":=]+\s*["\s]*([\w-]+)')
RELEVANT_TOKENS = (b'"tool_use"', b'"tool_result"', b"<task-notification>")
TITLE_RECORD_TOKEN = b'"custom-title"'
TRANSCRIPT_TOKENS = RELEVANT_TOKENS + (b'"timestamp"', b'"cwd"', b'"user"', b'"usage"', TITLE_RECORD_TOKEN)
CWD_PATTERN = re.compile(rb'"cwd"\s*:\s*("(?:[^"\\]|\\.)*")')
ENTRYPOINT_PATTERN = re.compile(rb'"entrypoint"\s*:\s*("(?:[^"\\]|\\.)*")')
HEADLESS_ENTRYPOINT = "sdk-cli"
LIST_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
LEADING_WRAPPER = re.compile(r"\s*<([A-Za-z][\w-]*)(?:\s[^>]*)?>.*?</\1\s*>", re.DOTALL)
LONE_TAG_LINE = re.compile(r"\s*</?[A-Za-z][\w-]*(?:\s[^>]*)?/?>\s*\Z")
ELLIPSIS = "\u2026"
RUNTIME_FAILURE_DETAILS = {
    "error_during_execution": "The turn failed during execution.",
    "error_max_turns": "The turn reached its turn limit.",
    "error_max_budget_usd": "The turn reached its spending limit.",
    "error_max_structured_output_retries": "The turn could not produce the required output format.",
}
RUNTIME_SESSION_PHASES = {"running": "working", "requires_action": "waiting", "idle": "idle"}
INTERRUPTION_TEXTS = frozenset({"[Request interrupted by user]", "[Request interrupted by user for tool use]"})


def default_root() -> Path:
    """Return the configured Claude data root, expanding the user's home directory."""
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser()


def parse_time(value: Any) -> datetime | None:
    """Parse an ISO 8601 timestamp as UTC, treating an omitted offset as UTC."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    except (ValueError, OverflowError):
        return None


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z") if value else None


def _stat(path: Path, *, strict: bool = False) -> os.stat_result | None:
    try:
        return path.stat()
    except OSError as error:
        if strict and not isinstance(error, FileNotFoundError):
            raise
        return None
    except ValueError:
        return None


def _regular_stat(path: Path, *, strict: bool = False) -> os.stat_result | None:
    file_stat = _stat(path, strict=strict)
    return file_stat if file_stat and stat.S_ISREG(file_stat.st_mode) else None


def _modified(path: Path | None) -> str | None:
    file_stat = _regular_stat(path) if path else None
    return _iso(datetime.fromtimestamp(file_stat.st_mtime, UTC)) if file_stat else None


def _process_alive(pid: int) -> bool:
    """Probe a process without signalling it; a permission refusal means it exists.

    Errors other than an absent process count as alive, so a probe never reports a false exit."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (OSError, OverflowError, ValueError):
        return True
    return True


def _children(path: Path, *, strict: bool = False) -> list[Path]:
    try:
        return list(path.iterdir())
    except OSError as error:
        if strict and not isinstance(error, FileNotFoundError):
            raise
        return []


def _finite(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _safe_json(value: Any, warnings: list[str]) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        warning = "Non-finite JSON values were replaced with null"
        if warning not in warnings:
            warnings.append(warning)
        return None
    if isinstance(value, list):
        return [_safe_json(item, warnings) for item in value]
    if isinstance(value, dict):
        return {key: _safe_json(item, warnings) for key, item in value.items()}
    return value


def _read_json(path: Path, warnings: list[str]) -> dict[str, Any]:
    if _regular_stat(path) is None:
        warnings.append(f"Could not read {path.name} as a regular file")
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        warnings.append(f"Could not read {path.name} as JSON")
        return {}
    if not isinstance(value, dict):
        warnings.append(f"{path.name} must contain a JSON object")
        return {}
    return _safe_json(value, warnings)


def _texts(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [item["text"] for item in content
                if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)]
    return []


def _queued_texts(record: dict[str, Any]) -> list[str]:
    """Return the text of a command queued for the parent: a queue operation's content or a mid-turn attachment's prompt.

    Task notifications arrive this way when the parent is busy, instead of as a user message."""
    if record.get("type") == "queue-operation":
        return _texts(record.get("content"))
    attachment = record.get("attachment")
    if record.get("type") == "attachment" and isinstance(attachment, dict) and attachment.get("type") == "queued_command":
        return _texts(attachment.get("prompt"))
    return []


def _prompt_title(text: str) -> str:
    """Return the first line a person typed, after markup elements the harness put before it and lines holding only a tag."""
    while wrapper := LEADING_WRAPPER.match(text):
        text = text[wrapper.end():]
    line = next((line.strip() for line in text.splitlines()
                 if line.strip() and not LONE_TAG_LINE.match(line)), "")
    if len(line) <= PROMPT_TITLE_CHARACTERS:
        return line
    return line[:PROMPT_TITLE_CHARACTERS - len(ELLIPSIS)] + ELLIPSIS


def _json_string(encoded: bytes) -> str | None:
    try:
        value = json.loads(encoded)
    except (ValueError, UnicodeError):
        return None
    return value if isinstance(value, str) else None


@dataclass
class _Naming:
    """What a transcript says about its session: the prompt title, latest custom title and latest working directory."""

    prompt: str = ""
    custom_title: str = ""
    project: str = ""
    entrypoint: str = ""

    def read(self, line: bytes) -> None:
        cwd = CWD_PATTERN.search(line)
        if cwd and (project := _json_string(cwd.group(1))):
            self.project = project
        if not self.entrypoint and (entrypoint := ENTRYPOINT_PATTERN.search(line)):
            self.entrypoint = _json_string(entrypoint.group(1)) or ""
        if TITLE_RECORD_TOKEN not in line and (self.prompt or b'"user"' not in line):
            return
        try:
            record = json.loads(line)
        except (ValueError, UnicodeError):
            return
        if not isinstance(record, dict):
            return
        if record.get("type") == "custom-title":
            custom_title = record.get("customTitle")
            if isinstance(custom_title, str) and custom_title.strip():
                self.custom_title = custom_title.strip()
        elif not self.prompt and record.get("type") == "user" and record.get("isMeta") is not True:
            message = record.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            self.prompt = next((title for text in _texts(content) if (title := _prompt_title(text))), "")

    def followed_by(self, later: _Naming) -> _Naming:
        """Combine an earlier reading with a later one: first prompt, latest title and directory."""
        return _Naming(self.prompt or later.prompt, later.custom_title or self.custom_title,
                       later.project or self.project, self.entrypoint or later.entrypoint)


def _agents_directory(transcript: Path) -> Path:
    return transcript.with_suffix("") / "subagents"


def _agent_ids(directory: Path) -> set[str]:
    agent_ids: set[str] = set()
    for path in _children(directory):
        if path.name.startswith("agent-") and path.name.endswith((".jsonl", ".meta.json")):
            suffix = ".meta.json" if path.name.endswith(".meta.json") else ".jsonl"
            agent_ids.add(path.name[len("agent-"):-len(suffix)])
    return agent_ids


def _workflow_runs(agents_directory: Path) -> list[Path]:
    """Return the run directories of a session's workflows, each holding its agents' transcripts and a journal."""
    runs = [path for path in _children(agents_directory / "workflows")
            if (file_stat := _stat(path)) and stat.S_ISDIR(file_stat.st_mode)]
    return sorted(runs, key=lambda path: path.name)


def _agent_count(agents_directory: Path) -> int:
    return len(_agent_ids(agents_directory)) + sum(len(_agent_ids(run)) for run in _workflow_runs(agents_directory))


def _line_is_complete(line: bytes, tokens: tuple[bytes, ...]) -> bool:
    if line.endswith(b"\n"):
        return True
    if not any(token in line for token in tokens):
        return False
    try:
        json.loads(line)
        return True
    except (ValueError, UnicodeError):
        return False


def append_checkpoint(stream: Any, offset: int) -> bytes:
    """Read the bounded consumed tail, leaving the stream positioned at offset."""
    start = max(0, offset - APPEND_CHECK_BYTES)
    stream.seek(start)
    return stream.read(offset - start)


def _task_id(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return None


@dataclass
class _TaskHistory:
    created: datetime | None = None
    started: datetime | None = None
    ended: datetime | None = None
    events: list[dict[str, Any]] = field(default_factory=list)

    def _record(self, timestamp: datetime, event: str, value: float | None = None) -> None:
        entry: dict[str, Any] = {"time": _iso(timestamp), "event": event}
        if value is not None:
            entry["value"] = value
        self.events.append(entry)


@dataclass
class _WorkflowLaunch:
    """What the parent's Workflow tool result says about a run: its name, summary, background task and launch time."""

    name: str
    summary: str
    task_id: str
    launched: datetime


@dataclass
class _Transcript:
    identity: tuple[int, int] | None = None
    offset: int = 0
    size: int = 0
    modified: int = 0
    checkpoint: bytes = b""
    first: datetime | None = None
    activity: str = ""
    handed_back: datetime | None = None
    runtime_phase: str = "unknown"
    last_activity: datetime | None = None
    last_execution: datetime | None = None
    runtime_detail: str | None = None
    runtime_messages: dict[str, bytes] = field(default_factory=dict)
    runtime_input_ids: set[str] = field(default_factory=set)
    naming: _Naming = field(default_factory=_Naming)
    histories: dict[str, _TaskHistory] = field(default_factory=dict)
    workflows: dict[str, _WorkflowLaunch] = field(default_factory=dict)
    notifications: dict[str, tuple[str, datetime]] = field(default_factory=dict)
    agent_aliases: dict[str, str] = field(default_factory=dict)
    response_usage: dict[str, dict[str, int]] = field(default_factory=dict)
    task_updates: dict[str, tuple[str, list[dict[str, Any]]]] = field(default_factory=dict)

    def _consume(self, line: bytes) -> None:
        timestamp_match = TIMESTAMP_PATTERN.search(line)
        timestamp = parse_time(timestamp_match.group(1).decode("ascii", errors="ignore")) if timestamp_match else None
        self.naming.read(line)
        if b'"usage"' in line:
            self._read_usage(line)
        if timestamp is None:
            return
        try:
            record = json.loads(line)
        except (ValueError, UnicodeError):
            return
        if not isinstance(record, dict):
            return
        self.first = self.first or timestamp
        self._read_runtime(record)
        if not any(token in line for token in RELEVANT_TOKENS):
            return
        if record.get("toolEndsTurn") is True:
            self.handed_back = timestamp
        self._read_workflow_launch(record.get("toolUseResult"), timestamp)
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        for text in _texts(content) + _queued_texts(record):
            self._read_notifications(text, timestamp)
        if not isinstance(content, list):
            return
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "tool_use":
                name = item.get("name")
                inputs = item.get("input")
                inputs = inputs if isinstance(inputs, dict) else {}
                description = inputs.get("description")
                self.activity = description if isinstance(description, str) else ""
                if name == "TaskUpdate":
                    self._read_task_update(inputs, timestamp, item.get("id"))
            elif item.get("type") == "tool_result":
                self._finish_task_update(item)
                if item.get("is_error") is True:
                    continue
                for text in _texts(item.get("content")):
                    creation = CREATION_PATTERN.match(text)
                    if creation:
                        history = self.histories.setdefault(creation.group(1), _TaskHistory())
                        history.created = history.created or timestamp
                        history._record(timestamp, "created")
                    self._read_notifications(text, timestamp)
                    self._read_agent_alias(text)

    def _read_runtime(self, record: dict[str, Any]) -> None:
        timestamp = parse_time(record.get("timestamp"))
        if timestamp is None or self.last_activity is not None and timestamp < self.last_activity:
            return
        kind, subtype = record.get("type"), record.get("subtype")
        phase, detail = None, None
        clear_pending_input = False
        if kind == "result" and isinstance(subtype, str):
            if subtype in RUNTIME_FAILURE_DETAILS:
                phase, detail = "failed", RUNTIME_FAILURE_DETAILS[subtype]
            elif subtype == "success":
                phase = "idle"
            clear_pending_input = phase is not None
        elif kind == "system" and subtype == "session_state_changed":
            state = record.get("state")
            phase = RUNTIME_SESSION_PHASES.get(state) if isinstance(state, str) else None
            clear_pending_input = phase == "idle"
        elif kind == "system" and subtype == "turn_duration":
            pending = [record.get(key) for key in ("pendingBackgroundAgentCount", "pendingWorkflowCount")]
            phase = "working" if any(type(value) is int and value > 0 for value in pending) else "idle"
            clear_pending_input = True
        elif kind in ("user", "assistant") and record.get("isMeta") is not True:
            message = record.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            texts = _texts(content)
            blocks = [item for item in content if isinstance(item, dict)] if isinstance(content, list) else []
            tools = []
            for item in blocks:
                tool_use = (kind == "assistant" and item.get("type") == "tool_use" and isinstance(item.get("input"), dict)
                            and all(isinstance(item.get(key), str) and item[key].strip() for key in ("id", "name")))
                tool_result = (kind == "user" and item.get("type") == "tool_result" and isinstance(item.get("tool_use_id"), str)
                               and item["tool_use_id"].strip())
                if tool_use or tool_result:
                    tools.append(item)
            meaningful = bool(tools) or any(text.strip() for text in texts) or any(
                item.get("type") == "thinking" and isinstance(item.get("thinking"), str) and item["thinking"].strip()
                for item in blocks)
            if meaningful:
                interrupted = kind == "user" and any(text.strip() in INTERRUPTION_TEXTS for text in texts)
                handed_back = record.get("toolEndsTurn") is True
                response_ended = kind == "assistant" and message.get("stop_reason") == "end_turn"
                phase = "working"
                if interrupted:
                    phase = "interrupted"
                elif handed_back:
                    phase = "idle"
                elif response_ended:
                    phase = "unknown"
                clear_pending_input = handed_back or response_ended or interrupted
                identifier = message.get("id")
                if kind == "assistant" and isinstance(identifier, str) and identifier:
                    payload = json.dumps((content, message.get("stop_reason"), record.get("toolEndsTurn")), sort_keys=True).encode()
                    signature = hashlib.sha256(payload).digest()
                    if self.runtime_messages.get(identifier) == signature:
                        return
                    self.runtime_messages[identifier] = signature
                if kind == "user" and any(_prompt_title(text) for text in texts) and not any(item.get("type") == "tool_result" for item in blocks):
                    self.runtime_input_ids.clear()
                for item in tools:
                    if kind == "user" and item.get("type") == "tool_result":
                        self.runtime_input_ids.discard(item["tool_use_id"])
                    elif kind == "assistant" and item.get("type") == "tool_use" and item.get("name") == "AskUserQuestion":
                        questions = item["input"].get("questions")
                        if (isinstance(questions, list) and questions
                                and all(isinstance(question, dict) and isinstance(question.get("question"), str)
                                        and question["question"].strip() for question in questions)):
                            self.runtime_input_ids.add(item["id"])
        if phase is not None:
            if kind in ("user", "assistant"):
                self.handed_back = None
                self.last_execution = timestamp
            if clear_pending_input:
                self.runtime_input_ids.clear()
            self.runtime_phase, self.last_activity, self.runtime_detail = phase, timestamp, detail

    def runtime(self) -> dict[str, Any]:
        if self.runtime_input_ids:
            return {"phase": "unknown", "lastActivityAt": _iso(self.last_activity),
                    "detail": "Recorded input request has no recorded response."}
        return {"phase": self.runtime_phase, "lastActivityAt": _iso(self.last_activity), "detail": self.runtime_detail}

    def _read_usage(self, line: bytes) -> None:
        try:
            record = json.loads(line)
        except (ValueError, UnicodeError):
            return
        if not isinstance(record, dict) or record.get("type") != "assistant":
            return
        message = record.get("message")
        if not isinstance(message, dict):
            return
        identifier, usage = message.get("id"), message.get("usage")
        if not isinstance(identifier, str) or not identifier or not isinstance(usage, dict):
            return
        details = usage.get("output_tokens_details")
        values = {**usage, "reasoning_tokens": details.get("thinking_tokens") if isinstance(details, dict) else None}
        valid = {key: value for key, value in values.items()
                 if key in {"input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens",
                            "output_tokens", "reasoning_tokens"}
                 and isinstance(value, int) and not isinstance(value, bool) and value >= 0}
        if valid:
            previous = self.response_usage.setdefault(identifier, {})
            for key, value in valid.items():
                previous[key] = max(previous.get(key, 0), value)

    def token_usage(self) -> dict[str, int | None]:
        totals = empty_token_usage()
        if not self.response_usage:
            return totals
        fields = {"cachedInputTokens": "cache_read_input_tokens",
                  "cacheWriteTokens": "cache_creation_input_tokens",
                  "outputTokens": "output_tokens", "reasoningOutputTokens": "reasoning_tokens"}
        for field_name, source_name in fields.items():
            if all(source_name in response for response in self.response_usage.values()):
                totals[field_name] = sum(response[source_name] for response in self.response_usage.values())
        if (all("input_tokens" in response for response in self.response_usage.values())
                and totals["cachedInputTokens"] is not None and totals["cacheWriteTokens"] is not None):
            totals["inputTokens"] = (sum(response["input_tokens"] for response in self.response_usage.values())
                                     + totals["cachedInputTokens"] + totals["cacheWriteTokens"])
        if totals["inputTokens"] is not None and totals["outputTokens"] is not None:
            totals["totalTokens"] = totals["inputTokens"] + totals["outputTokens"]
        return totals

    def _read_task_update(self, inputs: dict[str, Any], timestamp: datetime, tool_id: Any) -> None:
        task_id = _task_id(inputs.get("taskId"))
        if task_id is None:
            return
        history = self.histories.setdefault(task_id, _TaskHistory())
        first_event = len(history.events)
        status = inputs.get("status")
        if status == "in_progress":
            history.started = history.started or timestamp
            history._record(timestamp, "started")
        elif status == "completed":
            history.ended = timestamp
            history._record(timestamp, "completed")
        metadata = inputs.get("metadata")
        progress = metadata.get("progress") if isinstance(metadata, dict) else None
        if _finite(progress) and 0 <= progress <= 100:
            history._record(timestamp, "progress", progress)
        if isinstance(tool_id, str):
            self.task_updates[tool_id] = (task_id, history.events[first_event:])

    def _finish_task_update(self, result: dict[str, Any]) -> None:
        identifier = result.get("tool_use_id")
        if not isinstance(identifier, str):
            return
        pending = self.task_updates.pop(identifier, None)
        if pending is None or result.get("is_error") is not True:
            return
        task_id, rejected = pending
        history = self.histories[task_id]
        rejected_ids = {id(event) for event in rejected}
        history.events = [event for event in history.events if id(event) not in rejected_ids]
        history.started = next((parse_time(event["time"]) for event in history.events
                                if event["event"] == "started"), None)
        history.ended = next((parse_time(event["time"]) for event in reversed(history.events)
                              if event["event"] == "completed"), None)

    def _read_notifications(self, text: str, timestamp: datetime) -> None:
        for notification in NOTIFICATION_PATTERN.findall(text):
            task_id = re.search(r"<task-id>([^<]+)</task-id>", notification)
            status = re.search(r"<status>([^<]+)</status>", notification)
            if task_id and status:
                self.notifications[task_id.group(1).strip()] = (status.group(1).strip(), timestamp)

    def _read_workflow_launch(self, result: Any, timestamp: datetime) -> None:
        if not isinstance(result, dict) or not all(isinstance(result.get(key), str) for key in ("runId", "workflowName", "taskId")):
            return
        summary = result.get("summary")
        self.workflows[result["runId"]] = _WorkflowLaunch(result["workflowName"], summary if isinstance(summary, str) else "",
                                                          result["taskId"], timestamp)

    def _read_agent_alias(self, text: str) -> None:
        agent = AGENT_ID_PATTERN.search(text)
        task = TASK_ID_PATTERN.search(text)
        if agent and task:
            self.agent_aliases[task.group(1)] = agent.group(1)


def _agent_state(transcript: _Transcript, notification: tuple[str, datetime] | None,
                 now: datetime) -> tuple[str, datetime | None]:
    """Return an agent's status and end: a terminal notification, else its own hand-back, else running or silent.

    Activity after a notification means the agent was resumed, so that notification no longer applies."""
    status, notified = notification or ("running", None)
    resumed = bool(notified and transcript.last_execution and transcript.last_execution > notified)
    if status not in LIVE_AGENT_STATUSES and not resumed:
        return status, transcript.last_execution or notified
    if transcript.handed_back:
        return "completed", transcript.last_execution
    quiet = transcript.last_execution is not None and now - transcript.last_execution >= SILENT_AFTER
    return ("silent" if quiet else "running"), None


@dataclass
class _Identity:
    """A transcript's naming, read from a bounded prefix and tail and cached until the file changes.

    A final prefix cannot gain a prompt by growing: its prompt was found or its byte or line budget ran out.
    """

    identity: tuple[int, int]
    size: int
    modified: int
    prefix: _Naming
    prefix_end: int
    prefix_final: bool
    naming: _Naming


@dataclass
class _Journal:
    """An incrementally read journal: its count of result lines and, for a workflow's journal, which agents started and finished.

    `started` maps each agent identifier to its label and phase in start order."""

    identity: tuple[int, int] | None = None
    offset: int = 0
    size: int = 0
    modified: int = 0
    checkpoint: bytes = b""
    finished: int = 0
    started: dict[str, tuple[str, str]] = field(default_factory=dict)
    results: set[str] = field(default_factory=set)

    def _consume(self, line: bytes) -> None:
        if not RESULT_LINE_PATTERN.search(line) and b'"agentId"' not in line:
            return
        try:
            record = json.loads(line)
        except (ValueError, UnicodeError):
            return
        if not isinstance(record, dict):
            return
        if record.get("type") == "result":
            self.finished += 1
        agent_id = record.get("agentId")
        if not isinstance(agent_id, str) or not agent_id:
            return
        if record.get("type") == "started":
            label, phase = record.get("label"), record.get("phase")
            self.started[agent_id] = (label if isinstance(label, str) else "", phase if isinstance(phase, str) else "")
        elif record.get("type") == "result":
            self.results.add(agent_id)


@dataclass
class _Session:
    id: str
    task_dir: Path | None = None
    transcript: Path | None = None
    transcript_modified: float = 0
    task_count: int = 0
    modified: float = 0


@dataclass
class SourceStore:
    """Cache read-only Claude sources; relative journal and log paths use the task directory.

    Calls are serialized so several event-stream clients can safely share incremental offsets.
    No scan or polling occurs until a caller requests lists or state.
    """

    root: Path
    _transcripts: dict[Path, _Transcript] = field(default_factory=dict, init=False, repr=False)
    _identities: dict[Path, _Identity] = field(default_factory=dict, init=False, repr=False)
    _journals: dict[Path, _Journal] = field(default_factory=dict, init=False, repr=False)
    _task_documents: dict[Path, dict[str, Any]] = field(default_factory=dict, init=False, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)

    def __post_init__(self) -> None:
        self.root = Path(self.root).expanduser()

    def lists(self) -> list[dict[str, Any]]:
        """List sessions by latest source change, reading identities only for recent or chartable sessions."""
        with self._lock:
            sessions = sorted(self._sessions().values(), key=lambda session: (-session.modified, session.id))
            now = datetime.now(UTC)
            return [self._listing(session, now, []) for session in sessions]

    def plan(self, list_id: str) -> list[dict[str, Any]]:
        """Return a session's normalized tasks from its task files alone, without transcript history.

        An unknown or malformed identifier yields an empty plan; nothing outside the task
        directory is opened except live journals and run logs the tasks name."""
        with self._lock:
            if not LIST_ID.fullmatch(list_id):
                return []
            return [self._task(path, _Transcript()) for path in self._task_paths(self.root / "tasks" / list_id)]

    def state(self, list_id: str, now: datetime) -> dict[str, Any] | None:
        """Return normalized work and private runtime evidence, or None for an unknown session.

        `_runtime` holds a phase, the last meaningful parent activity time and a
        fixed safe failure detail. An ended assistant response alone leaves the
        harness phase unknown; native state or a parent-turn event must confirm it.
        """
        with self._lock:
            if not isinstance(list_id, str) or not LIST_ID.fullmatch(list_id):
                return None
            session = self._sessions().get(list_id)
            if session is None:
                self._task_paths(self.root / "tasks" / list_id)
                return None
            warnings: list[str] = []
            transcript = self._transcript(session.transcript) if session.transcript else _Transcript()
            listing = self._listing(session, now, warnings, read_identity=True)
            tasks = [self._task(path, transcript) for path in self._task_paths(
                session.task_dir or self.root / "tasks" / list_id)]
            agents = self._agents(session, transcript, now, warnings)
            workflows = self._workflows(session, transcript, now, warnings)
            assignable = {**agents, **{agent["id"]: agent for workflow in workflows for agent in workflow["agents"]}}
            assigned: set[str] = set()
            for task in tasks:
                agent_ids = task.pop("agentIds")
                task["agents"] = [assignable[agent_id] for agent_id in agent_ids if agent_id in assignable]
                assigned.update(agent_ids)
            for workflow in workflows:
                workflow["agents"] = [agent for agent in workflow["agents"] if agent["id"] not in assigned]
            return {**listing, "transcriptUpdatedAt": _modified(session.transcript), "tasks": tasks,
                    "provider": "claude", "sourceId": session.id, "tokenUsage": transcript.token_usage(),
                    "_runtime": transcript.runtime(),
                    "agents": [agent for key, agent in agents.items() if key not in assigned],
                    "workflows": workflows, "warnings": warnings}

    def _sessions(self) -> dict[str, _Session]:
        sessions: dict[str, _Session] = {}
        for task_dir in _children(self.root / "tasks", strict=True):
            directory_stat = _stat(task_dir, strict=True)
            if directory_stat is None or not stat.S_ISDIR(directory_stat.st_mode):
                continue
            session = sessions.setdefault(task_dir.name, _Session(task_dir.name))
            session.task_dir = task_dir
            task_paths = self._task_paths(task_dir)
            session.task_count = len(task_paths)
            for path in task_paths:
                file_stat = _stat(path, strict=True)
                if file_stat:
                    session.modified = max(session.modified, file_stat.st_mtime)
        for project_dir in _children(self.root / "projects", strict=True):
            for path in _children(project_dir, strict=True):
                if path.suffix != ".jsonl":
                    continue
                file_stat = _stat(path, strict=True)
                if file_stat is None or not stat.S_ISREG(file_stat.st_mode):
                    continue
                session = sessions.setdefault(path.stem, _Session(path.stem))
                previous_stat = _stat(session.transcript, strict=True) if session.transcript else None
                if previous_stat is None or file_stat.st_mtime >= previous_stat.st_mtime:
                    session.transcript = path
                    session.transcript_modified = file_stat.st_mtime
                session.modified = max(session.modified, file_stat.st_mtime)
        return sessions

    def _task_paths(self, task_dir: Path | None) -> list[Path]:
        paths = [path for path in _children(task_dir, strict=True)
                 if path.suffix == ".json" and _regular_stat(path, strict=True)] if task_dir else []
        present = set(paths)
        for path in list(self._task_documents):
            if path.parent == task_dir and path not in present:
                del self._task_documents[path]
        return sorted(paths, key=lambda path: path.name)

    def _listing(self, session: _Session, now: datetime, warnings: list[str], *,
                 read_identity: bool | None = None) -> dict[str, Any]:
        recent = session.modified >= (now - RECENT_SESSION_WINDOW).timestamp()
        agent_count = _agent_count(_agents_directory(session.transcript)) if session.transcript else 0
        chartable = bool(session.task_count or agent_count)
        if read_identity is None:
            read_identity = recent or chartable
        identity = self._identity(session.transcript) if read_identity and session.transcript else None
        naming = identity.naming if identity else _Naming()
        title = naming.custom_title or naming.prompt
        if read_identity and session.transcript:
            custom_title_path = session.transcript.with_suffix("") / "custom-title.json"
            if _stat(custom_title_path):
                custom_title = _read_json(custom_title_path, warnings).get("customTitle")
                if isinstance(custom_title, str) and custom_title.strip():
                    title = custom_title.strip()
        updated = datetime.fromtimestamp(session.modified, UTC) if session.modified else None
        transcript_updated = (datetime.fromtimestamp(session.transcript_modified, UTC)
                              if session.transcript else None)
        title = title or session.id
        return {"id": session.id, "provider": "claude", "sourceId": session.id,
                "title": title, "prompt": "" if naming.prompt == title else naming.prompt,
                "project": naming.project, "updatedAt": _iso(updated), "recent": recent,
                "active": session_active(transcript_updated, now), "chartable": chartable,
                "taskCount": session.task_count, "agentCount": agent_count,
                "headless": naming.entrypoint == HEADLESS_ENTRYPOINT}

    def _identity(self, path: Path) -> _Identity | None:
        file_stat = _regular_stat(path)
        if file_stat is None:
            return None
        identity = (file_stat.st_dev, file_stat.st_ino)
        cached = self._identities.get(path)
        same_file = cached and cached.identity == identity and file_stat.st_size >= cached.size
        if same_file and cached.size == file_stat.st_size and cached.modified == file_stat.st_mtime_ns:
            return cached
        try:
            with path.open("rb") as stream:
                if same_file and cached.prefix_final:
                    prefix, prefix_end, prefix_final = cached.prefix, cached.prefix_end, True
                else:
                    prefix, prefix_end, prefix_final = self._read_prefix(stream)
                tail = _Naming()
                tail_start = max(prefix_end, file_stat.st_size - IDENTITY_TAIL_BYTES)
                stream.seek(tail_start)
                skipped_lines = tail_start > prefix_end
                if skipped_lines:
                    stream.readline()
                for line in stream:
                    tail.read(line)
        except OSError:
            return cached
        if skipped_lines:
            tail.prompt = ""
        cached = _Identity(identity, file_stat.st_size, file_stat.st_mtime_ns, prefix, prefix_end, prefix_final,
                           prefix.followed_by(tail))
        self._identities[path] = cached
        return cached

    def _read_prefix(self, stream: Any) -> tuple[_Naming, int, bool]:
        prefix = _Naming()
        remaining = IDENTITY_SCAN_BYTES
        for _ in range(IDENTITY_SCAN_LINES):
            line_start = stream.tell()
            line = stream.readline(remaining + 1)
            remaining -= len(line)
            if remaining < 0 or not line:
                return prefix, line_start, remaining < 0
            prefix.read(line)
            if prefix.prompt:
                return prefix, stream.tell(), True
        return prefix, stream.tell(), True

    def _transcript(self, path: Path) -> _Transcript:
        file_stat = _regular_stat(path, strict=True)
        if file_stat is None:
            self._transcripts.pop(path, None)
            return _Transcript()
        identity = (file_stat.st_dev, file_stat.st_ino)
        cached = self._transcripts.get(path)
        reset = (cached is None or cached.identity != identity or file_stat.st_size < cached.size
                 or (file_stat.st_size == cached.size and file_stat.st_mtime_ns != cached.modified))
        if reset:
            cached = _Transcript(identity=identity)
            self._transcripts[path] = cached
        try:
            with path.open("rb") as stream:
                if cached.offset and append_checkpoint(stream, cached.offset) != cached.checkpoint:
                    cached = _Transcript(identity=identity)
                    self._transcripts[path] = cached
                stream.seek(cached.offset)
                while line := stream.readline():
                    if not _line_is_complete(line, TRANSCRIPT_TOKENS):
                        break
                    cached._consume(line)
                    cached.offset = stream.tell()
                cached.checkpoint = append_checkpoint(stream, cached.offset)
        except FileNotFoundError:
            self._transcripts.pop(path, None)
            return _Transcript()
        cached.size, cached.modified = file_stat.st_size, file_stat.st_mtime_ns
        naming = replace(cached.naming)
        self._identities[path] = _Identity(identity, cached.size, cached.modified, naming, cached.offset, True, naming)
        return cached

    def _task(self, path: Path, transcript: _Transcript) -> dict[str, Any]:
        warnings: list[str] = []
        document = _read_json(path, warnings)
        readable = bool(document)
        if readable:
            self._task_documents[path] = deepcopy(document)
        elif _regular_stat(path) is not None and path in self._task_documents:
            document = deepcopy(self._task_documents[path])
            warnings.append("Using the last readable task state")
        task_id = _task_id(document.get("id")) or path.stem
        metadata = document.get("metadata", {})
        if not isinstance(metadata, dict):
            warnings.append("metadata must be an object")
            metadata = {}
        status = document.get("status", "pending")
        if not isinstance(status, str) or status not in TASK_STATUSES:
            warnings.append("status must be pending, in_progress, or completed")
            status = "pending"
        weight = metadata.get("weight", 1)
        if not _finite(weight) or weight <= 0:
            warnings.append("weight must be a positive finite number")
            weight = 1
        progress = metadata.get("progress", 0)
        if not _finite(progress) or not 0 <= progress <= 100:
            warnings.append("progress must be between 0 and 100")
            progress = 0
        milestone = self._text(metadata, "milestone", "", warnings)
        lane = self._text(metadata, "lane", milestone or "Main", warnings)
        history = transcript.histories.get(task_id, _TaskHistory())
        return {
            "id": task_id, "subject": self._text(document, "subject", task_id, warnings),
            "description": self._text(document, "description", "", warnings),
            "activeForm": self._text(document, "activeForm", "", warnings), "status": status,
            "blocks": self._ids(document, "blocks", warnings), "blockedBy": self._ids(document, "blockedBy", warnings),
            "metadata": metadata, "weight": weight, "milestone": milestone, "lane": lane or milestone or "Main",
            "progress": progress, "liveProgress": self._live(metadata, path.parent, warnings),
            "estimate": self._estimate(metadata, warnings), "created": _iso(history.created),
            "started": _iso(history.started), "ended": _iso(history.ended),
            "history": list(history.events), "warnings": warnings,
            "readable": readable, "updatedAt": _modified(path),
            "agentIds": self._ids(metadata, "agents", warnings),
            "runs": self._runs(metadata, path.parent, warnings),
        }

    def _text(self, document: dict[str, Any], key: str, default: str, warnings: list[str]) -> str:
        value = document.get(key, default)
        if isinstance(value, str):
            return value
        warnings.append(f"{key} must be text")
        return default

    def _ids(self, document: dict[str, Any], key: str, warnings: list[str]) -> list[str]:
        values = document.get(key, [])
        if not isinstance(values, list):
            warnings.append(f"{key} must be a list of identifiers")
            return []
        result: list[str] = []
        for value in values:
            identifier = _task_id(value)
            if identifier is None:
                warnings.append(f"{key} contains an invalid identifier")
            elif identifier not in result:
                result.append(identifier)
        return result

    def _estimate(self, metadata: dict[str, Any], warnings: list[str]) -> dict[str, float] | None:
        if "estimate" not in metadata:
            return None
        estimate = metadata["estimate"]
        values = [estimate.get(key) for key in ("low", "likely", "high")] if isinstance(estimate, dict) else []
        if len(values) != 3 or not all(_finite(value) for value in values) or not 0 <= values[0] <= values[1] <= values[2]:
            warnings.append("estimate requires finite hours with 0 <= low <= likely <= high")
            return None
        return dict(zip(("low", "likely", "high"), values))

    def _path(self, value: str, task_dir: Path) -> Path:
        path = Path(value)
        try:
            path = path.expanduser()
        except RuntimeError:
            pass
        return path if path.is_absolute() else task_dir / path

    def _live(self, metadata: dict[str, Any], task_dir: Path, warnings: list[str]) -> float:
        if "live" not in metadata:
            return 0
        live = metadata["live"]
        if not isinstance(live, dict):
            warnings.append("live must be an object")
            return 0
        planned, start, end = live.get("planned"), live.get("from"), live.get("to")
        journal = live.get("journal")
        if not isinstance(journal, str) or not journal or not isinstance(planned, int) or isinstance(planned, bool) or planned <= 0:
            warnings.append("live requires a journal path and positive integer planned count")
            return 0
        if not _finite(start) or not _finite(end) or not 0 <= start <= end <= 100:
            warnings.append("live requires 0 <= from <= to <= 100")
            return 0
        path = self._path(journal, task_dir)
        read = self._journal(path)
        if read is None:
            warnings.append("live journal is unavailable")
            return 0
        return start + (end - start) * min(read.finished / planned, 1)

    def _journal(self, path: Path) -> _Journal | None:
        file_stat = _regular_stat(path, strict=True)
        if file_stat is None:
            self._journals.pop(path, None)
            return None
        identity = (file_stat.st_dev, file_stat.st_ino)
        cached = self._journals.get(path)
        if (cached is None or cached.identity != identity or file_stat.st_size < cached.size
                or (file_stat.st_size == cached.size and file_stat.st_mtime_ns != cached.modified)):
            cached = _Journal(identity=identity)
            self._journals[path] = cached
        try:
            with path.open("rb") as stream:
                if cached.offset and append_checkpoint(stream, cached.offset) != cached.checkpoint:
                    cached = _Journal(identity=identity)
                    self._journals[path] = cached
                stream.seek(cached.offset)
                while line := stream.readline():
                    if not _line_is_complete(line, (b'"result"', b'"agentId"')):
                        break
                    cached._consume(line)
                    cached.offset = stream.tell()
                cached.checkpoint = append_checkpoint(stream, cached.offset)
        except FileNotFoundError:
            self._journals.pop(path, None)
            return None
        cached.size, cached.modified = file_stat.st_size, file_stat.st_mtime_ns
        return cached

    def _runs(self, metadata: dict[str, Any], task_dir: Path, warnings: list[str]) -> list[dict[str, Any]]:
        values = metadata.get("runs", [])
        if not isinstance(values, list):
            warnings.append("runs must be a list")
            return []
        runs: list[dict[str, Any]] = []
        for index, value in enumerate(values):
            if not isinstance(value, dict):
                warnings.append(f"runs[{index}] must be an object")
                continue
            label = value.get("label")
            started = parse_time(value.get("started"))
            ended = parse_time(value.get("ended"))
            log = value.get("log")
            pid = value.get("pid")
            invalid = (not isinstance(label, str) or not label or started is None
                       or (value.get("ended") is not None and ended is None)
                       or (ended is not None and started is not None and ended < started)
                       or (log is not None and (not isinstance(log, str) or not log))
                       or (pid is not None and (not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0)))
            if invalid:
                warnings.append(f"runs[{index}] has invalid label, timestamps, log path, or pid")
                continue
            log_updated = None
            if log:
                log_updated = _modified(self._path(log, task_dir))
                if log_updated is None:
                    warnings.append(f"runs[{index}] log is unavailable")
            runs.append({"label": label, "started": _iso(started), "ended": _iso(ended),
                         "log": log, "logUpdatedAt": log_updated,
                         "lastActivityAt": log_updated or _iso(ended or started), "activity": label,
                         "pid": pid, "pidAlive": None if pid is None or ended else _process_alive(pid),
                         "status": "completed" if ended else "running"})
        return runs

    def _agents(self, session: _Session, parent: _Transcript, now: datetime, warnings: list[str]) -> dict[str, dict[str, Any]]:
        if session.transcript is None:
            return {}
        directory = _agents_directory(session.transcript)
        agent_ids = _agent_ids(directory)
        notifications: dict[str, tuple[str, datetime]] = {}
        for task_id, notification in parent.notifications.items():
            notifications[parent.agent_aliases.get(task_id, task_id)] = notification
        agents: dict[str, dict[str, Any]] = {}
        for agent_id in sorted(agent_ids):
            transcript = self._transcript(directory / f"agent-{agent_id}.jsonl")
            status, ended = _agent_state(transcript, notifications.get(agent_id), now)
            agents[agent_id] = self._agent(directory, agent_id, transcript, status, ended, warnings)
        return agents

    def _agent(self, directory: Path, agent_id: str, transcript: _Transcript, status: str, ended: datetime | None,
               warnings: list[str]) -> dict[str, Any]:
        meta_path = directory / f"agent-{agent_id}.meta.json"
        metadata = _read_json(meta_path, warnings) if _stat(meta_path) else {}
        return {
            "id": agent_id, "description": self._text(metadata, "description", agent_id, warnings),
            "agentType": self._text(metadata, "agentType", "", warnings),
            "worktreeBranch": self._text(metadata, "worktreeBranch", "", warnings),
            "started": _iso(transcript.first), "ended": _iso(ended),
            "lastActivityAt": _iso(transcript.last_execution), "activity": transcript.activity,
            "status": status,
        }

    def _workflows(self, session: _Session, parent: _Transcript, now: datetime, warnings: list[str]) -> list[dict[str, Any]]:
        """Return each workflow run with its agents in journal start order.

        A journal result line or the agent's own hand-back finishes an agent. The run's state is its background
        task's terminal notification; until then it runs while an agent runs and falls silent with its agents.
        An agent still unfinished when its run ends takes the run's state, or `stopped` when the run completed."""
        if session.transcript is None:
            return []
        workflows: list[dict[str, Any]] = []
        for run in _workflow_runs(_agents_directory(session.transcript)):
            launch = parent.workflows.get(run.name)
            notification = parent.notifications.get(launch.task_id) if launch else None
            run_ended = notification is not None and notification[0] not in LIVE_AGENT_STATUSES
            run_status, notified = notification if run_ended else ("running", None)
            journal = self._journal(run / "journal.jsonl") or _Journal()
            name = launch.name if launch else run.name
            agents: list[dict[str, Any]] = []
            firsts: list[datetime] = []
            lasts: list[datetime] = []
            for agent_id in [*journal.started, *sorted(_agent_ids(run) - set(journal.started))]:
                transcript = self._transcript(run / f"agent-{agent_id}.jsonl")
                firsts += [transcript.first] if transcript.first else []
                lasts += [transcript.last_execution] if transcript.last_execution else []
                if agent_id in journal.results:
                    status, ended = "completed", transcript.last_execution
                else:
                    status, ended = _agent_state(transcript, None, now)
                if run_ended and ended is None:
                    status, ended = ("stopped" if run_status == "completed" else run_status), transcript.last_execution
                agent = self._agent(run, agent_id, transcript, status, ended, warnings)
                label, phase = journal.started.get(agent_id, ("", ""))
                agent.update(description=label or agent["description"], phase=phase, workflow=name)
                agents.append(agent)
            last = max(lasts, default=None)
            if not run_ended:
                live = any(agent["status"] == "running" for agent in agents)
                quiet = last is not None and now - last >= SILENT_AFTER
                run_status = "running" if live or not quiet else "silent"
            workflows.append({
                "id": run.name, "name": name, "description": launch.summary if launch else "",
                "status": run_status, "started": _iso(launch.launched if launch else min(firsts, default=None)),
                "ended": _iso(notified), "lastActivityAt": _iso(last), "activity": "", "agents": agents,
            })
        return workflows
