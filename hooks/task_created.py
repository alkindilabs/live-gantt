"""Suggest missing scheduling metadata after a successful TaskCreate."""

import json
import sys
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT))

from live_gantt.freshness import SCHEDULING_FIELDS


def main() -> None:
    """Read a hook event and emit advisory context without blocking the session."""
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict):
            return
        if event.get("hook_event_name") != "PostToolUse" or event.get("tool_name") != "TaskCreate":
            return
        inputs = event.get("tool_input")
        response = event.get("tool_response")
        if not isinstance(inputs, dict) or not isinstance(response, dict):
            return
        metadata = inputs.get("metadata", {})
        task = response.get("task")
        if not isinstance(metadata, dict) or not isinstance(task, dict):
            return
        task_id = task.get("id")
        subject = task.get("subject")
        if not isinstance(task_id, str) or not isinstance(subject, str):
            return
        missing = [field for field in SCHEDULING_FIELDS if metadata.get(field) is None]
        if not missing:
            return
        fields = " and ".join(f"`{field}`" for field in missing)
        context = (
            f"Live Gantt cannot schedule task #{task_id} ({json.dumps(subject)}): "
            f"its metadata lacks {fields}. Load the live-gantt skill and use TaskUpdate "
            f"to set {fields} on this task."
        )
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": context,
        }}))
    except (OSError, UnicodeError, ValueError, RecursionError):
        return


if __name__ == "__main__":
    main()
