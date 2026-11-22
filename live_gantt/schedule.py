"""Pure lane scheduling and milestone forecasts for normalized source records."""

from collections import defaultdict, deque
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
import math
import re

from .usage import PERCENT_LIMIT, STALE_AFTER, _date, _number


_PERCENT = 100
_PERT_DIVISOR = 6
_P90_QUANTILE = 1.2816
_ZERO = 0.0
_SINGLE_RUNNING_TASK = 1
_NUMERIC_ID_PART = 0
_TEXT_ID_PART = 1
_ID_PARTS = re.compile(r"([0-9]+)")
_PROVIDERS = ("claude", "codex")
_EXTERNAL = "external"
_SECONDS_PER_HOUR = timedelta(hours=1).total_seconds()
_MILLISECOND = timedelta(milliseconds=1)


@dataclass(frozen=True)
class _Forecast:
    end: datetime | None
    path: tuple[str, ...] = ()
    unestimated: bool = False
    anchored: bool = False
    quota_wait: timedelta | None = None


@dataclass(frozen=True)
class _Outage:
    start: datetime
    end: datetime | None
    reason: str


@dataclass
class _Work:
    end: datetime | None
    segments: list
    delays: list


def _time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _id_key(value: str) -> tuple:
    parts = []
    for part in _ID_PARTS.split(value):
        if part.isascii() and part.isdecimal():
            digits = part.lstrip("0") or "0"
            parts.append((_NUMERIC_ID_PART, len(digits), digits))
        else:
            parts.append((_TEXT_ID_PART, part.casefold()))
    return tuple(parts), value


def _enrich(task: dict, now: datetime) -> dict:
    enriched = dict(task)
    enriched.update(
        warnings=list(task["warnings"]), markers=[], projectedStart=None,
        projectedEnd=None, overrun=False, overrunAt=None, blocked=False,
        queued=False, waitingFor=[],
        projectedSegments=[], quotaDelays=[], projectionAnchored=False,
        effectiveProgress=max(task["progress"], task["liveProgress"]),
        estimateMean=None, estimateSd=None,
    )
    estimate = task["estimate"]
    if estimate is not None:
        enriched["estimateMean"] = (
            estimate["likely"]
            + (estimate["low"] - estimate["likely"]) / _PERT_DIVISOR
            + (estimate["high"] - estimate["likely"]) / _PERT_DIVISOR
        )
        enriched["estimateSd"] = (estimate["high"] - estimate["low"]) / _PERT_DIVISOR
    if task["status"] != "pending" and (
        not task["started"] or (task["status"] == "completed" and not task["ended"])
    ):
        enriched["markers"].append("no history")
    if task["status"] != "completed" and estimate is None:
        enriched["markers"].append("unestimated")
    started = _time(task["started"])
    if (task["status"] == "in_progress" and started and estimate is not None
            and enriched["effectiveProgress"] == _ZERO):
        overrun_at = _calendar_end(started, enriched["estimateMean"])
        if overrun_at is not None:
            enriched["overrunAt"] = overrun_at.isoformat()
            enriched["overrun"] = now > overrun_at
    return enriched


def _variance(path: tuple[str, ...], variances: dict[str, float]) -> float:
    try:
        return math.fsum(variances[task_id] for task_id in path)
    except OverflowError:
        return math.inf


def _controlling_forecast(forecasts: list[_Forecast], variances: dict[str, float]) -> _Forecast:
    latest_end = max(forecast.end for forecast in forecasts)
    return max(
        (forecast for forecast in forecasts if forecast.end == latest_end),
        key=lambda forecast: _variance(forecast.path, variances),
    )


def _calendar_end(start: datetime, hours: float) -> datetime | None:
    try:
        return start + timedelta(hours=hours)
    except OverflowError:
        return None


def _warn(task: dict, message: str, warnings: list[str]) -> None:
    task["warnings"].append(message)
    warnings.append(f"Task {task['id']}: {message}")


