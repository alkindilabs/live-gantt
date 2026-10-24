"""Read local Codex session metadata, native plans, activity and recorded usage."""

from __future__ import annotations

import json
import hashlib
import os
import re
import threading
from collections import deque
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .freshness import session_active
from .sources import (LIST_ID, RECENT_SESSION_WINDOW, SILENT_AFTER, TASK_STATUSES, UTC,
                      _finite, _iso, _prompt_title, _regular_stat, append_checkpoint, parse_time)
from .usage import PERCENT_LIMIT, empty_token_usage


METADATA_BYTES = 64 * 1024
USAGE_TAIL_BYTES = 256 * 1024
USAGE_TRANSCRIPT_LIMIT = 64
USAGE_SNAPSHOT_LIMIT = 128
TITLE_INDEX_BYTES = 256 * 1024
TITLE_INDEX_LIMIT = 1024
MAX_PLAN_STEPS = 256
MAX_PLAN_TEXT = 4096
MAX_PENDING_NATIVE_CALLS = 32
NATIVE_PLAN_LANE = "Native plan"
PLAN_SUCCESS_OUTPUT = "Plan updated"
NANOSECONDS_PER_SECOND = 1_000_000_000
SESSION_FILENAME_ID = re.compile(r"([a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12})$")
TOKEN_FIELDS = {
    "inputTokens": "input_tokens", "cachedInputTokens": "cached_input_tokens",
    "cacheWriteTokens": "cache_write_input_tokens", "outputTokens": "output_tokens",
    "reasoningOutputTokens": "reasoning_output_tokens", "totalTokens": "total_tokens",
}


def default_codex_root() -> Path:
    """Return the configured Codex data root, expanding the user's home directory."""
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()


def _object(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _identifier(value: Any) -> str | None:
    return value if isinstance(value, str) and LIST_ID.fullmatch(value) else None


def _integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value, UTC)
        except (ValueError, OverflowError, OSError):
            return None
    return parse_time(value)


def _decode(line: bytes) -> dict:
    try:
        return _object(json.loads(line))
    except (ValueError, UnicodeError, RecursionError):
        return {}


def _discovery_error(error):
    if not isinstance(error, FileNotFoundError):
        raise error


def _plan_steps(value):
    if not isinstance(value, list) or len(value) > MAX_PLAN_STEPS:
        return None
    steps = []
    for item in value:
        if not isinstance(item, dict):
            return None
        step, status = item.get("step"), item.get("status")
        if (not isinstance(step, str) or not step.strip() or len(step) > MAX_PLAN_TEXT
                or not isinstance(status, str) or status not in TASK_STATUSES):
            return None
        steps.append((step.strip(), status))
    return steps


def _plan_success(output):
    if isinstance(output, str):
        return output.strip() == PLAN_SUCCESS_OUTPUT
    if isinstance(output, list) and all(isinstance(item, dict) and
            item.get("type") in ("input_text", "text") and isinstance(item.get("text"), str) for item in output):
        return "".join(item["text"] for item in output).strip() == PLAN_SUCCESS_OUTPUT
    return False


def _nonempty_text(value):
    return isinstance(value, str) and bool(value.strip())


def _meaningful_content(value):
    if isinstance(value, str):
        return _nonempty_text(value)
    if not isinstance(value, list):
        return False
    return any(isinstance(item, dict) and (
        item.get("type") in ("text", "input_text", "output_text") and _nonempty_text(item.get("text"))
        or item.get("type") == "input_image" and _nonempty_text(item.get("image_url"))) for item in value)


def _windows(limits: dict) -> list[dict]:
    windows = []
    for role in ("primary", "secondary"):
        value = _object(limits.get(role))
        percent = value.get("used_percent")
        if not _finite(percent) or not 0 <= percent <= PERCENT_LIMIT:
            continue
        minutes = _integer(value.get("window_minutes"))
        windows.append({"id": role, "usedPercent": percent, "windowMinutes": minutes or None,
                        "resetsAt": _iso(_timestamp(value.get("resets_at")))})
    return windows


