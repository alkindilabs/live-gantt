"""Pure lane scheduling and milestone forecasts for normalized source records."""

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta
import math
import re


_PERCENT = 100
_PERT_DIVISOR = 6
_P90_QUANTILE = 1.2816
_ZERO = 0.0
_SINGLE_RUNNING_TASK = 1
_NUMERIC_ID_PART = 0
_TEXT_ID_PART = 1
_ID_PARTS = re.compile(r"([0-9]+)")


@dataclass(frozen=True)
class _Forecast:
    end: datetime | None
    path: frozenset[str] = frozenset()
    unestimated: bool = False


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


def _variance(path: frozenset[str], variances: dict[str, float]) -> float:
    try:
        return math.fsum(variances[task_id] for task_id in path)
    except OverflowError:
        return math.inf


def _calendar_end(start: datetime, hours: float) -> datetime | None:
    try:
        return start + timedelta(hours=hours)
    except OverflowError:
        return None


def _warn(task: dict, message: str, warnings: list[str]) -> None:
    task["warnings"].append(message)
    warnings.append(f"Task {task['id']}: {message}")


def _milestones(tasks: list[dict], forecasts: dict[str, _Forecast], variances: dict[str, float], warnings: list[str]) -> list[dict]:
    groups = defaultdict(list)
    for task in tasks:
        groups[task["milestone"] or "Main"].append(task)
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
        remaining = [task for task in members if task["status"] != "completed"]
        known_ends = [_time(task["ended"]) for task in members if task["status"] == "completed" and task["ended"]]
        p50 = p90 = max(known_ends, default=None)
        if remaining:
            remaining_forecasts = [forecasts[task["id"]] for task in remaining]
            if any(forecast.end is None for forecast in remaining_forecasts):
                p50 = p90 = None
            else:
                p50 = max([forecast.end for forecast in remaining_forecasts] + known_ends)
                uncertain_ends = []
                for task in remaining:
                    forecast = forecasts[task["id"]]
                    uncertain_end = _calendar_end(
                        forecast.end, _P90_QUANTILE * math.sqrt(_variance(forecast.path, variances)),
                    )
                    if uncertain_end is None:
                        _warn(task, "P90 forecast exceeds the supported calendar range", warnings)
                    else:
                        uncertain_ends.append(uncertain_end)
                p90 = max(uncertain_ends + known_ends) if len(uncertain_ends) == len(remaining) else None
        milestones.append({
            "name": name, "progress": completed_weight / total_weight * _PERCENT,
            "hasRemaining": bool(remaining),
            "hasEstimate": any(task["estimate"] is not None for task in remaining),
            "p50": p50.isoformat() if p50 else None,
            "p90": p90.isoformat() if p90 else None,
        })
    return milestones


def project(tasks: list[dict], now: datetime) -> dict:
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
    breaking ties by variance, and adds each task's remaining variance once.
    Weighted milestone completion uses explicit progress, while forecast
    duration uses the larger of explicit and live-journal progress.
    """
    ordered = sorted(tasks, key=lambda task: (task["lane"].casefold(), _id_key(task["id"])))
    enriched = [_enrich(task, now) for task in ordered]
    by_id = {task["id"]: task for task in enriched}
    dependencies = {}
    followers = defaultdict(list)
    last_pending_in_lane = {}
    missing = {}
    warnings = []
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
        dependencies[task_id] = requirements & by_id.keys()
        for requirement in dependencies[task_id]:
            followers[requirement].append(task_id)

    degrees = {task_id: len(requirements) for task_id, requirements in dependencies.items()}
    ready = deque(task_id for task_id, degree in degrees.items() if not degree)
    forecasts = {}
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
        elif mean is None or inherited_unknown or missing.get(task_id):
            forecast = _Forecast(None, unestimated=mean is None or inherited_unestimated)
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
            controlling = max(
                [_Forecast(now)] + predecessors,
                key=lambda item: (item.end, _variance(item.path, variances)),
            )
            remaining = 1 - task["effectiveProgress"] / _PERCENT if status == "in_progress" else 1
            end = _calendar_end(controlling.end, mean * remaining)
            if end is None:
                forecast = _Forecast(None)
                _warn(task, "Estimate exceeds the supported calendar range", warnings)
            else:
                variances[task_id] = (deviation * remaining) ** 2
                forecast = _Forecast(end, controlling.path | {task_id})
                task["projectedStart"] = controlling.end.isoformat()
                task["projectedEnd"] = end.isoformat()
                if status == "pending":
                    task["waitingFor"] = sorted((
                        requirement for requirement in dependencies[task_id]
                        if by_id[requirement]["status"] != "completed"
                        and forecasts[requirement].end > now
                    ), key=_id_key)
                    task["queued"] = bool(task["waitingFor"])
        forecasts[task_id] = forecast
        for follower in followers[task_id]:
            degrees[follower] -= 1
            if not degrees[follower]:
                ready.append(follower)

    for task_id in degrees.keys() - forecasts.keys():
        task = by_id[task_id]
        forecasts[task_id] = _Forecast(None)
        task["blocked"] = True
        task["markers"].append("dependency cycle")
        _warn(task, "A dependency cycle prevents projection", warnings)
    return {
        "tasks": enriched,
        "milestones": _milestones(enriched, forecasts, variances, warnings),
        "warnings": warnings,
    }