def _quota_outages(usage, now):
    outages = {provider: [] for provider in _PROVIDERS}
    providers = usage.get("providers", []) if isinstance(usage, dict) else []
    if not isinstance(providers, list):
        return outages
    for account in providers:
        if not isinstance(account, dict) or account.get("provider") not in _PROVIDERS:
            continue
        provider = account["provider"]
        windows = account.get("windows", [])
        if not isinstance(windows, list):
            continue
        for window in windows:
            if not isinstance(window, dict) or window.get("bucketId") != provider or window.get("stale") is not False:
                continue
            observed = _date(window.get("measuredAt")) or _date(window.get("observedAt"))
            if observed is None or observed > now or now - observed > STALE_AFTER:
                continue
            reset = _date(window.get("resetsAt"))
            if reset is not None and reset <= now:
                continue
            percent = window.get("usedPercent")
            if not _number(percent) or not 0 <= percent <= PERCENT_LIMIT:
                continue
            if percent == PERCENT_LIMIT:
                outages[provider].append(_Outage(now, reset, "reached"))
                continue
            forecast = window.get("forecast")
            if not isinstance(forecast, dict) or forecast.get("status") != "predicted":
                continue
            exhaustion = _date(forecast.get("exhaustsAt"))
            if exhaustion is not None and reset is not None and now < exhaustion < reset:
                outages[provider].append(_Outage(exhaustion, reset, "predicted"))
    for provider, intervals in outages.items():
        merged = []
        for interval in sorted(intervals, key=lambda item: item.start):
            if merged and (merged[-1].end is None or interval.start <= merged[-1].end):
                previous = merged[-1]
                end = max(previous.end, interval.end) if previous.end is not None and interval.end is not None else None
                reason = "reached" if "reached" in (previous.reason, interval.reason) else "predicted"
                merged[-1] = _Outage(previous.start, end, reason)
            else:
                merged.append(interval)
        outages[provider] = merged
    return outages


def _task_provider(task, provider, warnings):
    metadata = task.get("metadata", {})
    if "provider" in metadata:
        override = metadata["provider"]
        if isinstance(override, str) and override in (*_PROVIDERS, _EXTERNAL):
            return override
        _warn(task, "Invalid provider override; using the session provider", warnings)
    elif task.get("runs") and not task.get("agents"):
        return _EXTERNAL
    return provider if provider in _PROVIDERS else None


def _schedule_work(start, hours, intervals, provider):
    cursor = start
    segments = []
    delays = []
    if hours == _ZERO:
        return _Work(start, [{"start": start.isoformat(), "end": start.isoformat()}], [])
    for interval in intervals:
        if interval.end is not None and interval.end <= cursor:
            continue
        finish = _calendar_end(cursor, hours)
        if finish is not None and finish <= interval.start:
            break
        if cursor < interval.start:
            segments.append({"start": cursor.isoformat(), "end": interval.start.isoformat()})
            hours -= (interval.start - cursor).total_seconds() / _SECONDS_PER_HOUR
            cursor = interval.start
        delays.append({"start": cursor.isoformat(), "end": interval.end.isoformat() if interval.end else None,
                       "provider": provider, "reason": interval.reason})
        if interval.end is None:
            return _Work(None, segments, delays)
        cursor = interval.end
    finish = _calendar_end(cursor, hours)
    if finish is not None:
        segments.append({"start": cursor.isoformat(), "end": finish.isoformat()})
    return _Work(finish, segments, delays)


def _work_forecast(controlling, path, work):
    quota_wait = controlling.quota_wait
    if quota_wait is not None and work.end is not None:
        for delay in work.delays:
            quota_wait += _time(delay["end"]) - _time(delay["start"])
    return _Forecast(work.end, path, anchored=controlling.anchored or bool(work.delays), quota_wait=quota_wait)


