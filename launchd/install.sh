#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
checkout=$(CDPATH= cd -- "$script_dir/.." && pwd)
template="$script_dir/local.live-gantt.plist"
label=$(/usr/bin/plutil -extract Label raw "$template")
python=$(/usr/bin/plutil -extract ProgramArguments.0 raw "$template")
user_id=$(/usr/bin/id -u)
plist="$HOME/Library/LaunchAgents/$label.plist"

if [ ! -x "$python" ]; then
    printf '%s\n' "Python is required at $python" >&2
    exit 1
fi

"$python" -B - "$template" "$checkout" "$plist" <<'PY'
import os
from pathlib import Path
import plistlib
import sys

MINIMUM_PYTHON = (3, 11)
OWNER_READ_WRITE = 0o600

if sys.version_info < MINIMUM_PYTHON:
    raise SystemExit("Python {}.{} or later is required".format(*MINIMUM_PYTHON))
template, checkout, target = map(Path, sys.argv[1:])
with template.open("rb") as stream:
    config = plistlib.load(stream)
log = Path.home() / "Library" / "Logs" / "live-gantt.log"
target.parent.mkdir(parents=True, exist_ok=True)
log.parent.mkdir(parents=True, exist_ok=True)
config["WorkingDirectory"] = str(checkout)
config["StandardOutPath"] = config["StandardErrorPath"] = str(log)
if "CLAUDE_CONFIG_DIR" in os.environ:
    config["EnvironmentVariables"]["CLAUDE_CONFIG_DIR"] = str(
        Path(os.environ["CLAUDE_CONFIG_DIR"]).expanduser().resolve()
    )
with target.open("wb") as stream:
    plistlib.dump(config, stream)
target.chmod(OWNER_READ_WRITE)
PY

/bin/launchctl bootout "gui/$user_id/$label" 2>/dev/null || :
/bin/launchctl bootstrap "gui/$user_id" "$plist"
cd "$checkout"
"$python" -B -c 'from live_gantt.server import DEFAULT_PORT, LOOPBACK; print(f"Live Gantt installed: http://{LOOPBACK}:{DEFAULT_PORT}")'
