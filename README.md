# Live Gantt

Live Gantt shows local Claude Code and Codex sessions, their recorded token use, and account quota with reset times and current-pace estimates. For Claude Code task plans, it also shows progress and when each milestone is expected to finish. The overview pairs reported progress with an expected finish (50th-percentile planning estimate, P50) and a later estimate (90th-percentile planning estimate, P90). The timeline explains that outlook through tasks, agents, dependencies and recorded work. It fills the workspace height below the session header; account usage and plan progress scroll independently in the Overview sidebar. Narrow screens switch between Overview and the timeline. It runs on your machine from local files and makes no provider requests. The task list is the plan; there is no separate plan file.

Requires Python 3.11 or later. Hooks started under an older `python3`, as Codex's background daemon does on macOS, re-run under Homebrew's Python when it is installed. Uses only its standard library and one static page with Hypertext Markup Language (HTML), Cascading Style Sheets (CSS) and JavaScript. No dependencies or build step are needed. Login startup supports macOS with launchd and Linux with a systemd user service. Live Gantt supports Claude Code and local Codex desktop and command-line sessions on macOS and Linux. Ordinary ChatGPT or Claude web conversations and Cowork are outside this local source boundary. Codex activity on a ChatGPT account is included when recorded locally.

## Install as a Claude Code plugin

This repository is both the plugin and its one-plugin marketplace. In your terminal, add the public marketplace and install the plugin:

```sh
claude plugin marketplace add alkindilabs/live-gantt
claude plugin install live-gantt@live-gantt
```

Then start or restart Claude Code and run `/live-gantt:open`.

To update an existing installation:

```sh
claude plugin update live-gantt@live-gantt
```

Restart Claude Code to load the updated plugin. Its SessionStart hook updates an existing local service when the plugin version advances. For local development, use the checkout's path instead of `alkindilabs/live-gantt` when adding the marketplace.

The open command checks the local server. If it is not running, Claude explains installation and asks before running the installer, then gives you the chart address. The plugin's skill guides task planning; its TaskCreate hook reminds Claude when scheduling fields are missing, and its freshness hooks keep the plan from going stale (see [Freshness](#freshness)).

Installation copies the application out of the replaceable plugin cache into stable per-user storage:

| Platform | Application directory |
| --- | --- |
| macOS | `~/Library/Application Support/live-gantt` |
| Linux | `$XDG_DATA_HOME/live-gantt`, or `~/.local/share/live-gantt` when unset |

It records the installed version and registers a login service using your Python interpreter. The service runs at niceness 10 with low-priority input/output. Local task polling stops while the chart is closed, except for the last opened session, which keeps syncing for ten minutes after it was last read. A SessionStart hook refreshes and restarts an already installed service when the plugin version is newer. It does nothing when no installation exists, or the installed version is current or newer. Installing the plugin alone does not install the service.

## Install without a plugin

From a checkout, register the same per-user service:

```sh
./scripts/install.sh
```

Open <http://127.0.0.1:4791>. No administrator privileges are required. Explicit `CLAUDE_CONFIG_DIR` and `CODEX_HOME` values are captured during installation and retained across updates; reinstall with a new value to change either source root. An explicitly empty value clears that override. Reinstall from the updated checkout to update a standalone installation.

To run without a login service:

```sh
python3 -B -m live_gantt
```

The foreground command reads `CLAUDE_CONFIG_DIR` and `CODEX_HOME` from its environment, falling back to each provider’s standard per-user directory. It also reads optional Claude quota captures from the application’s stable storage. No installation or provider settings changes are required.

To use another port for this foreground process, pass `--port 4792`. Stop it with Control-C. The `-B` flag prevents bytecode caches.

## Privacy

The chart runs locally. The server binds only IPv4 loopback and accepts only `127.0.0.1` and `localhost` Host headers with its port. It sends no cross-origin resource sharing headers; source text is rendered as text, not executable markup. There are no analytics or remote assets. It makes no outbound requests.

