# Live Gantt

When will your agents be done? Live Gantt answers from the task list your Claude Code session already keeps: each milestone's likely finish (P50) and a cautious one (P90), from estimates, lanes and dependencies, redrawn live as tasks start, progress and finish. Background agents and outside runs (Codex, long jobs) appear under the tasks they serve, with their latest step. It runs on your machine, reads only local files and sends nothing anywhere. The task list is the plan; there is no separate plan file.

Requires Python 3.11 or later. Uses only its standard library and one static page with Hypertext Markup Language (HTML), Cascading Style Sheets (CSS) and JavaScript. No dependencies or build step are needed. Login startup supports macOS with launchd and Linux with a systemd user service.

## Install as a Claude Code plugin

This repository is both the plugin and its one-plugin marketplace. Nothing is published yet. With a local checkout, run these commands in Claude Code, replacing the example directory with the checkout's absolute path:

```text
/plugin marketplace add /absolute/path/to/live-gantt
/plugin install live-gantt@live-gantt
/live-gantt:open
```

The open command checks the local server. If it is not running, Claude explains installation and asks before running the installer, then gives you the chart address. The plugin's skill guides task planning; its TaskCreate hook reminds Claude when scheduling fields are missing.

Installation copies the application out of the replaceable plugin cache into stable per-user storage:

| Platform | Application directory |
| --- | --- |
| macOS | `~/Library/Application Support/live-gantt` |
| Linux | `$XDG_DATA_HOME/live-gantt`, or `~/.local/share/live-gantt` when unset |

It records the installed version and registers a login service using your Python interpreter. The service runs at niceness 10 with low-priority input/output and does no polling while the chart is closed. A SessionStart hook refreshes and restarts an already installed service when the plugin version is newer. It does nothing when no installation exists, or the installed version is current or newer. Installing the plugin alone does not install the service.

## Install without a plugin

From a checkout, register the same per-user service:

```sh
./scripts/install.sh
```

Open <http://127.0.0.1:4791>. No administrator privileges are required. An explicitly set `CLAUDE_CONFIG_DIR` is captured during installation and retained across updates; reinstall with a new value to change the source root. Reinstall from the updated checkout to update a standalone installation.

To run without a login service:

```sh
python3 -B -m live_gantt
```

To use another port for this foreground process, pass `--port 4792`. Stop it with Control-C. The `-B` flag prevents bytecode caches.

## Privacy

The chart runs locally, reads local files without modifying them, and sends nothing anywhere. The server binds only IPv4 loopback and accepts only `127.0.0.1` and `localhost` Host headers with its port. It sends no cross-origin resource sharing headers; source text is rendered as text, not executable markup. There are no analytics, remote assets or outbound requests.

The default source is Claude Code's configuration directory; `CLAUDE_CONFIG_DIR` selects another root. The service writes its own installed files and logs, never tasks or transcripts. Closing every chart tab stops source polling; reopening reconstructs history from transcripts.

## Compatibility

Tested with Claude Code **2.1.292**. Task lists, session transcripts and subagent metadata are local implementation details, not a public, versioned application programming interface (API). A Claude Code update can change them without notice. Task discovery and task history or agent association are likely to break first: expect missing sessions, missing actual start/end times or detached agents. Metadata warnings can identify individual invalid fields.

Report a compatibility problem as an issue with your Claude Code version, what is missing, and any warning text. Do not attach private transcripts. The invented-data [compatibility fixture](tests/fixtures/claude-code-2.1.292) and [end-to-end test](tests/test_compatibility.py) pin the observed file shapes and must be refreshed against each supported Claude Code release.

## Read the chart

Sessions are ordered by the last parent transcript or task-file change. The picker initially shows sessions changed within 14 days; **Show older sessions** reveals earlier sessions by identifier and date. Their title and project are read only when selected. A custom session title takes precedence over the first user prompt. Tasks are grouped by lane, in task identifier order. Current running work reserves its lane before pending work. Done bars use recorded start and completion times; a task without those times has a `no history` marker rather than invented dates.

