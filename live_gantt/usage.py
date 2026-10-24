"""Validate local quota observations and derive bounded pace estimates."""

from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import platform
import re

from .installation import installation_directory

FORECAST_HORIZON = timedelta(minutes=30)
MINIMUM_SAMPLE_SPAN = timedelta(minutes=5)
STALE_AFTER = timedelta(minutes=5)
MAX_HISTORY_SNAPSHOTS = 512
MAX_HISTORY_BYTES = 1024 * 1024
MAX_WINDOWS = 16
MAX_LABEL_CHARACTERS = 160
HISTORY_FILENAME = "quota-history.json"
OWNER_DIRECTORY_PERMISSIONS = 0o700
OWNER_FILE_PERMISSIONS = 0o600
TOKEN_FIELDS = ("inputTokens", "cachedInputTokens", "cacheWriteTokens", "outputTokens",
                "reasoningOutputTokens", "totalTokens")
CLAUDE_WINDOWS = {"five_hour": 300, "seven_day": 10080}
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}\Z")
PERCENT_LIMIT = 100
MINUTES_PER_HOUR = 60
MINUTES_PER_DAY = 1440


def empty_token_usage():
    """Return unknown session token categories, without inventing zero consumption."""
    return dict.fromkeys(TOKEN_FIELDS)


def default_usage_root():
    """Return application-owned capture storage without creating it."""
    return installation_directory(platform.system(), Path.home(), os.environ) / "usage"


def _identifier(value):
    return isinstance(value, str) and IDENTIFIER.fullmatch(value) is not None