def _upper_forecast(task, mean_forecast, predecessors, variances, origin, intervals, provider):
    if mean_forecast.end is None or any(item.end is None for item in predecessors):
        return _Forecast(None, anchored=mean_forecast.anchored or any(item.anchored for item in predecessors))
    controlling = _controlling_forecast([origin] + predecessors, variances)
    path = (*controlling.path, task["id"])
    before = math.sqrt(_variance(controlling.path, variances))
    after = math.sqrt(_variance(path, variances))
    remaining = 1 - task["effectiveProgress"] / _PERCENT if task["status"] == "in_progress" else 1
    extra = _P90_QUANTILE * (after - before)
    if not math.isfinite(extra):
        return _Forecast(None, path, anchored=controlling.anchored)
    work = _schedule_work(controlling.end, task["estimateMean"] * remaining + extra, intervals, provider)
    upper = _work_forecast(controlling, path, work)
    if upper.anchored or mean_forecast.anchored:
        return upper
    end = _calendar_end(mean_forecast.end, _P90_QUANTILE * math.sqrt(_variance(mean_forecast.path, variances)))
    return replace(mean_forecast, end=end)


def _milestone_forecast(remaining, completed_end, variances):
    if not remaining:
        return _Forecast(completed_end)
    if any(item.end is None for item in remaining):
        return _Forecast(None, anchored=any(item.anchored for item in remaining))
    controlling = _controlling_forecast(remaining, variances)
    if completed_end is not None and completed_end > controlling.end:
        return _Forecast(completed_end)
    anchored = any(item.end == controlling.end and item.anchored for item in remaining)
    return replace(controlling, anchored=anchored)


def _forecast_durations(forecast, now):
    if forecast.end is None or forecast.quota_wait is None:
        return None
    elapsed_ms = (forecast.end - now) // _MILLISECOND
    wait_ms = forecast.quota_wait // _MILLISECOND
    return {"workMs": elapsed_ms - wait_ms, "quotaWaitMs": wait_ms}


def _milestones(
    tasks: list[dict], forecasts: dict[str, _Forecast], upper_forecasts: dict[str, _Forecast],
    variances: dict[str, float], warnings: list[str], now: datetime,
) -> list[dict]:
    groups = defaultdict(list)
    projected_work = set()
    for task in tasks:
        groups[task["milestone"]].append(task)
        if any(segment["start"] != segment["end"] for segment in task["projectedSegments"]):
            projected_work.add(task["id"])
    milestones = []
    for name, members in groups.items():
        largest_weight = max(task["weight"] for task in members)
        total_weight = math.fsum(task["weight"] / largest_weight for task in members)
        completed_weight = math.fsum(
            task["weight"] / largest_weight * (
                1 if task["status"] == "completed"
                else task["progress"] / _PERCENT if task["status"] == "in_progress"
                else _ZERO
            )
            for task in members
        )
        remaining = sorted((task for task in members if task["status"] != "completed"),
                           key=lambda task: _id_key(task["id"]))
        completed_end = max((_time(task["ended"]) for task in members
                             if task["status"] == "completed" and task["ended"]), default=None)
        p50 = _milestone_forecast([forecasts[task["id"]] for task in remaining], completed_end, variances)
        p90 = _milestone_forecast([upper_forecasts[task["id"]] for task in remaining], completed_end, variances)
        if p50.end is not None and p90.end is None:
            for task in members:
                if upper_forecasts[task["id"]].end is None:
                    _warn(task, "P90 forecast exceeds the supported calendar range", warnings)
        milestones.append({
            "name": name, "progress": completed_weight / total_weight * _PERCENT,
            "hasRemaining": bool(remaining),
            "hasEstimate": any(task["estimate"] is not None for task in remaining),
            "p50": p50.end.isoformat() if p50.end else None,
            "p90": p90.end.isoformat() if p90.end else None,
            "p50Anchored": p50.anchored, "p90Anchored": p90.anchored,
            "criticalPathTaskIds": list(p50.path) if not projected_work.isdisjoint(p50.path) else [],
            "forecastDurations": {"p50": _forecast_durations(p50, now), "p90": _forecast_durations(p90, now)},
        })
    return milestones