Running bars end at now and have a patterned projection for the remaining estimate. Overrunning tasks use hatching. Pending tasks with a forecast waiting for their lane or prerequisites are **Queued**, with labels such as `after #56`. **Blocked** means a missing prerequisite or dependency cycle prevents a forecast. An unknown estimate leaves the task unestimated and marks affected successors. Bar and activity labels sit at the right end inside a bar when they fit, or just after it when they do not; horizontal scrolling reveals labels beyond the visible window. Detached agents appear in an Agents lane when active in the visible window. Silent agents have had no transcript activity for 30 minutes and no terminal notification. External runs stay live until their `ended` timestamp is set, even if their log goes quiet.

The default view is **12 h**, with now one quarter of the way across the visible chart. Use **12 h**, **3 days**, **Week** or **Fit**; the browser remembers your choice when local storage is available. The view follows now until you scroll horizontally; **Follow now** resumes it. Milestones with estimated remaining work use compact cards showing completion share and forecasts. Finished milestones share a **Done** line; milestones with no estimated remaining tasks share an **Unestimated** line. Hover or focus a task label to inspect its description, metadata, estimate and history; click to keep details open. Escape closes details. Connection age updates continuously; the browser reconnects after a disconnection. There is no animation when reduced motion is enabled.

## Task planning

Load the [live-gantt skill](skills/live-gantt/SKILL.md) when creating or updating a multi-step plan, or starting agents and external runs attached to it. That is the task-metadata contract, including field guidance, a two-lane plan and the external-run lifecycle. The chart never writes task metadata itself.

## Interfaces and verification

The page uses read-only Hypertext Transfer Protocol (HTTP) endpoints:

| Endpoint | Response |
| --- | --- |
| `/api/lists` | `{ "lists": [...] }`, ordered by recent source change. Each entry includes a `recent` flag; older entries carry identifier/date with an identifier title and empty project. |
| `/api/state?list=ID` | Full selected-session model with tasks, agents, milestones, warnings and an `observedAt` projection timestamp; an unknown list returns 404. |
| `/events?list=ID` | Server-Sent Events (SSE): `state` on selected-source changes, `lists` when the picker changes, and a `heartbeat` event plus comment every 15 seconds. A removed session emits `missing`. |
| `/favicon.ico` | Inline Scalable Vector Graphics (SVG) Gantt icon using the page's color tokens. |

Sources are checked every two seconds only while an event stream is connected, or on explicit page/API requests. State events carry full models; unchanged sources do not emit state events. The browser advances now-dependent dates between updates. A fresh state request always recalculates the projection.

```sh
python3 -B -m unittest
node --check live_gantt/static/app.js
```

Tests use invented fixtures and temporary directories, never your Claude configuration. HTTP tests open an ephemeral loopback listener; a sandbox that denies binding sockets cannot run those tests. On this Mac, run checks at utility priority with `taskpolicy -c utility python3 -B -m unittest`.

Continuous integration (CI) runs the same unit tests and JavaScript syntax check on Ubuntu and macOS with Python 3.11 and the latest stable Python.

See [ARCHITECTURE.md](ARCHITECTURE.md) for source and projection boundaries.

## Uninstall

Stop and unregister the per-user service from a checkout or plugin directory:

```sh
./scripts/uninstall.sh
```

It stops the process and removes the service definition, copied application, recorded configuration and installed-version marker. Removing the marker keeps the SessionStart hook from reinstalling a service you chose to remove. Logs and Claude Code files are retained. On macOS, logs are in the user's Library Logs directory; on Linux, use `journalctl --user -u live-gantt.service`.

For a plugin installation, also run in Claude Code:

```text
/plugin uninstall live-gantt@live-gantt
/plugin marketplace remove live-gantt
```

Removing the plugin alone does not unregister the service. Foreground-only use needs no uninstall step beyond stopping the process.

## Licence

Licensed under either of [Apache License, Version 2.0](LICENSE-APACHE) or [MIT licence](LICENSE-MIT) at your option.

Made by al-Kindi (https://alkindi.pt)
