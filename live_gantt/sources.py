"""Read Claude task lists and incremental transcript history without writing sources."""

from __future__ import annotations

import json
import math
import os
import re
import stat
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


UTC = timezone.utc
SILENT_AFTER = timedelta(minutes=30)
IDENTITY_SCAN_BYTES = 1024 * 1024
IDENTITY_SCAN_LINES = 128
TASK_STATUSES = frozenset({"pending", "in_progress", "completed"})
LIVE_AGENT_STATUSES = frozenset({"running", "in_progress", "pending"})
TIMESTAMP_PATTERN = re.compile(rb'"timestamp"\s*:\s*"([^"\\]+)"')
CREATION_PATTERN = re.compile(r"^Task #(\S+) created successfully(?:\b|:)")
NOTIFICATION_PATTERN = re.compile(r"<task-notification>(.*?)</task-notification>", re.DOTALL)
RESULT_LINE_PATTERN = re.compile(rb'"type"\s*:\s*"result"')
AGENT_ID_PATTERN = re.compile(r'(?:agentId|agent_id)\s*[":=]+\s*["\s]*([\w-]+)')
TASK_ID_PATTERN = re.compile(r'(?:task_id|taskId)\s*[":=]+\s*["\s]*([\w-]+)')
RELEVANT_TOKENS = (b'"tool_use"', b'"tool_result"', b"<task-notification>")
TRANSCRIPT_TOKENS = RELEVANT_TOKENS + (b'"timestamp"', b'"cwd"', b'"user"')


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


def _stat(path: Path) -> os.stat_result | None:
    try:
        return path.stat()
    except (OSError, ValueError):
        return None


def _regular_stat(path: Path) -> os.stat_result | None:
    file_stat = _stat(path)
    return file_stat if file_stat and stat.S_ISREG(file_stat.st_mode) else None


def _children(path: Path) -> list[Path]:
    try:
        return list(path.iterdir())
    except OSError:
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
class _Transcript:
    identity: tuple[int, int] | None = None
    offset: int = 0
    size: int = 0
    modified: int = 0
    first: datetime | None = None
    last: datetime | None = None
    activity: str = ""
    title: str = ""
    project: str = ""
    histories: dict[str, _TaskHistory] = field(default_factory=dict)
    notifications: dict[str, tuple[str, datetime]] = field(default_factory=dict)
    agent_aliases: dict[str, str] = field(default_factory=dict)

    def _consume(self, line: bytes) -> None:
        timestamp_match = TIMESTAMP_PATTERN.search(line)
        timestamp = parse_time(timestamp_match.group(1).decode("ascii", errors="ignore")) if timestamp_match else None
        if timestamp:
            self.first = self.first or timestamp
            self.last = timestamp
        needs_identity = not self.title or not self.project
        relevant = any(token in line for token in RELEVANT_TOKENS)
        identity_line = needs_identity and (b'"cwd"' in line or b'"user"' in line)
        if not relevant and not identity_line:
            return
        try:
            record = json.loads(line)
        except (ValueError, UnicodeError):
            return
        if not isinstance(record, dict):
            return
        self._read_identity(record)
        if timestamp is None:
            return
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        for text in _texts(content):
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
                self.activity = description if isinstance(description, str) and description else str(name or "")
                if name == "TaskUpdate":
                    self._read_task_update(inputs, timestamp)
            elif item.get("type") == "tool_result":
                for text in _texts(item.get("content")):
                    creation = CREATION_PATTERN.match(text)
                    if creation:
                        history = self.histories.setdefault(creation.group(1), _TaskHistory())
                        history.created = history.created or timestamp
                        history._record(timestamp, "created")
                    self._read_notifications(text, timestamp)
                    self._read_agent_alias(text)

    def _read_identity(self, record: dict[str, Any]) -> None:
        cwd = record.get("cwd")
        if not self.project and isinstance(cwd, str):
            self.project = cwd
        if self.title or record.get("type") != "user":
            return
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        for text in _texts(content):
            if text.strip() and not text.lstrip().startswith("<task-notification>"):
                self.title = text.strip().splitlines()[0]
                break

    def _read_task_update(self, inputs: dict[str, Any], timestamp: datetime) -> None:
        task_id = _task_id(inputs.get("taskId"))
        if task_id is None:
            return
        history = self.histories.setdefault(task_id, _TaskHistory())
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

    def _read_notifications(self, text: str, timestamp: datetime) -> None:
        for notification in NOTIFICATION_PATTERN.findall(text):
            task_id = re.search(r"<task-id>([^<]+)</task-id>", notification)
            status = re.search(r"<status>([^<]+)</status>", notification)
            if task_id and status:
                self.notifications[task_id.group(1).strip()] = (status.group(1).strip(), timestamp)

    def _read_agent_alias(self, text: str) -> None:
        agent = AGENT_ID_PATTERN.search(text)
        task = TASK_ID_PATTERN.search(text)
        if agent and task:
            self.agent_aliases[task.group(1)] = agent.group(1)


