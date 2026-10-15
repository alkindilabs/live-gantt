"""Pure rules that report where a session's plan has silently gone out of date.

The chart and the plugin's hooks both judge a plan through ``assess``, so the rules
live only here. Inputs are the normalized task records produced by the source module.
"""

from datetime import datetime, timedelta
import math
from typing import Any


STALE_PROGRESS_MINUTES = 15
ACTIVE_SESSION_MINUTES = 5
SCHEDULING_FIELDS = ("estimate", "lane")
FINISHED_STATUSES = frozenset({"completed", "deleted"})
SECONDS_PER_MINUTE = 60

MISSING_SCHEDULE = "missing-schedule"
STALE_PROGRESS = "stale-progress"
MISSING_PROGRESS = "missing-progress"


def _time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def _whole_minutes(duration: timedelta) -> int:
    return int(duration.total_seconds() // SECONDS_PER_MINUTE)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _missing_schedule(task: dict) -> list[str]:
    lane = task["metadata"].get("lane")
    present = {"estimate": task["estimate"] is not None, "lane": isinstance(lane, str) and bool(lane)}
    return [field for field in SCHEDULING_FIELDS if not present[field]]


def _finding(rule: str, task: dict, message: str) -> dict:
    return {"rule": rule, "taskId": task["id"], "message": message}


def _task_findings(task: dict, session_active: bool, now: datetime) -> list[dict]:
    findings = []
    missing = _missing_schedule(task)
    if missing:
        findings.append(_finding(MISSING_SCHEDULE, task,
                                 f"Task #{task['id']} lacks {' and '.join(missing)} metadata."))
    if task["status"] != "in_progress":
        return findings
    if not _is_number(task["metadata"].get("progress")):
        findings.append(_finding(MISSING_PROGRESS, task,
                                 f"Task #{task['id']} is in progress without a numeric progress value."))
    updated = _time(task["updatedAt"])
    unchanged = now - updated if updated else None
    if session_active and unchanged is not None and unchanged >= timedelta(minutes=STALE_PROGRESS_MINUTES):
        findings.append(_finding(STALE_PROGRESS, task,
                                 f"Task #{task['id']} is in progress but has not been updated "
                                 f"for {_whole_minutes(unchanged)} minutes."))
    return findings


def assess(tasks: list[dict], transcript_updated: datetime | None, now: datetime) -> dict:
    """Judge a session's plan at ``now``.

    ``transcript_updated`` is the parent transcript's last change; the session is active
    when it changed within ``ACTIVE_SESSION_MINUTES``, inclusive. Unreadable task files,
    completed and deleted tasks produce no findings. ``sinceChangeSeconds`` measures
    from the newest task file change and is ``None`` without task files. The result is
    JSON-ready: ``{stale, findings: [{rule, taskId, message}], sinceChangeSeconds,
    sessionActive}``.
    """
    session_active = (transcript_updated is not None
                      and now - transcript_updated <= timedelta(minutes=ACTIVE_SESSION_MINUTES))
    readable = [task for task in tasks if task["readable"]]
    findings = [
        finding
        for task in readable if task["status"] not in FINISHED_STATUSES
        for finding in _task_findings(task, session_active, now)
    ]
    changes = [moment for moment in (_time(task["updatedAt"]) for task in readable) if moment]
    since_change = int((now - max(changes)).total_seconds()) if changes else None
    return {"stale": bool(findings), "findings": findings,
            "sinceChangeSeconds": since_change, "sessionActive": session_active}
