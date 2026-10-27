"""Refresh an already-installed service when its plugin version advances."""

import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT))

from live_gantt.python_support import require_supported_python

require_supported_python()

from live_gantt.installation import VERSION_FILENAME, installation_directory
from live_gantt.endpoint import discover_server_url


VERSION_PATTERN = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
INSTALL_TIMEOUT_SECONDS = 60


def _version(value: object) -> tuple[int, ...] | None:
    if not isinstance(value, str):
        return None
    match = VERSION_PATTERN.fullmatch(value.strip())
    return tuple(int(part) for part in match.groups()) if match else None


def update_installation(plugin_root: Path, installed_root: Path) -> None:
    """Silently refresh an existing older copy; leave absent or unknown versions alone."""
    try:
        installed_version = _version((installed_root / VERSION_FILENAME).read_text())
        if installed_version is None:
            return
        manifest = json.loads((plugin_root / ".claude-plugin" / "plugin.json").read_text())
        if not isinstance(manifest, dict):
            return
        plugin_version = _version(manifest.get("version"))
        if plugin_version is None or installed_version >= plugin_version:
            return
        subprocess.run(
            [sys.executable, "-B", "-m", "live_gantt.installation", "install"],
            cwd=plugin_root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=INSTALL_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, UnicodeError, ValueError, subprocess.TimeoutExpired):
        return


def main() -> None:
    """Check the current user's stable installation without changing first-run setup."""
    try:
        installed_root = installation_directory(platform.system(), Path.home(), os.environ)
        update_installation(PLUGIN_ROOT, installed_root)
        address = discover_server_url(directory=installed_root)
        if address is not None:
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": f"Live Gantt for this account: {address}",
            }}))
    except (OSError, ValueError):
        return


if __name__ == "__main__":
    main()
