---
description: Install if needed and open the local Live Gantt chart
---

Find the running server for the current operating-system user before offering installation. Run the shared discovery command from the plugin root:

```sh
cd "${CLAUDE_PLUGIN_ROOT}" && python3 -B -m live_gantt.python_support live_gantt.open
```

On success it prints `Live Gantt: http://127.0.0.1:PORT`. Give the user that address to open in their browser; do not assume a fixed port. The command selects a supported Python before loading the application, verifies listener ownership and checks the page without requesting provider data. It can discover an existing current-user service on the original port without reinstalling it.

On failure, report the diagnostic without claiming the server is absent: discovery may also be unable to verify its ownership or readiness. Never offer an unverified address. If the user wants setup, explain that installation copies the application into a stable per-user data directory, records its version, and registers and starts a login service using launchd on macOS or a systemd user service on Linux. It uses Python 3.11 or later, requires no administrator access or dependencies, and serves only on loopback. The service chooses an available port and saves its address in private per-user storage. Once installed, a newer plugin version refreshes that copy at session start. An existing installation is refreshed and restarted by the same installer.

Discovery and provider sources remain separate for each operating-system user. Loopback is not user authentication.

Ask the user before running the installer. After approval, run:

```sh
"${CLAUDE_PLUGIN_ROOT}/scripts/install.sh"
```

Recheck the server with the first command and give the printed address. If installation or the recheck fails, report the error instead of claiming the chart is running. Do not override the port or change provider configuration to bypass a discovery failure.