def project(tasks: list[dict], now: datetime, *, provider=None, usage=None) -> dict:
    """Project normalized tasks without mutating input records.

    Running work starts its remaining forecast at ``now``. Pending work waits
    for its lane predecessor and explicit blockers. Missing estimates or
    unresolved dependencies suppress the affected forecast. Running work
    reserves its lane before pending work, including tasks with earlier IDs.
    Multiple running tasks remain visible and produce a warning. Completed
    work never interrupts the chain of remaining tasks in a lane and is
    available immediately when its completion timestamp is unknown.

    Queued tasks identify unfinished prerequisites whose forecasts end after
    ``now``. Missing prerequisites and cycles mark affected tasks blocked;
    unknown estimates retain their separate markers. Milestone estimates
    describe remaining work, excluding completed tasks.

    Forecast uncertainty follows the prerequisite with the latest mean end,
    breaking ties by higher variance and then natural task ID order. Milestone
    ``criticalPathTaskIds`` follows the same ordering to select the unfinished
    task that reaches the expected finish and lists its chain in prerequisite
    order. It is empty when no such chain contains projected work.
    Each ``forecastDurations`` entry divides the time from ``now`` to that
    percentile's finish into work and modeled quota wait in milliseconds.
    A missing finish, a completed milestone or an interval attributable only
    to a future recorded completion leaves that percentile's entry null.
    Weighted milestone completion uses explicit progress, while forecast
    duration uses the larger of explicit and live-journal progress.

    ``provider`` identifies the session provider. A task's ``metadata.provider``
    can override it with ``claude``, ``codex`` or ``external``; unassigned external
    runs default to external. Fresh shared-account windows in ``usage`` define
    unavailable intervals. Work pauses across those intervals without changing
    recorded activity, and an exhausted quota without a reset leaves dependent
    dates unknown. Model-specific quota buckets do not constrain this projection.

    ``projectedSegments`` contain future work and ``quotaDelays`` contain pauses
    applied to that task. ``projectionAnchored`` marks dates tied to quota times,
    including inherited dependencies; milestone anchor fields apply separately
    to each estimate. The constrained upper pass allocates each incremental
    root-sum-square uncertainty contribution to its task's provider calendar
    along the controlling upper path. This is a planning approximation; paths
    unaffected by quota retain the existing uncertainty calculation.
    """
    ordered = sorted(tasks, key=lambda task: (task["lane"].casefold(), _id_key(task["id"])))
    enriched = [_enrich(task, now) for task in ordered]
    by_id = {task["id"]: task for task in enriched}
    dependencies = {}
    followers = defaultdict(list)
    last_pending_in_lane = {}
    missing = {}
    warnings = []
    outages = _quota_outages(usage, now)
    task_providers = {task["id"]: _task_provider(task, provider, warnings) for task in enriched}
    running_in_lane = defaultdict(list)
    for task in enriched:
        if task["status"] == "in_progress":
            running_in_lane[task["lane"]].append(task["id"])
    for lane, running_ids in running_in_lane.items():
        if len(running_ids) > _SINGLE_RUNNING_TASK:
            for task_id in running_ids:
                _warn(by_id[task_id], f"Multiple tasks are running in lane {lane}", warnings)

    for task in enriched:
        task_id = task["id"]
        requirements = set()
        if task["status"] == "pending":
            requirements.update(task["blockedBy"])
            requirements.update(running_in_lane[task["lane"]])
            predecessor = last_pending_in_lane.get(task["lane"])
            last_pending_in_lane[task["lane"]] = task_id
            if predecessor is not None:
                requirements.add(predecessor)
            missing[task_id] = requirements - by_id.keys()
            for absent in sorted(missing[task_id], key=_id_key):
                _warn(task, f"Missing prerequisite task {absent}", warnings)
        dependencies[task_id] = sorted(requirements & by_id.keys(), key=_id_key)
        for requirement in dependencies[task_id]:
            followers[requirement].append(task_id)

    degrees = {task_id: len(requirements) for task_id, requirements in dependencies.items()}
    ready = deque(task_id for task_id, degree in degrees.items() if not degree)
    origin = _Forecast(now, quota_wait=timedelta())
    forecasts = {}
    upper_forecasts = {}
    variances = {}
    while ready:
        task_id = ready.popleft()
        task = by_id[task_id]
        status = task["status"]
        predecessors = [forecasts[requirement] for requirement in dependencies[task_id]]
        inherited_unknown = any(forecast.end is None for forecast in predecessors)
        inherited_unestimated = any(forecast.unestimated for forecast in predecessors)
        task["blocked"] = bool(missing.get(task_id)) or any(
            by_id[requirement]["blocked"] for requirement in dependencies[task_id]
        )
        mean, deviation = task["estimateMean"], task["estimateSd"]
        if status == "completed":
            forecast = _Forecast(_time(task["ended"]) or now)
            upper = forecast
        elif mean is None or inherited_unknown or missing.get(task_id):
            anchored = any(item.anchored for item in predecessors)
            forecast = _Forecast(None, unestimated=mean is None or inherited_unestimated, anchored=anchored)
            upper = _Forecast(None, anchored=anchored)
            task["projectionAnchored"] = anchored
            if inherited_unestimated:
                lane_predecessors = [
                    by_id[requirement] for requirement in dependencies[task_id]
                    if by_id[requirement]["lane"] == task["lane"]
                ]
                if any(forecasts[item["id"]].unestimated for item in lane_predecessors):
                    task["markers"].append("after an unestimated task")
                else:
                    task["markers"].append("blocked by an unestimated task")
            elif inherited_unknown:
                task["markers"].append("waiting for unresolved task")
        else:
            controlling = _controlling_forecast([origin] + predecessors, variances)
            remaining = 1 - task["effectiveProgress"] / _PERCENT if status == "in_progress" else 1
            task_provider = task_providers[task_id]
            intervals = outages.get(task_provider, [])
            work = _schedule_work(controlling.end, mean * remaining, intervals, task_provider)
            end = work.end
            forecast = _work_forecast(controlling, (*controlling.path, task_id), work)
            task["projectedSegments"] = work.segments
            task["quotaDelays"] = work.delays
            task["projectionAnchored"] = forecast.anchored
            if end is None:
                reason = ("Quota is exhausted but its reset time is unknown" if any(
                    delay["end"] is None for delay in work.delays) else
                    "Estimate exceeds the supported calendar range")
                _warn(task, reason, warnings)
            else:
                variances[task_id] = (deviation * remaining) ** 2
                task["projectedStart"] = work.segments[0]["start"]
                task["projectedEnd"] = end.isoformat()
                if status == "pending":
                    task["waitingFor"] = [
                        requirement for requirement in dependencies[task_id]
                        if by_id[requirement]["status"] != "completed"
                        and forecasts[requirement].end > now
                    ]
                    task["queued"] = bool(task["waitingFor"])
            upper = _upper_forecast(task, forecast,
                                   [upper_forecasts[requirement] for requirement in dependencies[task_id]],
                                   variances, origin, intervals, task_provider)
        forecasts[task_id] = forecast
        upper_forecasts[task_id] = upper
        for follower in followers[task_id]:
            degrees[follower] -= 1
            if not degrees[follower]:
                ready.append(follower)

    for task_id in degrees.keys() - forecasts.keys():
        task = by_id[task_id]
        forecasts[task_id] = _Forecast(None)
        upper_forecasts[task_id] = _Forecast(None)
        task["blocked"] = True
        task["markers"].append("dependency cycle")
        _warn(task, "A dependency cycle prevents projection", warnings)
    return {
        "tasks": enriched,
        "milestones": _milestones(enriched, forecasts, upper_forecasts, variances, warnings, now),
        "warnings": warnings,
        "quotaConstrained": any(item.anchored for item in forecasts.values()) or
                            any(item.anchored for item in upper_forecasts.values()),
    }
