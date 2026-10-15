"""Keep the session's plan fresh: block a stale Stop once, remind on prompts and tool calls.

The hook reads the session's task files, its transcript's modification time and the run
logs and processes those tasks declare. It writes only its own per-session state file
under the plugin data directory, prints to the session and always exits successfully.
"""

import json
import os
import platform
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT))

from live_gantt.freshness import assess
from live_gantt.installation import installation_directory
from live_gantt.sources import LIST_ID, SourceStore, default_root


BLOCK_COOLDOWN_MINUTES = 5
MID_TURN_REMINDER_MINUTES = 5
DATA_DIRECTORY_VARIABLE = "CLAUDE_PLUGIN_DATA"
STATE_DIRECTORY = "freshness"
BLOCKED_AT = "blockedAt"
REMINDED_AT = "remindedAt"


def _data_directory() -> Path:
    configured = os.environ.get(DATA_DIRECTORY_VARIABLE)
    if configured:
        return Path(configured)
    return installation_directory(platform.system(), Path.home(), os.environ)


def _state_path(session_id: str) -> Path:
    return _data_directory() / STATE_DIRECTORY / f"{session_id}.json"


def _read_state(path: Path) -> dict:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _record(path: Path, state: dict, key: str, now: datetime) -> bool:
    """Atomically store ``now`` under ``key``; report whether the cooldown can be honoured."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        staged = path.with_name(f".{path.name}.{os.getpid()}")
        staged.write_text(json.dumps({**state, key: now.isoformat()}), encoding="utf-8")
        os.replace(staged, path)
    except OSError:
        return False
    return True


def _cooling_down(state: dict, key: str, minutes: int, now: datetime) -> bool:
    try:
        previous = datetime.fromisoformat(state[key])
    except (KeyError, TypeError, ValueError):
        return False
    return previous.tzinfo is not None and now - previous < timedelta(minutes=minutes)


def _transcript_updated(transcript_path: object) -> datetime | None:
    if not isinstance(transcript_path, str) or not transcript_path:
        return None
    try:
        return datetime.fromtimestamp(Path(transcript_path).stat().st_mtime, timezone.utc)
    except (OSError, ValueError):
        return None


def _findings(session_id: str, transcript_path: object, now: datetime) -> list[dict]:
    tasks = SourceStore(default_root()).plan(session_id)
    return assess(tasks, _transcript_updated(transcript_path), now)["findings"]


def _reminder(findings: list[dict]) -> str:
    messages = " ".join(finding["message"] for finding in findings)
    return (f"Live Gantt shows this session's plan as stale. {messages} "
            "Fix each with TaskUpdate as the live-gantt skill describes, so the chart stays true.")


def _context(event_name: str, findings: list[dict]) -> dict:
    return {"hookSpecificOutput": {"hookEventName": event_name, "additionalContext": _reminder(findings)}}


def respond(event: object, now: datetime) -> dict | None:
    """Return the hook output for one event, or None to stay silent."""
    if not isinstance(event, dict):
        return None
    session_id = event.get("session_id")
    if not isinstance(session_id, str) or not LIST_ID.fullmatch(session_id):
        return None
    event_name = event.get("hook_event_name")
    transcript_path = event.get("transcript_path")
    if event_name == "UserPromptSubmit":
        findings = _findings(session_id, transcript_path, now)
        return _context(event_name, findings) if findings else None
    if event_name == "Stop":
        if event.get("stop_hook_active") is True:
            return None
        key, minutes = BLOCKED_AT, BLOCK_COOLDOWN_MINUTES
    elif event_name == "PostToolUse":
        key, minutes = REMINDED_AT, MID_TURN_REMINDER_MINUTES
    else:
        return None
    path = _state_path(session_id)
    state = _read_state(path)
    if _cooling_down(state, key, minutes, now):
        return None
    findings = _findings(session_id, transcript_path, now)
    if not findings or not _record(path, state, key, now):
        return None
    if event_name == "Stop":
        return {"decision": "block", "reason": _reminder(findings)}
    return _context(event_name, findings)


def main() -> None:
    """Answer one hook event on standard input; any failure leaves the session undisturbed."""
    try:
        output = respond(json.load(sys.stdin), datetime.now(timezone.utc))
        if output is not None:
            print(json.dumps(output))
    except Exception:
        return


if __name__ == "__main__":
    main()