The sources are Claude Code’s configuration directory and Codex’s local session directory; `CLAUDE_CONFIG_DIR` and `CODEX_HOME` select alternate roots. Live Gantt never extracts credentials or changes provider settings. The service writes its own installed files and logs, never tasks or transcripts. Opt-in Claude quota capture stores only validated quota fields, a source session identifier and observation times in application-owned storage; raw status-line input, prompts and credentials are not saved. The last opened session keeps syncing for ten minutes after chart tabs close; reopening restores its current view. Quota capture history is an application-owned file with owner-only (`0600`) permissions and atomic replacement, bounded to 512 records and one mebibyte. It preserves original observation times, quota resets and bucket boundaries. A corrupt or unwritable file leaves the chart and your status line working.

## Compatibility

The Claude adapter is tested with Claude Code **2.1.292**. The Codex adapter reads local session metadata, turn activity, cumulative token records and rate-limit snapshots. These task, transcript and subagent schemas are private implementation details, not a public, versioned application programming interface (API). A provider update can change them without notice. Codex titles use its local title index, then a recorded prompt or agent nickname, with the identifier as fallback. It shows activity without inventing a task plan. Task discovery and task history or agent association are likely to break first: expect missing sessions, missing actual start/end times or detached agents. Metadata warnings can identify individual invalid fields.

Report a compatibility problem as an issue with your provider and version, what is missing, and any warning text. Do not attach private transcripts. The invented-data [compatibility fixture](tests/fixtures/claude-code-2.1.292) and [end-to-end test](tests/test_compatibility.py) pin the observed file shapes and must be refreshed against each supported Claude Code release. [Codex boundary tests](tests/test_codex.py) exercise invented session records rather than private transcripts.

## Account usage and session tokens

Compact account usage rows stay visible above the plan, even with no session selected. Each shows the recorded percentage, window duration, reset and exhaustion outlook; **Details** keeps source and observation information available on demand. Session tokens and milestone estimates use compact rows so the desktop timeline stays in view. Long timelines scroll within their panel. Native window duration determines the label; a primary window is not assumed to mean a particular number of hours. Percentages from different sessions or buckets are never added. Missing records are **Unavailable**, not zero. A missing window in the newest complete observation no longer appears as current usage.

**At current pace** estimates use recent recorded percentage changes, not token totals or estimated cost. Hover over **at current pace** or a forecast value to see the actual observation span used for each quota window. At least two compatible observations must span five minutes within the last thirty minutes. Changing the observing session, quota bucket, window duration or reset period, or a decreasing percentage, starts a new sample segment. No observed increase, insufficient history, a reached limit and a reset expected before exhaustion each receive their own explanation. No adapter provides a native exhaustion forecast.

If an estimated exhaustion time passes before a fresh reading arrives, the display changes to **Awaiting fresh usage**. It does not infer that the limit was reached; **Limit reached** requires a recorded 100% usage reading.

A quota observation becomes **Stale** after five minutes without a confirmed local change or provider measurement, or when its reset passes. A stale record has no current exhaustion estimate. Repeated identical local captures do not refresh confirmed-change time or create forecast samples. Local capture time is distinct from provider measurement time. These are local estimates based on available records, not a promise of future access.

### Quota-aware task forecasts

Task and milestone forecasts account for fresh shared-account quota from the task's provider. A confirmed 100% reading pauses projected work until its reset; a fresh **At current pace** exhaustion estimate inserts a conditional pause from that estimated time until reset. Overlapping quota pauses are combined. Affected task finishes, lane successors and explicit dependents move accordingly. Completed work and recorded agent or process activity remain unchanged.

`metadata.provider` can be `claude`, `codex` or `external`. Omitted values use the session provider; a task declaring only external runs, with no agents, defaults to `external`. External work receives no provider quota pause, though its dependencies can still delay it. Only the provider's shared-account bucket applies. Model or feature limits do not apply without an attributed task, and Live Gantt never switches models or providers automatically.

Stale or unknown quota adds no invented delay. A fresh reached limit with no reset leaves the affected finish unknown. These estimates remain conditional on current consumption pace and the known reset times. The task row marks quota pauses with hatching and a label, keeping the forecast compact. Reset-based times stay anchored to the reported reset rather than drifting forward with the browser clock. The later milestone estimate keeps the existing combined-variance calculation and also respects projected quota pauses.

**Session tokens** belongs only to the selected session and excludes child sessions. It shows total, input, output, cached input, cache writes and reasoning output when recorded; missing categories stay **Unknown**. Cached input and reasoning can be subsets of other categories, so do not add the displayed categories to reconstruct the provider’s total. Codex uses the newest valid cumulative thread total. Claude Code deduplicates recorded response usage.