@dataclass
class _Identity:
    id: str
    path: Path
    signature: tuple
    modified: datetime
    parent: str | None = None
    project: str = ""
    archived: bool = False
    title: str = ""
    nickname: str = ""


@dataclass
class _TitleIndex:
    signature: tuple | None = None
    offset: int = 0
    titles: dict[str, str] = field(default_factory=dict)


@dataclass
class _Transcript:
    signature: tuple | None = None
    size: int = 0
    modified_ns: int = 0
    offset: int = 0
    checkpoint: bytes = b""
    first: datetime | None = None
    started: datetime | None = None
    ended: datetime | None = None
    turn_id: str | None = None
    running: bool = False
    observed_activity: bool = False
    interrupted: bool = False
    last_activity: datetime | None = None
    runtime_phase: str = "unknown"
    runtime_detail: str | None = None
    input_mode: str | None = None
    pending_inputs: dict = field(default_factory=dict)
    plan: list = field(default_factory=list)
    plan_revision: int = 0
    pending_plans: dict = field(default_factory=dict)
    token_at: datetime | None = None
    token_usage: dict = field(default_factory=empty_token_usage)
    snapshots: deque = field(default_factory=lambda: deque(maxlen=USAGE_SNAPSHOT_LIMIT))

    def _update_plan(self, steps, timestamp):
        if steps != [(task["subject"], task["status"]) for task in self.plan]:
            self._activity(timestamp)
        previous = {task["id"]: task for task in self.plan}
        occurrences = {}
        plan = []
        for text, status in steps:
            occurrence = occurrences.get(text, 0)
            occurrences[text] = occurrence + 1
            digest = hashlib.sha256(json.dumps([text, occurrence]).encode()).hexdigest()
            identifier = "plan-" + digest
            task = previous.get(identifier)
            if task is None:
                task = {"id": identifier, "subject": text, "description": "", "activeForm": "",
                        "status": "pending", "blocks": [], "blockedBy": [],
                        "metadata": {"lane": NATIVE_PLAN_LANE}, "weight": 1, "milestone": "",
                        "lane": NATIVE_PLAN_LANE, "progress": 0, "liveProgress": 0, "estimate": None,
                        "created": _iso(timestamp), "started": None, "ended": None, "history": [],
                        "warnings": [], "readable": True, "updatedAt": _iso(timestamp),
                        "agents": [], "runs": []}
            if not task["history"] or task["status"] != status:
                task["status"] = status
                task["updatedAt"] = _iso(timestamp)
                if status == "in_progress":
                    task["started"] = task["started"] or _iso(timestamp)
                    task["ended"] = None
                elif status == "completed":
                    task["ended"] = _iso(timestamp)
                else:
                    task["ended"] = None
                task["history"].append({"time": _iso(timestamp), "event": status})
            plan.append(task)
        self.plan = plan

    def _plan_record(self, record, payload, timestamp):
        if timestamp is None:
            return
        if record.get("type") == "event_msg" and payload.get("type") == "plan_update":
            steps = _plan_steps(payload.get("plan"))
            if steps is not None:
                self._update_plan(steps, timestamp)
                self.plan_revision += 1
        elif record.get("type") == "response_item":
            call_id = _identifier(payload.get("call_id"))
            if call_id is None:
                return
            if payload.get("type") == "function_call" and payload.get("name") in ("update_plan", "functions.update_plan"):
                try:
                    arguments = json.loads(payload.get("arguments", ""))
                except (ValueError, TypeError, RecursionError):
                    return
                steps = _plan_steps(_object(arguments).get("plan"))
                if steps is not None:
                    self.pending_plans[call_id] = (steps, self.plan_revision)
                    while len(self.pending_plans) > MAX_PENDING_NATIVE_CALLS:
                        self.pending_plans.pop(next(iter(self.pending_plans)))
            elif payload.get("type") == "function_call_output":
                pending = self.pending_plans.pop(call_id, None)
                if pending is not None and pending[1] == self.plan_revision and _plan_success(payload.get("output")):
                    self._update_plan(pending[0], timestamp)

    def _activity(self, timestamp):
        if timestamp is not None:
            self.last_activity = max(self.last_activity, timestamp) if self.last_activity else timestamp

    def _runtime_record(self, record, payload, kind, timestamp):
        if timestamp is None:
            return
        if kind in ("task_started", "turn_started"):
            self.started = self.started or timestamp
            self.ended = None
            self.turn_id = payload.get("turn_id")
            self.running = True
            self.interrupted = False
            self.observed_activity = True
            self.runtime_phase, self.runtime_detail = "working", None
            self.input_mode = payload.get("collaboration_mode_kind")
            self.pending_plans.clear()
            self.pending_inputs.clear()
            self._activity(timestamp)
        elif kind in ("task_complete", "turn_complete", "turn_aborted"):
            turn_id = payload.get("turn_id")
            if self.turn_id and turn_id != self.turn_id:
                return
            self.ended = timestamp
            self.running = False
            self.interrupted = kind == "turn_aborted"
            self.observed_activity = True
            self.pending_inputs.clear()
            reason = payload.get("reason")
            failed = payload.get("error") is not None or reason == "budget_limited"
            self.runtime_phase = "failed" if failed else "interrupted" if self.interrupted else "idle"
            self.runtime_detail = ("Recorded turn stopped at a budget limit" if reason == "budget_limited"
                                   else "Recorded turn failed" if failed else "Recorded turn was interrupted"
                                   if self.interrupted else None)
            self._activity(timestamp)
        elif record.get("type") == "event_msg" and kind in ("user_message", "agent_message"):
            if (_nonempty_text(payload.get("message")) or any(_nonempty_text(image)
                    for images in (payload.get("images"), payload.get("local_images"))
                    if isinstance(images, list) for image in images)):
                self._activity(timestamp)
        elif record.get("type") == "response_item":
            item_kind = payload.get("type")
            message = item_kind == "agent_message" or (
                item_kind == "message" and payload.get("role") in ("user", "assistant"))
            call_id = _identifier(payload.get("call_id"))
            tool_call = (item_kind in ("function_call", "custom_tool_call") and call_id is not None
                         and _nonempty_text(payload.get("name")) and _nonempty_text(payload.get(
                             "arguments" if item_kind == "function_call" else "input")))
            tool_result = (item_kind in ("function_call_output", "custom_tool_call_output")
                           and call_id is not None and _meaningful_content(payload.get("output")))
            if message and _meaningful_content(payload.get("content")) or tool_call or tool_result:
                self._activity(timestamp)
            if item_kind == "function_call_output" and tool_result:
                self.pending_inputs.pop(call_id, None)
            elif (item_kind == "function_call" and self.running and self.input_mode == "plan"
                  and call_id is not None and payload.get("name") in ("request_user_input", "functions.request_user_input")):
                try:
                    arguments = json.loads(payload.get("arguments", ""))
                except (ValueError, TypeError, RecursionError):
                    return
                questions = _object(arguments).get("questions")
                if (isinstance(questions, list) and questions and all(isinstance(question, dict)
                        and all(isinstance(question.get(key), str) and question[key].strip()
                                for key in ("id", "header", "question")) for question in questions)):
                    self.pending_inputs[call_id] = None
                    while len(self.pending_inputs) > MAX_PENDING_NATIVE_CALLS:
                        self.pending_inputs.pop(next(iter(self.pending_inputs)))

    def runtime(self):
        unresolved = self.running and bool(self.pending_inputs)
        return {"phase": "unknown" if unresolved else self.runtime_phase,
                "lastActivityAt": _iso(self.last_activity),
                "detail": "Recorded input request has no recorded response" if unresolved else self.runtime_detail}

    def consume(self, record: dict, source_id: str) -> None:
        timestamp = parse_time(record.get("timestamp"))
        payload = _object(record.get("payload"))
        kind = payload.get("type") if record.get("type") == "event_msg" else record.get("type")
        if not isinstance(kind, str):
            return
        if timestamp:
            self.first = min(self.first, timestamp) if self.first else timestamp
        self._plan_record(record, payload, timestamp)
        self._runtime_record(record, payload, kind, timestamp)
        if kind not in {"token_count", "token_usage_record"}:
            return
        usage = _object(_object(payload.get("info")).get("total_token_usage"))
        if kind == "token_usage_record":
            usage = _object(payload.get("thread_token_usage"))
            if not usage:
                usage = _object(record.get("thread_token_usage"))
        normalized = {name: _integer(usage.get(native)) for name, native in TOKEN_FIELDS.items()}
        if timestamp and any(value is not None for value in normalized.values()) and (
                self.token_at is None or timestamp >= self.token_at):
            self.token_usage = normalized
            self.token_at = timestamp
        limits = payload.get("rate_limits")
        if not isinstance(limits, dict) or timestamp is None:
            return
        snapshot = {"sourceId": source_id, "capturedAt": _iso(timestamp),
                    "bucketId": limits.get("limit_id") if isinstance(limits.get("limit_id"), str) else "codex",
                    "windows": _windows(limits)}
        previous = self.snapshots[-1] if self.snapshots else None
        if previous and all(previous[key] == snapshot[key] for key in ("sourceId", "bucketId", "windows")):
            return
        self.snapshots.append(snapshot)


