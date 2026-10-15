---
description: Install if needed and open the local Live Gantt chart
---

Check whether the local server answers before offering installation. Run this from the plugin root; it obtains the address from the server's own constants:

```sh
cd "${CLAUDE_PLUGIN_ROOT}" && python3 -B - <<'PY'
from http.client import HTTPConnection
from http import HTTPStatus
from live_gantt.server import DEFAULT_PORT, LOOPBACK

CONNECTION_TIMEOUT_SECONDS = 2

connection = HTTPConnection(LOOPBACK, DEFAULT_PORT, timeout=CONNECTION_TIMEOUT_SECONDS)
try:
    connection.request("GET", "/")
    response = connection.getresponse()
    if response.status != HTTPStatus.OK:
        raise SystemExit("Live Gantt is not answering successfully")
    print(f"Live Gantt: http://{LOOPBACK}:{DEFAULT_PORT}")
except OSError:
    raise SystemExit("Live Gantt is not running")
finally:
    connection.close()
PY
```

If it answers, give the user the printed URL to open in their browser.

If it does not answer, explain that installation copies the application into a stable per-user data directory, records its version, and registers and starts a login service using launchd on macOS or a systemd user service on Linux. It uses Python 3.11 or later, requires no administrator access or dependencies, and serves only on loopback. Once installed, a newer plugin version refreshes that copy at session start. An existing installation is refreshed and restarted by the same installer.

Ask the user before running the installer. After approval, run:

```sh
"${CLAUDE_PLUGIN_ROOT}/scripts/install.sh"
```

Recheck the server with the first command and give the URL. If installation or the recheck fails, report the error instead of claiming the chart is running. Do not change the port or Claude configuration to work around a failure.