### Opt-in Claude quota capture

Codex quota snapshots are read from local logs. Claude Code exposes quota fields to its status-line command; its ordinary transcript is not a reliable quota source. To enable capture, place `python3 -B -m live_gantt.capture_usage` before your existing status-line renderer. It passes standard input through byte-for-byte, including on malformed input or a storage failure, and saves only normalized quota observations. Nothing installs or changes your status-line settings automatically.

The command needs to find the installed package when Claude runs outside the checkout. Set `LIVE_GANTT_HOME` inside the status-line command or its existing script, using the stable application directory from the installation table. For macOS:

```sh
LIVE_GANTT_HOME="$HOME/Library/Application Support/live-gantt"
```

For Linux:

```sh
LIVE_GANTT_HOME="${XDG_DATA_HOME:-$HOME/.local/share}/live-gantt"
```

If you use a custom Linux data directory, keep this value consistent with the service installation. Add the following upstream stage to the command after that assignment. Replace `EXISTING_RENDERER_COMMAND` with your existing renderer command, preserving its arguments and quoting; retain a copy of the original setting before making the change:

```sh
PYTHONPATH="$LIVE_GANTT_HOME${PYTHONPATH:+:$PYTHONPATH}" \
  python3 -B -m live_gantt.capture_usage | EXISTING_RENDERER_COMMAND
```

Keep the `LIVE_GANTT_HOME` assignment in the command or script that Claude invokes; setting it only in an unrelated terminal does not configure a desktop-launched session. If no renderer exists, the following capture command is sufficient after the same assignment and deliberately leaves the status line blank:

```sh
PYTHONPATH="$LIVE_GANTT_HOME${PYTHONPATH:+:$PYTHONPATH}" \
  python3 -B -m live_gantt.capture_usage > /dev/null
```

Store that command in Claude Code’s status-line configuration yourself. The capture history stays bounded in the stable application directory’s usage storage. It does not write to Claude Code’s configuration or transcript directories.

## Read the chart

Sessions from both providers are ordered by source activity. Each picker entry identifies its provider. Codex root sessions and orphans are selectable; recorded child sessions appear under their root. Their **Session activity** lane shows recorded turn starts and completions: **Idle** means between turns, not permanently finished. Quiet unfinished work becomes **Silent**. Codex supplies recorded native plans, turn activity and token usage. Missing estimates remain unknown.

Claude Code sessions are ordered by the last parent transcript or task-file change. The session picker (click it or press `/`) initially shows the ten most recently updated work sessions, plus the selected session if it is older. Each entry shows its title, the first prompt when a custom title replaces it, **Active** when its transcript changed in the last 5 minutes (otherwise how long ago it changed), the project and its task and agent counts. Search also finds older work by title, prompt, project or provider. **All sessions** includes older sessions, background `claude -p` runs such as commit gates, and chats without recorded work. Use the arrow keys and Enter to switch and Escape to close. The address keeps the selected session as `#session=<id>`, so a bookmark or reload returns to it. Sessions without tasks that last changed more than 14 days ago show only their identifier and date until selected. A session's custom title (the latest rename) takes precedence over the first prompt you typed, and the project shown is the session's latest working directory; command caveats, slash-command echoes and other harness markup are skipped. Tasks are grouped by lane, in task identifier order. Current running work reserves its lane before pending work. Done bars use recorded start and completion times; a task without those times has a `no history` marker rather than invented dates.

The timeline opens as an overview: planned tasks and one row per workflow. Workflow rows show the workflow's recorded state and counts of recorded agent states; a failed or stopped agent never counts as completed. These counts describe observed activity, not a workflow completion percentage or a promise that all work has been scheduled. Other agents share one **Other agent activity** row. Codex's parent session activity remains visible. All child agents and external runs stay collapsed until you explicitly expand their parent row; previous detailed expansion settings do not open them automatically.

