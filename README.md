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

The open command checks the local server. If it is not running, Claude explains installation and asks before running the installer, then gives you the chart address. The plugin's skill guides task planning; its TaskCreate hook reminds Claude when scheduling fields are missing, and its freshness hooks keep the plan from going stale (see [Freshness](#freshness)).

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

Sessions are ordered by the last parent transcript or task-file change. The session picker (click it or press `/`) lists sessions that have task files or agents, since only those can draw a chart; headless `claude -p` runs such as commit gates, and chats without tasks, stay hidden until you tick **All sessions**. Each entry shows its title, the first prompt when a custom title replaces it, **Active** when its transcript changed in the last 5 minutes (otherwise how long ago it changed), the project and its task and agent counts. Type to filter by title, prompt or project; use the arrow keys and Enter to switch and Escape to close. The address keeps the selected session as `#session=<id>`, so a bookmark or reload returns to it. Sessions without tasks that last changed more than 14 days ago show only their identifier and date until selected. A session's custom title (the latest rename) takes precedence over the first prompt you typed, and the project shown is the session's latest working directory; command caveats, slash-command echoes and other harness markup are skipped. Tasks are grouped by lane, in task identifier order. Current running work reserves its lane before pending work. Done bars use recorded start and completion times; a task without those times has a `no history` marker rather than invented dates.

Running bars end at now and have a patterned projection for the remaining estimate. Overrunning tasks use hatching. Pending tasks with a forecast waiting for their lane or prerequisites are **Queued**, with labels such as `after #56`. **Blocked** means a missing prerequisite or dependency cycle prevents a forecast. An unknown estimate leaves the task unestimated and marks affected successors. A running agent's bar names its latest step when that step described itself; finished, failed and silent agents' bars name their state. Bar and activity labels sit at the right end inside a bar when they fit, or just after it when they do not; horizontal scrolling reveals labels beyond the visible window. Detached agents appear in an Agents lane when active in the visible window. An agent is done once it hands its report back or the session is notified that it finished; a resumed agent runs again. Silent agents have had no transcript activity for 30 minutes without finishing. External runs stay live until their `ended` timestamp is set, even if their log goes quiet.

The default view is **12 h**, with now one quarter of the way across the visible chart. Use **12 h**, **3 days**, **Week** or **Fit**; the browser remembers your choice when local storage is available. The view follows now until you scroll horizontally; **Follow now** resumes it. Milestones with estimated remaining work use compact cards showing completion share and forecasts. Finished milestones share a **Done** line; milestones with no estimated remaining tasks share an **Unestimated** line. Each task is one compact row. Tasks finished more than two hours ago fold into one summary row per lane and milestone (for example `Core · 12 done, 29 h`), and finished or failed agent attempts show as marks under the task's bar with an `N attempts` badge; click either to expand, and the browser remembers it. Hover or focus a task label to inspect its description, metadata, estimate and history; click to keep details open. Escape closes details. Connection age updates continuously; the browser reconnects after a disconnection. There is no animation when reduced motion is enabled.

## Freshness

The chart is only useful if it is never silently out of date, so one set of rules judges every session, in [freshness.py](live_gantt/freshness.py). A finding is reported when:

1. an unfinished task lacks a valid `estimate` or a `lane`;
2. an in-progress task file has not changed for 15 minutes while the session transcript changed within the last 5 minutes;
3. an in-progress task has no numeric `progress`;
4. a run that has not ended names a `log` that is missing, or whose log has not changed for 10 minutes while the session is active;
5. a run that has not ended declares a `pid` that no longer exists, and its log, if it has one, shows no life.

Completed and deleted tasks and task files that cannot be read never produce findings. When findings exist, the page shows a red banner listing them; each in-progress task shows how long ago it was updated and, for a run finding, what was declared against what was observed, for example `declared running; observed: silent 23 min`.

The plugin enforces the same rules with [its freshness hook](hooks/freshness.py) on three events:

| Event | Behaviour |
| --- | --- |
| `Stop` | Blocks the end of a turn with one paragraph naming each finding, at most once per 5 minutes per session, and never while a stop hook is already active. |
| `UserPromptSubmit` | Adds the same paragraph as context on each prompt while findings exist. |
| `PostToolUse` (every tool) | Adds the same paragraph as context at most once per 5 minutes; inside that window it reads only its own state file. |

These hooks only read local files: the session's task files, its transcript's modification time and the logs its runs name. They probe declared process identifiers with signal zero, which delivers no signal. They print to the session and write only one small state file per session, holding the last block and reminder times, under the plugin data directory (`CLAUDE_PLUGIN_DATA`, or the application directory above when unset). They never write tasks or transcripts, send nothing anywhere, and stay silent when they cannot read the task files or record their state.

## Task planning

Load the [live-gantt skill](skills/live-gantt/SKILL.md) when creating or updating a multi-step plan, or starting agents and external runs attached to it. That is the task-metadata contract, including field guidance, a two-lane plan and the external-run lifecycle. The chart never writes task metadata itself.

## Interfaces and verification

The page uses read-only Hypertext Transfer Protocol (HTTP) endpoints:

| Endpoint | Response |
| --- | --- |
| `/api/lists` | `{ "lists": [...] }`, ordered by recent source change. Each entry has `id`, `title`, `prompt` (the first prompt when a custom title hides it, else empty), `project`, `updatedAt`, `recent`, `active` (transcript changed within 5 minutes), `chartable` (has task files or agents), `taskCount`, `agentCount` and `headless` (a non-interactive `claude -p` run). Older entries that cannot draw a chart carry identifier/date with an identifier title and empty project. |
| `/api/state?list=ID` | Full selected-session model with tasks, agents, `workflows` (each run's `id`, `name`, `description`, `status`, `started`, `ended` and unassigned `agents`, which add `phase` and `workflow`), milestones, warnings, `freshness`, `transcriptUpdatedAt` and an `observedAt` projection timestamp; an unknown list returns 404. |
| `/events?list=ID` | Server-Sent Events (SSE): `state` on selected-source changes, `lists` when the picker changes, and a `heartbeat` event plus comment every 15 seconds. A removed session emits `missing`. |
| `/favicon.ico` | Inline Scalable Vector Graphics (SVG) Gantt icon using the page's color tokens. |

`freshness` is `{stale, findings, sinceChangeSeconds, sessionActive}`. Each finding has `rule` (`missing-schedule`, `stale-progress`, `missing-progress`, `silent-run` or `process-gone`), `taskId` and a plain-sentence `message`; run findings add `observed`, such as `silent 23 min`, `log not found` or `process gone`. `sinceChangeSeconds` counts from the newest task-file change and is `null` without task files. Each task carries `updatedAt`, its file's modification time, and `readable`. Each run carries `logUpdatedAt`, `pid` and `pidAlive`, which is `null` without a `pid` or once the run has ended.

Sources are checked every two seconds only while an event stream is connected, or on explicit page/API requests. State events carry full models; unchanged sources do not emit state events, except when the freshness verdict changes with time alone. The browser advances now-dependent dates between updates. A fresh state request always recalculates the projection.

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
