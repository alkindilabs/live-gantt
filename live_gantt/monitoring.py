"""Derive one actionable session summary from independently observed evidence."""

from datetime import timedelta

from .freshness import ACTIVE_SESSION_MINUTES, STALE_PROGRESS_MINUTES, SILENT_RUN_MINUTES, MISSING_SCHEDULE
from .sources import parse_time


MAX_ISSUES = 8
ACTIVE_PHASES = frozenset({"working", "waiting", "idle"})
CHILD_ACTIVE_STATUSES = frozenset({"running", "in_progress", "silent"})
FAILURE_PHASES = frozenset({"failed", "interrupted"})
FAILED_WORK_STATUSES = frozenset({"failed", "error", "killed", "stopped", "interrupted"})


def _recent(value, now):
    stamp = parse_time(value)
    return stamp is not None and timedelta() <= now - stamp < timedelta(minutes=ACTIVE_SESSION_MINUTES)


def _background(model, now):
    agents = [agent for agent in model.get("agents", []) if agent.get("agentType") != "session"]
    runs = []
    for task in model.get("tasks", []):
        agents.extend(task.get("agents", []))
        runs.extend(task.get("runs", []))
    for workflow in model.get("workflows", []):
        agents.extend(workflow.get("agents", []))
    pending_agents = [agent for agent in agents if agent.get("status") in CHILD_ACTIVE_STATUSES]
    pending_runs = [run for run in runs if not run.get("ended")]
    pending_workflows = [flow for flow in model.get("workflows", []) if flow.get("status") in CHILD_ACTIVE_STATUSES]
    times = [parse_time(agent.get("lastActivityAt")) for agent in pending_agents]
    problems = []
    if any(agent.get("status") in FAILED_WORK_STATUSES for agent in agents):
        problems.append("A recorded agent failed or stopped before completion.")
    if any(flow.get("status") in FAILED_WORK_STATUSES for flow in model.get("workflows", [])):
        problems.append("A recorded workflow failed or stopped before completion.")
    for run in pending_runs:
        logged = parse_time(run.get("logUpdatedAt"))
        recent_log = logged is not None and timedelta() <= now - logged < timedelta(minutes=SILENT_RUN_MINUTES)
        if run.get("pidAlive") is False and not recent_log:
            problems.append("An external process exited while its run remains unfinished.")
        times.append(logged or (parse_time(run.get("lastActivityAt")) if run.get("pidAlive") is True else None))
    activity = max((stamp for stamp in times if stamp is not None and stamp <= now), default=None)
    return bool(pending_agents or pending_runs or pending_workflows), activity, problems


def _quota_wait(tasks, now):
    for task in tasks:
        if task.get("status") == "completed":
            continue
        for delay in task.get("quotaDelays", []):
            start, end = parse_time(delay.get("start")), parse_time(delay.get("end"))
            if delay.get("reason") == "reached" and start is not None and start <= now and (end is None or now < end):
                return True
    return False