Running task bars end at now and have a patterned projection for the remaining estimate. Overrunning tasks use hatching. Pending tasks waiting for their lane or prerequisites are **Queued**, with labels such as `after #56`. **Blocked** means a missing prerequisite or dependency cycle prevents a forecast. An unknown estimate leaves the task unestimated and marks affected successors. An activity group's bar shows only its first-to-last recorded span, not continuous work or a finish forecast. In expanded details, a running agent names its latest recorded step; other agent bars name their state. Silent agents have had no transcript activity for 30 minutes without finishing. External runs stay live until their `ended` timestamp is set, even if their log goes quiet.

The overview keeps each unfinished milestone visible with its own progress bar, **Expected finish**, **Later estimate**, and estimated time remaining. Percentages come from the plan's weighted progress reports; finish estimates can also use live agent progress. Independent milestones are not averaged into a misleading overall percentage. **Needs estimates** means no remaining task has an estimate; **Forecast unavailable** means the plan cannot currently produce a finish date. Missing dates never become a guessed deadline. Completed milestones are listed together, and **Planned tasks complete** refers to the task plan, not to all possible development or agent activity. A session containing only agents shows activity without inventing progress or a finish date. The primary monitor status flags stale plans, failed work and monitoring gaps; technical details remain available under **Details**.

The default timeline view is **12 h**, with now one quarter of the way across the visible chart. Use **12 h**, **3 days**, **Week** or **Fit**; the browser remembers your choice when local storage is available. **Fit** uses the overview rows and includes individual children only when expanded. The view follows now until you scroll horizontally; **Follow now** resumes it. **Status key** explains bar patterns without occupying the chart by default.

Task rows show their names and current status. Tasks finished more than two hours ago fold into one summary row per lane and milestone (for example `Core: 12 done, 29 h`); select the summary to expand them. Select a task or workflow to open its details alongside the timeline, or below it on a narrow screen. Keyboard focus moves to the inspector's close button; **Escape** or **Close** returns focus to the selected work. Raw task metadata stays inside a disclosure. Connection age updates visually while assistive technology is notified only when connection state changes. Reduced-motion preferences are respected.


## Freshness

The chart is only useful if it is never silently out of date, so one set of rules judges every session, in [freshness.py](live_gantt/freshness.py). A finding is reported when:

1. an unfinished task lacks a valid `estimate` or a `lane`;
2. an in-progress task file has not changed for 15 minutes while the session transcript changed within the last 5 minutes;
3. an in-progress task has no numeric `progress`;
4. a run that has not ended names a `log` that is missing, or whose log has not changed for 10 minutes while the session is active;
5. a run that has not ended declares a `pid` that no longer exists, and its log, if it has one, shows no life.