def _number(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _date(value):
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(timezone.utc) if result.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def _iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_snapshot(value, now):
    if not isinstance(value, dict):
        return None
    captured = _date(value.get("capturedAt"))
    if captured is None or captured > now:
        return None
    source, bucket = value.get("sourceId"), value.get("bucketId")
    raw_windows = value.get("windows")
    if not _identifier(source) or not _identifier(bucket) or not isinstance(raw_windows, list):
        return None
    windows = []
    seen = set()
    for raw in raw_windows[:MAX_WINDOWS]:
        if not isinstance(raw, dict) or not _identifier(raw.get("id")) or raw["id"] in seen:
            continue
        seen.add(raw["id"])
        percent = raw.get("usedPercent")
        percent = percent if _number(percent) and 0 <= percent <= PERCENT_LIMIT else None
        duration = raw.get("windowMinutes")
        duration = duration if type(duration) is int and duration > 0 else None
        reset = _date(raw.get("resetsAt"))
        measured = _date(raw.get("measuredAt"))
        if measured is not None and measured > captured:
            measured = None
        observed = _date(raw.get("observedAt"))
        if observed is None or observed > captured:
            observed = captured
        windows.append({"id": raw["id"], "windowMinutes": duration, "usedPercent": percent,
                        "resetsAt": _iso(reset) if reset else None,
                        "measuredAt": _iso(measured) if measured else None,
                        "observedAt": _iso(observed)})
        label = raw.get("label")
        if isinstance(label, str) and label.strip():
            windows[-1]["label"] = label.strip()[:MAX_LABEL_CHARACTERS]
    snapshot = {"sourceId": source, "bucketId": bucket, "capturedAt": _iso(captured), "windows": windows}
    return snapshot


def _read_history(root, now):
    if root is None:
        return []
    try:
        with (Path(root) / HISTORY_FILENAME).open("rb") as stream:
            payload = stream.read(MAX_HISTORY_BYTES + 1)
        if len(payload) > MAX_HISTORY_BYTES:
            return []
        values = json.loads(payload)
    except (OSError, ValueError, UnicodeError):
        return []
    if not isinstance(values, list):
        return []
    return [snapshot for value in values[-MAX_HISTORY_SNAPSHOTS:]
            if (snapshot := _normalize_snapshot(value, now)) is not None]


def _window_signature(window):
    return tuple(window.get(key) for key in ("id", "windowMinutes", "usedPercent", "resetsAt", "measuredAt"))


def _append_snapshot(history, snapshot):
    prior = next((entry for entry in reversed(history)
                  if entry["sourceId"] == snapshot["sourceId"] and entry["bucketId"] == snapshot["bucketId"]), None)
    if prior is not None:
        if _date(snapshot["capturedAt"]) < _date(prior["capturedAt"]):
            return False
        previous = {window["id"]: window for window in prior["windows"]}
        for window in snapshot["windows"]:
            earlier = previous.get(window["id"])
            if earlier is not None and _window_signature(earlier) == _window_signature(window):
                window["observedAt"] = earlier["observedAt"]
        if snapshot == prior:
            return False
        if snapshot["windows"] == prior["windows"]:
            prior["capturedAt"] = snapshot["capturedAt"]
            history.sort(key=lambda value: _date(value["capturedAt"]))
            return True
    history.append(snapshot)
    history.sort(key=lambda value: _date(value["capturedAt"]))
    del history[:-MAX_HISTORY_SNAPSHOTS]
    return True


def _label(duration, role):
    if duration is None:
        return role.replace("_", " ")
    if duration % MINUTES_PER_DAY == 0:
        value, unit = duration // MINUTES_PER_DAY, "day"
    elif duration % MINUTES_PER_HOUR == 0:
        value, unit = duration // MINUTES_PER_HOUR, "hour"
    else:
        value, unit = duration, "minute"
    return f"{value} {unit}{'' if value == 1 else 's'}"


def _forecast(status, *, exhausts=None, basis=None, seconds=None):
    return {"status": status, "exhaustsAt": _iso(exhausts) if exhausts else None,
            "basis": basis, "sampleSeconds": seconds}


def _same_window(current, previous):
    if previous is None or any(previous[key] != current[key] for key in ("id", "windowMinutes")):
        return False
    if previous["resetsAt"] == current["resetsAt"]:
        return True
    current_reset = _date(current["resetsAt"])
    return current_reset is not None and current_reset == _date(previous["resetsAt"])


def _samples(history, latest, current, now):
    samples = []
    for snapshot in history:
        if snapshot["sourceId"] != latest["sourceId"] or snapshot["bucketId"] != latest["bucketId"]:
            continue
        window = next((row for row in snapshot["windows"] if row["id"] == current["id"]), None)
        if not _same_window(current, window):
            samples = []
            continue
        percent = window["usedPercent"]
        if percent is None:
            samples = []
            continue
        instant = _date(window["measuredAt"] or window["observedAt"])
        if samples and percent < samples[-1][1]:
            samples = []
        if not samples or (percent != samples[-1][1] or instant != samples[-1][0]):
            samples.append((instant, percent))
    return [sample for sample in samples if now - FORECAST_HORIZON <= sample[0] <= now]


def _window_state(provider, history, latest, current, now):
    captured = _date(current["observedAt"])
    measured = _date(current["measuredAt"])
    samples = _samples(history, latest, current, now)
    freshness = "provider_measurement" if measured else "recorded_change" if len(samples) > 1 else "unknown"
    reset = _date(current["resetsAt"])
    available = current["usedPercent"] is not None
    stale = available and (now - (measured or captured) > STALE_AFTER or (reset is not None and now >= reset))
    if not available:
        forecast = _forecast("unavailable")
    elif stale:
        forecast = _forecast("stale")
    elif current["usedPercent"] == PERCENT_LIMIT:
        forecast = _forecast("reached", exhausts=measured or captured, basis="recorded_changes")
    elif len(samples) < 2 or samples[-1][0] - samples[0][0] < MINIMUM_SAMPLE_SPAN:
        forecast = _forecast("insufficient_history")
    else:
        seconds = (samples[-1][0] - samples[0][0]).total_seconds()
        change = samples[-1][1] - samples[0][1]
        if change <= 0:
            forecast = _forecast("no_observed_pace", basis="recorded_changes", seconds=seconds)
        else:
            remaining_seconds = (PERCENT_LIMIT - current["usedPercent"]) * seconds / change
            try:
                exhaustion = samples[-1][0] + timedelta(seconds=remaining_seconds)
                forecast = (_forecast("resets_first", basis="recorded_changes", seconds=seconds)
                            if reset is not None and exhaustion >= reset else
                            _forecast("predicted", exhausts=exhaustion, basis="recorded_changes", seconds=seconds))
            except OverflowError:
                forecast = _forecast("resets_first" if reset else "unavailable", basis="recorded_changes", seconds=seconds)
    return {"id": current["id"], "bucketId": latest["bucketId"],
            "label": current.get("label") or _label(current["windowMinutes"], current["id"]),
            "windowMinutes": current["windowMinutes"], "usedPercent": current["usedPercent"],
            "resetsAt": current["resetsAt"], "capturedAt": latest["capturedAt"],
            "observedAt": _iso(captured),
            "measuredAt": current["measuredAt"], "freshnessBasis": freshness, "stale": stale,
            "forecast": forecast}


def _provider_state(provider, history, now):
    latest_by_bucket = {}
    for snapshot in history:
        latest_by_bucket[snapshot["bucketId"]] = snapshot
    windows = []
    for bucket, latest in sorted(latest_by_bucket.items()):
        current = {window["id"]: window for window in latest["windows"]}
        known = {window["id"]: window for entry in history if entry["bucketId"] == bucket
                 for window in entry["windows"]}
        for role, previous in known.items():
            window = current.get(role)
            if window is None:
                window = {**previous, "usedPercent": None, "resetsAt": None,
                          "measuredAt": None, "observedAt": latest["capturedAt"]}
            windows.append(_window_state(provider, history, latest, window, now))
    readable = [window for window in windows if window["usedPercent"] is not None]
    status = "available" if any(not window["stale"] for window in readable) else "stale" if readable else "unavailable"
    latest = max(history, key=lambda entry: _date(entry["capturedAt"])) if history else None
    message = {"available": "Quota observations are available.", "stale": "Recorded quota is stale.",
               "unavailable": "No readable quota observations."}[status]
    return {"provider": provider, "status": status, "sourceId": latest["sourceId"] if latest else None,
            "windows": windows, "message": message}


class UsageStore:
    """Accumulate Codex quota observations beside the recorded Claude status-line history.

    Callers serialize access. A null capture root disables disk reads and writes.
    """

    def __init__(self, capture_root=None):
        self.capture_root = capture_root
        self._codex_history = []

    def state(self, codex_snapshots, now):
        """Return provider quota, freshness and local forecasts at aware UTC time."""
        now = now.astimezone(timezone.utc)
        if isinstance(codex_snapshots, list):
            valid = [snapshot for value in codex_snapshots
                     if (snapshot := _normalize_snapshot(value, now)) is not None]
            ordered = sorted(valid, key=lambda entry: _date(entry["capturedAt"]))
            for snapshot in ordered[-MAX_HISTORY_SNAPSHOTS:]:
                _append_snapshot(self._codex_history, snapshot)
        return {"providers": [_provider_state("claude", _read_history(self.capture_root, now), now),
                              _provider_state("codex", self._codex_history, now)]}