@dataclass
class CodexSourceStore:
    """Serialize reads of local Codex files and retain bounded incremental caches.

    Listings read metadata only. Selection reconstructs a session and its descendants;
    quota discovery reads bounded tails of recent active files, excluding archives.
    """

    root: Path
    _identities: dict = field(default_factory=dict, init=False, repr=False)
    _identity_signatures: dict = field(default_factory=dict, init=False, repr=False)
    _transcripts: dict = field(default_factory=dict, init=False, repr=False)
    _usage: dict = field(default_factory=dict, init=False, repr=False)
    _title_index: _TitleIndex = field(default_factory=_TitleIndex, init=False, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)

    def __post_init__(self) -> None:
        self.root = Path(self.root).expanduser()

    def lists(self) -> list[dict]:
        """Return selectable root and orphan sessions in source modification order."""
        with self._lock:
            sessions = self._sessions()
            now = datetime.now(UTC)
            return [self._listing(session, sessions, now) for session in sessions.values()
                    if session.parent not in sessions]

    def state(self, source_id: str, now: datetime) -> dict | None:
        """Return the root's native plan, session activity and tokens, or None for unknown IDs."""
        if _identifier(source_id) is None:
            return None
        with self._lock:
            sessions = self._sessions()
            session = sessions.get(source_id)
            if session is None:
                return None
            if session.archived:
                resolved = self._identity(session.path, session.signature, True)
                if resolved is None or resolved.id != source_id:
                    return None
                session = resolved
                sessions[source_id] = resolved
            selected = [session, *self._descendants(source_id, sessions)]
            transcripts = {item.id: self._read(item, self._transcripts, None) for item in selected}
            return {**self._listing(session, sessions, now), "transcriptUpdatedAt": _iso(session.modified),
                    "tasks": deepcopy(transcripts[session.id].plan), "workflows": [], "warnings": [],
                    "agents": [self._agent(item, transcripts[item.id], now, item.id == source_id)
                               for item in selected], "tokenUsage": dict(transcripts[source_id].token_usage),
                    "_runtime": transcripts[source_id].runtime()}

    def usage_snapshots(self) -> list[dict]:
        """Read bounded active transcript tails and return distinct recorded quota snapshots."""
        with self._lock:
            sessions = [session for session in self._sessions().values() if not session.archived]
            selected = sessions[:USAGE_TRANSCRIPT_LIMIT]
            selected_paths = {session.path for session in selected}
            self._usage = {path: cache for path, cache in self._usage.items() if path in selected_paths}
            snapshots = [dict(snapshot, windows=[dict(window) for window in snapshot["windows"]])
                         for session in selected for snapshot in self._read(session, self._usage, USAGE_TAIL_BYTES).snapshots]
            return sorted(snapshots, key=lambda value: (value["capturedAt"], value["sourceId"], value["bucketId"]))

    def _sessions(self) -> dict[str, _Identity]:
        self._read_titles()
        identities = []
        existing = set()
        for directory, archived in (("sessions", False), ("archived_sessions", True)):
            for parent, directories, filenames in os.walk(self.root / directory, followlinks=False,
                                                         onerror=_discovery_error):
                directories[:] = [name for name in directories if not (Path(parent) / name).is_symlink()]
                for filename in filenames:
                    path = Path(parent) / filename
                    if path.suffix != ".jsonl" or path.is_symlink():
                        continue
                    info = _regular_stat(path, strict=True)
                    if info is None:
                        continue
                    existing.add(path)
                    signature = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
                    identity = self._identities.get(path)
                    if self._identity_signatures.get(path) != signature:
                        if archived:
                            match = SESSION_FILENAME_ID.search(path.stem)
                            identifier = match.group(1) if match else path.stem.removeprefix("rollout-")
                            identity = (_Identity(identifier, path, signature,
                                                  datetime.fromtimestamp(info.st_mtime, UTC), archived=True)
                                        if _identifier(identifier) else None)
                        else:
                            identity = self._identity(path, signature, archived)
                        self._identities[path] = identity
                        self._identity_signatures[path] = signature
                    if identity:
                        identities.append(identity)
        self._identities = {path: identity for path, identity in self._identities.items() if path in existing}
        self._identity_signatures = {path: signature for path, signature in self._identity_signatures.items()
                                     if path in existing}
        self._transcripts = {path: cache for path, cache in self._transcripts.items() if path in existing}
        result = {}
        for identity in sorted(identities, key=lambda item: (-item.modified.timestamp(), item.id)):
            result.setdefault(identity.id, identity)
        return result

    def _identity(self, path: Path, signature: tuple, archived: bool) -> _Identity | None:
        try:
            with path.open("rb") as stream:
                prefix = stream.read(METADATA_BYTES)
        except FileNotFoundError:
            return None
        identity = None
        for line in prefix.splitlines():
            record = _decode(line)
            payload = _object(record.get("payload"))
            if identity and record.get("type") == "event_msg" and payload.get("type") == "user_message":
                message = payload.get("message")
                if not identity.title and isinstance(message, str):
                    identity.title = _prompt_title(message)
            if record.get("type") != "session_meta" or identity:
                continue
            identifier = _identifier(payload.get("id")) or _identifier(payload.get("session_id"))
            if identifier is None:
                return None
            spawned = _object(_object(_object(payload.get("source")).get("subagent")).get("thread_spawn"))
            parent = _identifier(payload.get("parent_thread_id")) or _identifier(spawned.get("parent_thread_id"))
            cwd = payload.get("cwd")
            nickname = payload.get("agent_nickname")
            identity = _Identity(identifier, path, signature,
                                 datetime.fromtimestamp(signature[3] / NANOSECONDS_PER_SECOND, UTC),
                                 parent, cwd if isinstance(cwd, str) else "", archived,
                                 nickname=nickname if isinstance(nickname, str) else "")
        return identity

    def _read_titles(self) -> None:
        path = self.root / "session_index.jsonl"
        info = _regular_stat(path)
        if info is None:
            self._title_index = _TitleIndex()
            return
        signature = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        index = self._title_index
        if index.signature == signature:
            return
        reset = (index.signature is None or signature[:2] != index.signature[:2]
                 or signature[2] < index.signature[2]
                 or signature[2] == index.signature[2] and signature[3] != index.signature[3])
        if reset:
            index = self._title_index = _TitleIndex()
        start = max(0, info.st_size - TITLE_INDEX_BYTES)
        try:
            with path.open("rb") as stream:
                if reset or index.offset < start:
                    stream.seek(start)
                    if start:
                        stream.readline()
                    index.offset = stream.tell()
                stream.seek(index.offset)
                while line := stream.readline():
                    record = _decode(line)
                    if not line.endswith(b"\n") and not record:
                        break
                    identifier = _identifier(record.get("id"))
                    title = record.get("thread_name")
                    if identifier and isinstance(title, str) and title.strip():
                        index.titles.pop(identifier, None)
                        index.titles[identifier] = title.strip()
                        if len(index.titles) > TITLE_INDEX_LIMIT:
                            index.titles.pop(next(iter(index.titles)))
                    index.offset = stream.tell()
        except OSError:
            return
        index.signature = signature

    def _descendants(self, source_id: str, sessions: dict) -> list[_Identity]:
        found = []
        parents = {source_id}
        while children := [session for session in sessions.values() if session.parent in parents and session.id not in parents]:
            found.extend(children)
            parents.update(session.id for session in children)
        return found

    def _listing(self, session: _Identity, sessions: dict, now: datetime) -> dict:
        title = self._title_index.titles.get(session.id) or session.title or session.nickname or session.id
        transcript = self._transcripts.get(session.path)
        return {"id": session.id, "sourceId": session.id, "provider": "codex", "title": title,
                "prompt": "", "project": session.project, "updatedAt": _iso(session.modified),
                "recent": now - session.modified <= RECENT_SESSION_WINDOW,
                "active": session_active(session.modified, now), "chartable": True,
                "taskCount": len(transcript.plan) if transcript is not None else 0,
                "agentCount": len(self._descendants(session.id, sessions)), "headless": False}

    def _read(self, session: _Identity, cache: dict, tail_bytes: int | None) -> _Transcript:
        info = _regular_stat(session.path, strict=True)
        if info is None:
            cache.pop(session.path, None)
            return _Transcript()
        signature = (info.st_dev, info.st_ino)
        transcript = cache.get(session.path)
        reset = (transcript is None or transcript.signature != signature or info.st_size < transcript.size
                 or (info.st_size == transcript.size and info.st_mtime_ns != transcript.modified_ns))
        if reset:
            transcript = _Transcript(signature=signature)
            cache[session.path] = transcript
        try:
            with session.path.open("rb") as stream:
                if transcript.offset and append_checkpoint(stream, transcript.offset) != transcript.checkpoint:
                    transcript = _Transcript(signature=signature)
                    cache[session.path] = transcript
                    reset = True
                start = max(0, info.st_size - tail_bytes) if tail_bytes is not None else 0
                if reset or transcript.offset < start:
                    transcript.offset = start
                    stream.seek(start)
                    if start:
                        stream.readline()
                        transcript.offset = stream.tell()
                stream.seek(transcript.offset)
                while line := stream.readline():
                    record = _decode(line)
                    if not line.endswith(b"\n") and not record:
                        break
                    transcript.consume(record, session.id)
                    transcript.offset = stream.tell()
                transcript.checkpoint = append_checkpoint(stream, transcript.offset)
        except FileNotFoundError:
            return transcript
        transcript.size, transcript.modified_ns = info.st_size, info.st_mtime_ns
        return transcript

    def _agent(self, session: _Identity, transcript: _Transcript, now: datetime, root: bool) -> dict:
        quiet = transcript.last_activity is None or now - transcript.last_activity >= SILENT_AFTER
        status = "silent" if quiet else "running"
        if not transcript.running:
            status = "stopped" if transcript.interrupted else "idle" if transcript.observed_activity else "silent"
        if transcript.runtime_phase == "failed":
            status = "failed"
        description = session.nickname or self._title_index.titles.get(session.id) or session.title or session.id
        return {"id": session.id, "description": description, "agentType": "session" if root else "subagent",
                "worktreeBranch": "", "started": _iso(transcript.started or transcript.first),
                "ended": _iso(transcript.ended), "lastActivityAt": _iso(transcript.last_activity),
                "activity": "Turn failed" if transcript.runtime_phase == "failed" else "Working" if transcript.running
                else "Turn stopped" if transcript.interrupted else "Between turns",
                "status": status}
