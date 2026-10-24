"""Capture normalized Claude quota observations without interrupting a status line."""

import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import sys
import tempfile

from .usage import (CLAUDE_WINDOWS, HISTORY_FILENAME, MAX_HISTORY_BYTES,
                    OWNER_DIRECTORY_PERMISSIONS, OWNER_FILE_PERMISSIONS,
                    _append_snapshot, _identifier, _iso, _normalize_snapshot,
                    _number, _read_history, default_usage_root)

MAX_INPUT_BYTES = 1024 * 1024


def _epoch(value):
    if not _number(value):
        return None
    try:
        return _iso(datetime.fromtimestamp(value, timezone.utc))
    except (ValueError, OverflowError, OSError):
        return None


def capture(document, now, root):
    """Persist only quota fields; return whether capture succeeded.

    Concurrent writers serialize using an application-owned lock and replace a
    bounded history atomically. Invalid input and storage failures return false.
    """
    if not isinstance(document, dict) or not _identifier(document.get("session_id")):
        return False
    rate_limits = document.get("rate_limits")
    if not isinstance(rate_limits, dict):
        rate_limits = {}
    windows = []
    for role, minutes in CLAUDE_WINDOWS.items():
        window = rate_limits.get(role)
        if not isinstance(window, dict):
            continue
        windows.append({"id": role, "windowMinutes": minutes,
                        "usedPercent": window.get("used_percentage"),
                        "resetsAt": _epoch(window.get("resets_at"))})
    try:
        now = now.astimezone(timezone.utc)
        snapshot = _normalize_snapshot({"sourceId": document["session_id"], "bucketId": "claude",
                                        "capturedAt": _iso(now), "windows": windows}, now)
        root = Path(root)
        root.mkdir(mode=OWNER_DIRECTORY_PERMISSIONS, parents=True, exist_ok=True)
        descriptor = os.open(root / ".capture.lock", os.O_RDWR | os.O_CREAT, OWNER_FILE_PERMISSIONS)
        with os.fdopen(descriptor, "a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            history = _read_history(root, datetime.max.replace(tzinfo=timezone.utc))
            if not _append_snapshot(history, snapshot):
                return True
            payload = json.dumps(history, allow_nan=False, separators=(",", ":")).encode()
            while len(payload) > MAX_HISTORY_BYTES and len(history) > 1:
                history.pop(0)
                payload = json.dumps(history, allow_nan=False, separators=(",", ":")).encode()
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode="wb", dir=root, prefix=".quota-", delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, root / HISTORY_FILENAME)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        return True
    except (OSError, ValueError, TypeError, OverflowError):
        return False


def main():
    """Pass standard input through byte-for-byte, even when capture cannot run."""
    payload = sys.stdin.buffer.read()
    try:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--directory", type=Path)
        arguments = parser.parse_args()
        if len(payload) <= MAX_INPUT_BYTES:
            capture(json.loads(payload), datetime.now(timezone.utc),
                    arguments.directory if arguments.directory is not None else default_usage_root())
    except (Exception, SystemExit):
        pass
    finally:
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