def summarize(model, now, *, runtime=None, native=None):
    """Return a monitor state without treating successful reads as work progress.

    Runtime fields are private reader evidence. Native phases require a verified
    process binding. Failed checks, unresolved failures and missing activity can
    never produce a working state. Inputs are not modified.
    """
    runtime, native = runtime or {}, native or {}
    tasks = model.get("tasks", [])
    pending, background_activity, background_problems = _background(model, now)
    background_recent = background_activity is not None and _recent(background_activity.isoformat(), now)
    stamps = [parse_time(value) for value in (runtime.get("lastActivityAt"), native.get("lastActivityAt"))]
    stamps.append(background_activity)
    activity = max((stamp for stamp in stamps if stamp is not None and stamp <= now), default=None)
    last_activity = activity.isoformat() if activity else None
    recent = _recent(last_activity, now)
    phase = runtime.get("phase", "unknown")
    native_phase = native.get("phase", "unknown")
    if native_phase not in ACTIVE_PHASES:
        native_phase = "unknown"
    effective_phase = native_phase if native_phase != "unknown" else phase
    liveness = model.get("liveness", {})
    owner = liveness.get("status", "unknown")
    issues = []
    if phase in FAILURE_PHASES or phase == "unknown" and runtime.get("detail"):
        issues.append(runtime.get("detail") or "The last recorded turn did not finish successfully.")
    for finding in model.get("freshness", {}).get("findings", []):
        if finding.get("rule") != MISSING_SCHEDULE and finding.get("message"):
            issues.append(finding["message"])
    for task in tasks:
        updated = parse_time(task.get("updatedAt"))
        if (task.get("status") == "in_progress" and updated is not None
                and now - updated >= timedelta(minutes=STALE_PROGRESS_MINUTES)):
            issues.append(f"Task {task.get('id', '?')} has no recorded plan update for {STALE_PROGRESS_MINUTES} minutes or more.")
    issues.extend(background_problems)

    def result(state, summary, extra=None):
        messages = ([extra] if extra else []) + issues
        return {"state": state, "summary": summary, "lastActivityAt": last_activity,
                "issues": list(dict.fromkeys(messages))[:MAX_ISSUES]}

    if model.get("sync", {}).get("status") != "current":
        return result("unverified", "Session updates are unavailable; retrying automatically.",
                      "The last saved view may be out of date.")
    if phase in FAILURE_PHASES and not (owner == "alive" and native_phase == "waiting"):
        return result("attention", "The last recorded turn failed or was interrupted.")
    complete = bool(tasks) and all(task.get("status") == "completed" for task in tasks)
    if owner == "exited":
        if complete and phase == "idle" and not pending and not issues:
            return result("plan_complete", "All recorded tasks are complete; the harness has exited.")
        return result("attention", "The harness exited before completion could be confirmed.")
    if owner != "alive":
        return result("unverified", "The session's runtime ownership cannot be verified.",
                      "Recent file changes alone do not establish that the harness is running.")
    if native_phase == "waiting":
        return result("needs_you", native.get("detail") or "The harness is waiting for your input or approval.")
    if native_phase == "unknown" and native.get("detail"):
        return result("unverified", native["detail"])
    if phase == "unknown" and runtime.get("detail"):
        return result("attention", runtime["detail"])
    recorded_at = parse_time(runtime.get("lastActivityAt"))
    native_at = parse_time(native.get("lastActivityAt"))
    if ({phase, native_phase} == {"working", "idle"} and recorded_at is not None
            and native_at is not None and native_at < recorded_at <= now):
        native_observed = (parse_time(liveness.get("checkedAt"))
                           if model.get("provider") == "claude" and liveness.get("evidence") == "native_registry_process"
                           else None)
        if native_observed is not None and native_observed < recorded_at:
            effective_phase = phase
        else:
            return result("attention", "Native status conflicts with newer recorded execution.",
                          "Conflicting harness and transcript signals prevent a reliable working status.")
    if background_problems:
        return result("attention", "Recorded background work did not finish successfully.")
    if pending and effective_phase == "idle":
        if background_recent:
            if issues:
                return result("attention", "Background execution continues, but the recorded plan needs attention.")
            return result("working", "Background work has recent execution activity.")
        return result("unverified", "Background work remains open without recent execution activity.",
                      "The harness is idle; continuing background progress cannot be confirmed.")
    quota_wait = _quota_wait(tasks, now)
    if quota_wait and not pending and not (effective_phase == "working" and recent):
        return result("waiting_for_quota", "A confirmed quota limit is holding planned work until reset.")
    if effective_phase == "idle":
        if complete and not pending:
            return result("plan_complete", "All recorded tasks are complete; no continuing work is recorded.")
        if any(task.get("status") == "in_progress" for task in tasks):
            return result("attention", "The harness is idle while tasks remain marked in progress.")
        return result("idle", "The last turn ended; remaining planned work has not started." if tasks
                      else "The harness is idle; no active plan is recorded.")
    if not recent:
        return result("unverified", "The harness is alive, but recent execution activity is missing.",
                      f"No meaningful execution update was observed in the last {ACTIVE_SESSION_MINUTES} minutes.")
    if issues:
        return result("attention", "Execution was observed, but the recorded plan needs attention.")
    if effective_phase == "working" or background_recent:
        return result("working", "Execution continues; some planned work waits for a quota reset." if quota_wait
                      else "Recent execution activity is confirmed for this session.")
    return result("unverified", "The harness is alive, but its execution state is not confirmed.")