@dataclass
class _Identity:
    identity: tuple[int, int]
    size: int
    modified: int
    title: str = ""
    project: str = ""


@dataclass
class _Journal:
    identity: tuple[int, int] | None = None
    offset: int = 0
    size: int = 0
    modified: int = 0
    finished: int = 0


@dataclass
class _Session:
    id: str
    task_dir: Path | None = None
    transcript: Path | None = None
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
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)

    def __post_init__(self) -> None:
        self.root = Path(self.root).expanduser()

    def lists(self) -> list[dict[str, Any]]:
        """Return all discoverable sessions ordered by newest task or parent-transcript change."""
        with self._lock:
            sessions = sorted(self._sessions().values(), key=lambda session: (-session.modified, session.id))
            return [self._listing(session, []) for session in sessions]

    def state(self, list_id: str, now: datetime) -> dict[str, Any] | None:
        """Return normalized tasks and agents, or None for an undiscovered session identifier."""
        with self._lock:
            session = self._sessions().get(list_id)
            if session is None:
                return None
            warnings: list[str] = []
            transcript = self._transcript(session.transcript) if session.transcript else _Transcript()
            listing = self._listing(session, warnings)
            tasks = [self._task(path, transcript) for path in self._task_paths(session.task_dir)]
            agents = self._agents(session, transcript, now, warnings)
            assigned: set[str] = set()
            for task in tasks:
                agent_ids = task.pop("agentIds")
                task["agents"] = [agents[agent_id] for agent_id in agent_ids if agent_id in agents]
                assigned.update(agent_ids)
            return {**listing, "tasks": tasks, "agents": [agent for key, agent in agents.items() if key not in assigned], "warnings": warnings}

    def _sessions(self) -> dict[str, _Session]:
        sessions: dict[str, _Session] = {}
        for task_dir in _children(self.root / "tasks"):
            directory_stat = _stat(task_dir)
            if directory_stat is None or not stat.S_ISDIR(directory_stat.st_mode):
                continue
            session = sessions.setdefault(task_dir.name, _Session(task_dir.name))
            session.task_dir = task_dir
            for path in self._task_paths(task_dir):
                file_stat = _stat(path)
                if file_stat:
                    session.modified = max(session.modified, file_stat.st_mtime)
        for project_dir in _children(self.root / "projects"):
            for path in _children(project_dir):
                if path.suffix != ".jsonl":
                    continue
                file_stat = _stat(path)
                if file_stat is None or not stat.S_ISREG(file_stat.st_mode):
                    continue
                session = sessions.setdefault(path.stem, _Session(path.stem))
                previous_stat = _stat(session.transcript) if session.transcript else None
                if previous_stat is None or file_stat.st_mtime >= previous_stat.st_mtime:
                    session.transcript = path
                session.modified = max(session.modified, file_stat.st_mtime)
        return sessions

    def _task_paths(self, task_dir: Path | None) -> list[Path]:
        paths = [path for path in _children(task_dir) if path.suffix == ".json" and _regular_stat(path)] if task_dir else []
        return sorted(paths, key=lambda path: path.name)

    def _listing(self, session: _Session, warnings: list[str]) -> dict[str, Any]:
        identity = self._identity(session.transcript) if session.transcript else None
        title = identity.title if identity else ""
        if session.transcript:
            custom_title_path = session.transcript.with_suffix("") / "custom-title.json"
            if _stat(custom_title_path):
                custom_title = _read_json(custom_title_path, warnings).get("customTitle")
                if isinstance(custom_title, str) and custom_title.strip():
                    title = custom_title.strip()
        updated = datetime.fromtimestamp(session.modified, UTC) if session.modified else None
        return {"id": session.id, "title": title or session.id,
                "project": identity.project if identity else "", "updatedAt": _iso(updated)}

    def _identity(self, path: Path) -> _Identity | None:
        file_stat = _regular_stat(path)
        if file_stat is None:
            return None
        identity = (file_stat.st_dev, file_stat.st_ino)
        cached = self._identities.get(path)
        same_file = cached and cached.identity == identity and file_stat.st_size >= cached.size
        unchanged = same_file and cached.size == file_stat.st_size and cached.modified == file_stat.st_mtime_ns
        known = same_file and cached.title and cached.project and file_stat.st_size > cached.size
        if unchanged or known:
            cached.size, cached.modified = file_stat.st_size, file_stat.st_mtime_ns
            return cached
        parsed = _Transcript()
        try:
            with path.open("rb") as stream:
                remaining = IDENTITY_SCAN_BYTES
                for _ in range(IDENTITY_SCAN_LINES):
                    line = stream.readline(remaining + 1)
                    remaining -= len(line)
                    if remaining < 0 or not line:
                        break
                    if b'"cwd"' in line or b'"user"' in line:
                        try:
                            record = json.loads(line)
                        except (ValueError, UnicodeError):
                            continue
                        if isinstance(record, dict):
                            parsed._read_identity(record)
                    if parsed.title and parsed.project:
                        break
        except OSError:
            return cached
        cached = _Identity(identity, file_stat.st_size, file_stat.st_mtime_ns, parsed.title, parsed.project)
        self._identities[path] = cached
        return cached

    def _transcript(self, path: Path) -> _Transcript:
        file_stat = _regular_stat(path)
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
                stream.seek(cached.offset)
                while line := stream.readline():
                    if not _line_is_complete(line, TRANSCRIPT_TOKENS):
                        break
                    cached._consume(line)
                    cached.offset = stream.tell()
        except OSError:
            return cached
        cached.size, cached.modified = file_stat.st_size, file_stat.st_mtime_ns
        self._identities[path] = _Identity(identity, cached.size, cached.modified, cached.title, cached.project)
        return cached

    def _task(self, path: Path, transcript: _Transcript) -> dict[str, Any]:
        warnings: list[str] = []
        document = _read_json(path, warnings)
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
        finished = self._finished(path)
        if finished is None:
            warnings.append("live journal is unavailable")
            return 0
        return start + (end - start) * min(finished / planned, 1)

    def _finished(self, path: Path) -> int | None:
        file_stat = _regular_stat(path)
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
                stream.seek(cached.offset)
                while line := stream.readline():
                    if not _line_is_complete(line, (b'"result"',)):
                        break
                    if RESULT_LINE_PATTERN.search(line):
                        cached.finished += 1
                    cached.offset = stream.tell()
        except OSError:
            return None
        cached.size, cached.modified = file_stat.st_size, file_stat.st_mtime_ns
        return cached.finished

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
            invalid = (not isinstance(label, str) or not label or started is None
                       or (value.get("ended") is not None and ended is None)
                       or (ended is not None and started is not None and ended < started)
                       or (log is not None and (not isinstance(log, str) or not log)))
            if invalid:
                warnings.append(f"runs[{index}] has invalid label, timestamps, or log path")
                continue
            last_activity = ended or started
            if log:
                log_stat = _regular_stat(self._path(log, task_dir))
                if log_stat:
                    last_activity = datetime.fromtimestamp(log_stat.st_mtime, UTC)
                else:
                    warnings.append(f"runs[{index}] log is unavailable")
            runs.append({"label": label, "started": _iso(started), "ended": _iso(ended),
                         "log": log, "lastActivityAt": _iso(last_activity), "activity": label,
                         "status": "completed" if ended else "running"})
        return runs

    def _agents(self, session: _Session, parent: _Transcript, now: datetime, warnings: list[str]) -> dict[str, dict[str, Any]]:
        if session.transcript is None:
            return {}
        directory = session.transcript.with_suffix("") / "subagents"
        agent_ids: set[str] = set()
        for path in _children(directory):
            if path.name.startswith("agent-") and path.name.endswith((".jsonl", ".meta.json")):
                suffix = ".meta.json" if path.name.endswith(".meta.json") else ".jsonl"
                agent_ids.add(path.name[len("agent-"):-len(suffix)])
        notifications: dict[str, tuple[str, datetime]] = {}
        for task_id, notification in parent.notifications.items():
            notifications[parent.agent_aliases.get(task_id, task_id)] = notification
        agents: dict[str, dict[str, Any]] = {}
        for agent_id in sorted(agent_ids):
            path = directory / f"agent-{agent_id}.jsonl"
            transcript = self._transcript(path)
            meta_path = directory / f"agent-{agent_id}.meta.json"
            metadata = _read_json(meta_path, warnings) if _stat(meta_path) else {}
            notification = notifications.get(agent_id)
            status, notification_time = notification if notification else ("running", None)
            ended = transcript.last or notification_time
            if status in LIVE_AGENT_STATUSES:
                ended = None
                status = "silent" if transcript.last and now - transcript.last >= SILENT_AFTER else "running"
            agents[agent_id] = {
                "id": agent_id, "description": self._text(metadata, "description", agent_id, warnings),
                "agentType": self._text(metadata, "agentType", "", warnings),
                "worktreeBranch": self._text(metadata, "worktreeBranch", "", warnings),
                "started": _iso(transcript.first), "ended": _iso(ended),
                "lastActivityAt": _iso(transcript.last), "activity": transcript.activity,
                "status": status,
            }
        return agents