Completed and deleted tasks and task files that cannot be read never produce findings. When findings exist, **Details** lists them; each in-progress task shows how long ago it was updated and, for a run finding, what was declared against what was observed, for example `declared running; observed: silent 23 min`.

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
| `/api/lists` | `{ "lists": [...] }`, ordered by recent source change. Each entry has `id`, `provider` (`claude` or `codex`), `sourceId`, `title`, `prompt` (the first prompt when a custom title hides it, else empty), `project`, `updatedAt`, `recent`, `active` (transcript changed within 5 minutes), `chartable` (Claude task/agent files, or a discovered Codex session), `taskCount`, `agentCount` and `headless` (a non-interactive `claude -p` run). Older entries that cannot draw a chart carry identifier/date with an identifier title and empty project. |
| `/api/state?list=ID` | Full selected-session model with `provider`, `sourceId`, `tokenUsage`, tasks, agents, `workflows` (each run's `id`, `name`, `description`, `status`, `started`, `ended` and unassigned `agents`, which add `phase` and `workflow`), milestones, warnings, `freshness`, `sync`, `liveness`, `monitor`, `transcriptUpdatedAt` and an `observedAt` projection timestamp; an unknown list returns 404; a malformed identifier returns 400; a temporary source failure before the first successful read returns 503. Existing Claude identifiers are preserved; Codex identifiers use a `codex-` prefix. |
| `/api/usage` | `{ "providers": [...] }` with each provider’s quota windows, freshness and forecast. |
| `/events?list=ID` | Server-Sent Events (SSE): initial and changed `state`, `lists` and `usage`, and a `heartbeat` event plus comment every 15 seconds. A removed session emits `missing`. |
| `/events/usage` | Initial and changed `usage` and `lists`, plus heartbeats, without requiring a selected session. |
| `/favicon.ico` | Inline Scalable Vector Graphics (SVG) Gantt icon using the page's color tokens. |

Each usage provider has `provider`, `status` (`available`, `stale` or `unavailable`), `sourceId`, `windows` and `message`. Each window has `id`, `bucketId`, `label`, `windowMinutes`, `usedPercent`, `resetsAt`, `capturedAt`, `observedAt`, `measuredAt`, `freshnessBasis`, `stale` and `forecast`. `capturedAt` is the latest local capture; `observedAt` is the last distinct local observation; repeated captures leave it unchanged. `measuredAt` is supplied only when a native provider measurement time is known. `freshnessBasis` is `provider_measurement`, `recorded_change` or `unknown`. Forecasts have `status`, `exhaustsAt`, `basis` and `sampleSeconds`; status is `predicted`, `reached`, `resets_first`, `insufficient_history`, `no_observed_pace`, `stale` or `unavailable`. Basis is `recorded_changes` or `null`. No finite forecast means `exhaustsAt: null`, never infinity. Dates are ISO 8601 strings in Coordinated Universal Time (UTC); missing dates, durations and token values are `null`.

Projected tasks include `projectedSegments`, an ordered list of `{start, end}` work intervals, and `quotaDelays`, an ordered list of `{start, end, provider, reason}` pauses whose reason is `reached` or `predicted`. Existing `projectedStart` and `projectedEnd` remain the enclosing forecast interval. `projectionAnchored` marks a reset-anchored task projection; milestones expose `p50Anchored` and `p90Anchored` for their corresponding finish estimates, and the root `quotaConstrained` flag reports that quota affected the projection. Quota changes can update task and milestone forecasts even when no task file changes.

`tokenUsage` contains `inputTokens`, `cachedInputTokens`, `cacheWriteTokens`, `outputTokens`, `reasoningOutputTokens` and `totalTokens`, each a nonnegative integer or `null`.

`freshness` is `{stale, findings, sinceChangeSeconds, sessionActive}`. Each finding has `rule` (`missing-schedule`, `stale-progress`, `missing-progress`, `silent-run` or `process-gone`), `taskId` and a plain-sentence `message`; run findings add `observed`, such as `silent 23 min`, `log not found` or `process gone`. `sinceChangeSeconds` counts from the newest task-file change and is `null` without task files. Each task carries `updatedAt`, its file's modification time, and `readable`. Each run carries `logUpdatedAt`, `pid` and `pidAlive`, which is `null` without a `pid` or once the run has ended.

Local sources are checked every two seconds for connected views and the last opened session, even when its tab is hidden or closed. Failed reads retain the last complete snapshot and retry automatically; provider discovery failures are isolated. Restarting the service reconstructs recorded progress from disk. Claude reads task files, transcript history and workflow journals; Codex reads native plan updates and turn activity. Unrecorded progress cannot be inferred, and ending a Codex turn does not complete its plan.

`sync` is `{status, checkedAt, error}` on session responses and heartbeat events. `status` is `current` or `retrying`; `checkedAt` is the last successful source read, including unchanged readings, or null before success; `error` is a sanitized message or null. An unchanged model needs no new state event: heartbeats confirm successful checks. **Details** shows Synced, Retrying or Disconnected, with the check time in its tooltip. It describes recorded-source synchronization, not whether an agent has reported every real-world step.

`liveness` is `{status, checkedAt, evidence, detail}` on session responses and heartbeat events. Status is `alive`, `exited` or `unknown`. On macOS, Claude is checked against its native session registry and process-start identity; Codex is checked against its session writer lock and owning process. `alive` confirms runtime ownership, not forward progress. A known process exiting or being replaced can yield `exited`; missing, ambiguous, unsupported or inaccessible evidence yields `unknown`. `checkedAt` timestamps the observation; `evidence` identifies its basis and `detail` explains it without exposing process identifiers or command lines. Unverified platforms report unknown.

Account usage sits above the session selector because quotas apply across sessions. The left-aligned selector starts the session workspace; its monitor, tokens, plan progress and timeline follow beneath it. Switching sessions does not change the scope of account usage.

The compact view leads with one actionable status and its reason: **Working**, **Needs you**, **Waiting for quota**, **Idle**, **Plan complete**, **Needs attention**, or **Unable to verify**. **Details** separates **Harness** liveness, **Plan** freshness and **Sync** source/transport health. Checks continue for the last opened session while its tab is hidden. Neither recent file changes nor a successful read can establish harness liveness.

`monitor` is `{state, summary, lastActivityAt, issues}` on session responses and heartbeat events. `state` is `working`, `needs_you`, `waiting_for_quota`, `idle`, `plan_complete`, `attention` or `unverified`. `summary` explains the verdict; `lastActivityAt` is the latest meaningful execution observation or null; `issues` is a bounded list of plain-text reasons. Private source evidence never enters the response.

Working requires verified ownership and recent execution evidence. Token accounting, metadata and repeated reads cannot refresh it. Five minutes without meaningful activity becomes **Unable to verify**. A plan left in progress for fifteen minutes without updates, a recorded failure, an unanswered recorded question, or conflicting evidence needs attention. Completed plan steps cannot hide pending or failed background work. A confirmed quota pause becomes **Waiting for quota** only when ongoing execution does not contradict that verdict.

Claude's verified native waiting phase produces **Needs you**. For ordinary Codex command-line sessions, Live Gantt connects to the existing shared local daemon. Pending native approvals and questions produce **Needs you** on the next dashboard check, without waiting for a transcript update, and answering or dismissing the request clears it. Live Gantt only observes requests; it cannot approve, reject or send work to the session.

It reads the exact session’s native waiting flags with `thread/read` and `includeTurns: false`, and listens for status changes. These metadata-only reads do not resume, subscribe to, or take control of the session. Nothing starts a daemon or changes how approvals are answered.

The connection validates its source and reconnects automatically. Broken or unsupported coverage produces **Unable to verify**. The Codex desktop app, literal `codex --no-daemon` sessions and sessions that fall back to an embedded server have no external live interface. To monitor an isolated terminal session, use the installed launcher:

```sh
live-gantt-codex
live-gantt-codex -C . "Review this project"
live-gantt-codex resume <session-uuid>
```

The installer places this command in the per-user executable directory; that directory must be on your shell's `PATH`. The launcher starts one private native server and the normal Codex terminal interface. It uses native remote-client execution, keeps the shared daemon separate, and closes its own server when the terminal exits. It reads the installed Codex executable and source root settings, with explicit environment values taking precedence. It leaves provider configuration, hooks and your Claude status line unchanged.

This first version accepts a working directory, model, sandbox, approval policy, web search, inline terminal mode and a quoted prompt for new sessions. Resume and fork require an explicit session identifier and preserve saved permissions. Profiles, arbitrary configuration overrides, additional writable directories and explicit remote or `--no-daemon` flags are rejected. Native permission checks reject unsupported configurations before the terminal starts. Run `live-gantt-codex --help` for the supported options. Existing embedded sessions cannot be attached automatically; exit and resume them through the launcher when convenient.

Live Gantt discovers private runtimes through small, owner-only registrations, validates their process identity and reads exact-session native status. Registrations contain no prompts or credentials, and terminal output is not recorded. Saved logs remain the source of recorded progress and failures; they do not reliably contain every approval prompt. Disconnects or forty seconds without a valid monitor update become **Unable to verify**, preserving the last known report under **Details**. Usage events and ordinary connection heartbeats cannot renew a missing monitor report.

State events carry full snapshots, including after reconnect; quota changes recalculate forecasts without a task change. The page closes its stream while hidden and reconnects when visible. A silent-stream watchdog, bounded requests and malformed-event recovery request fresh snapshots automatically. Temporary outages preserve the selection; only confirmed removal clears it. The browser advances now-dependent dates between updates; explicit state requests recalculate projections.

```sh
python3 -B -m unittest
node --check live_gantt/static/app.js
```

Tests use invented fixtures and temporary directories, never your provider configuration. HTTP tests open an ephemeral loopback listener; a sandbox that denies binding sockets cannot run those tests. On macOS, checks can run at utility priority with `taskpolicy -c utility python3 -B -m unittest`.

Rendered layout checks use Playwright and Chromium available in the development environment:

```sh
node --test tests/browser_layout.test.cjs
```

`PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH` can select an existing Chromium executable. These checks cover viewport height, independent scrolling, sticky timeline labels, Overview switching, disclosure readability and keyboard interaction at wide, narrow and short window sizes.

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
