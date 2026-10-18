"""Pure rules that report where a session's plan has silently gone out of date.

The chart and the plugin's hooks both judge a plan through ``assess``, so the rules
live only here. Inputs are the normalized task records produced by the source module.
"""

from datetime import datetime, timedelta
import math
from typing import Any


STALE_PROGRESS_MINUTES = 15
ACTIVE_SESSION_MINUTES = 5
SILENT_RUN_MINUTES = 10
SCHEDULING_FIELDS = ("estimate", "lane")
FINISHED_STATUSES = frozenset({"completed", "deleted"})
SECONDS_PER_MINUTE = 60

MISSING_SCHEDULE = "missing-schedule"
STALE_PROGRESS = "stale-progress"
MISSING_PROGRESS = "missing-progress"
SILENT_RUN = "silent-run"
PROCESS_GONE = "process-gone"


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


def _run_finding(rule: str, task: dict, message: str, observed: str) -> dict:
    return {**_finding(rule, task, message), "observed": observed}


def _run_findings(task: dict, run: dict, session_active: bool, now: datetime) -> list[dict]:
    if run["ended"]:
        return []
    name = f"Run '{run['label']}'"
    log_updated = _time(run["logUpdatedAt"])
    silence = now - log_updated if log_updated else None
    log_missing = run["log"] is not None and log_updated is None
    log_silent = silence is not None and silence >= timedelta(minutes=SILENT_RUN_MINUTES)
    log_shows_no_life = run["log"] is None or log_missing or log_silent
    if run["pidAlive"] is False and log_shows_no_life:
        return [_run_finding(PROCESS_GONE, task,
                             f"{name} (pid {run['pid']}) of task #{task['id']} is no longer running but has not ended.",
                             "process gone")]
    if log_missing:
        return [_run_finding(SILENT_RUN, task, f"{name} of task #{task['id']}: log not found at {run['log']}.",
                             "log not found")]
    if log_silent and session_active:
        minutes = _whole_minutes(silence)
        return [_run_finding(SILENT_RUN, task,
                             f"{name} of task #{task['id']} has written nothing to its log for {minutes} minutes.",
                             f"silent {minutes} min")]
    return []


def _task_findings(task: dict, session_active: bool, now: datetime) -> list[dict]:
    findings = [finding for run in task["runs"] for finding in _run_findings(task, run, session_active, now)]
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


def session_active(transcript_updated: datetime | None, now: datetime) -> bool:
    """Whether a session is active: its parent transcript changed within ``ACTIVE_SESSION_MINUTES``, inclusive."""
    return transcript_updated is not None and now - transcript_updated <= timedelta(minutes=ACTIVE_SESSION_MINUTES)


def assess(tasks: list[dict], transcript_updated: datetime | None, now: datetime) -> dict:
    """Judge a session's plan at ``now``.

    ``transcript_updated`` is the parent transcript's last change, judged by ``session_active``. Unreadable task files,
    completed and deleted tasks produce no findings. A run that has not ended is stale when
    its log is missing, when its log has not changed for ``SILENT_RUN_MINUTES`` while the
    session is active, or when its declared process has exited and its log, if any, shows
    no life; a log still being written outweighs an exited or reused process identifier. ``sinceChangeSeconds`` measures
    from the newest task file change and is ``None`` without task files. The result is
    JSON-ready: ``{stale, findings: [{rule, taskId, message}], sinceChangeSeconds,
    sessionActive}``; run findings add ``observed``, a short phrase for what was seen.
    """
    active = session_active(transcript_updated, now)
    readable = [task for task in tasks if task["readable"]]
    findings = [
        finding
        for task in readable if task["status"] not in FINISHED_STATUSES
        for finding in _task_findings(task, active, now)
    ]
    changes = [moment for moment in (_time(task["updatedAt"]) for task in readable) if moment]
    since_change = int((now - max(changes)).total_seconds()) if changes else None
    return {"stale": bool(findings), "findings": findings,
            "sinceChangeSeconds": since_change, "sessionActive": active}
