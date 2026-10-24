# Architecture

## Sources and ownership

[sources.py](live_gantt/sources.py) reads Claude plans and transcripts. Task deletions persist; identity reads are bounded; custom titles win. Assistant response ends leave runtime unknown: Stop hooks may still run. Native status or a parent-turn event confirms the phase. Task or agent files establish chartability.

Readers consume complete records by offset. Replacement, shrinkage, same-length changes or changed consumed-tail checkpoints rebuild history; edits preserving checkpoints can escape detection. Invalid task writes retain prior data without contributing progress. Only meaningful activity resumes terminal agents or clears hand-back; metadata and accounting cannot. Silence never means completion. Workflow journals supply identity and progress; results and hand-back finish agents. Running children keep workflows running; unfinished children inherit terminal workflow state. Metadata assigns agents to tasks.

External runs require explicit endings; processes and logs prove observations, never completion. Invalid metadata warns and preserves unknown estimates; unknown keys cannot schedule work.

[codex.py](live_gantt/codex.py) indexes sessions under `CODEX_HOME` or its default; archives load on selection. Changed files re-read bounded metadata before identifying sessions. Parent metadata joins children; orphans remain selectable. Native turns control activity; recorded failures remain failures. Native plan events and acknowledged `update_plan` calls supply steps. Turn completion never completes a plan. Estimates, percentages and dependencies are not inferred.

Root tokens exclude children: Codex uses cumulative totals; Claude deduplicates responses. Missing categories stay null; overlaps never add.

## Quota observations and forecasts

[usage.py](live_gantt/usage.py) owns quota validation, identity, freshness and forecasts. Quota reads bounded recent Codex tails, excluding archives, plus optional Claude captures. Failed reads still age cached quota without blocking the other provider. Failures retain the last success and timestamp plus a sanitized error; replays cannot mask failure. Native roles and durations remain intact; tokens or money never imply percentages. Complete observations replace buckets; missing windows are unavailable. Status is available with any fresh window, stale with only stale windows, otherwise unavailable.

Percentages are finite within 0–100; durations are positive integers or null. ISO 8601 dates use Coordinated Universal Time (UTC). Capture, observation and provider measurement times remain distinct. [README interfaces](README.md#interfaces-and-verification) defines fields and states.

Samples share source, bucket, role and duration. Resets match exactly; reported timestamps stay intact. Missing windows, changed resets or decreasing percentages break continuity. Two samples must span five minutes within thirty minutes. Consumed percentage points over elapsed time give pace; remaining percentage gives exhaustion. No increase, insufficient history, reached limit and reset-before-exhaustion are distinct statuses. Observations older than five minutes or past reset are stale, without forecasts. Adapters supply no verified native forecast; estimates use `recorded_changes`. Unavailable times are null.

[capture_usage.py](live_gantt/capture_usage.py) is an opt-in Claude status-line stage. Input passes through byte-for-byte despite validation or storage failure; it never installs settings. Locked atomic writes retain only validated quota, identity and times, never raw input, prompts or credentials.

## Projection

[schedule.py](live_gantt/schedule.py) owns pure `project(tasks, now, *, provider=None, usage=None)`; omitted options preserve existing calls. It copies inputs into projections, milestones and warnings. Actual times and activity never move; missing history stays missing. Completed tasks lacking end times are available now to dependents, without invented actual bars.

Lanes reserve running work first, then pending tasks in natural identifier order. Multiple running tasks warn; pending tasks wait for all. `blockedBy` adds cross-lane prerequisites. Starts follow now, predecessor and blocker finishes. Prerequisite waits use `waitingFor`. Missing blockers, cycles and unknown estimates suppress affected forecasts; unknown never means zero duration.

Elapsed-hour Program Evaluation and Review Technique (PERT) estimates supply mean `(low + 4 * likely + high) / 6` and standard deviation `(high - low) / 6`. Running work uses remaining fraction `1 - max(progress, liveProgress) / 100`; pending work uses the whole estimate. Zero progress beyond expected elapsed duration marks overrun. Calendar overflow warns and leaves unsupported dates unknown.

`metadata.provider` selects `claude`, `codex` or `external`; the session provider is default, except tasks with external runs and no agents. Only attributed shared-account limits constrain work. Fresh reached or predicted limits pause affected work and dependencies until reset; overlaps merge. Stale/unknown usage adds no delay; a reached limit without reset makes affected forecasts unknown. No provider fallback is automatic. Work segments exclude pauses; their envelope and milestone flags preserve reset anchors in the browser. Quota changes invalidate projections independently.

Milestones weight completed tasks fully, in-progress tasks proportionally, pending zero. `hasRemaining` follows status even at 100%; `hasEstimate` means any remaining work is estimated. The 50th-percentile (P50) finish uses expected durations. The 90th-percentile (P90) adds `1.2816` times root-sum-square deviations on the controlling prerequisite chain, counting shared tasks once; mean controls joins, variance breaks ties. Deviations scale by remaining fraction. Both passes apply quota pauses. Independent durations and join approximations are assumptions; unknown required work suppresses affected finishes, while calendar overflow may leave P50 known and P90 unknown.

## Plan freshness

[freshness.py](live_gantt/freshness.py) owns pure `assess(tasks, transcript_updated, now)` and stale-plan rules. It returns findings, session activity and age of the latest task-file change. Discovery shares `session_active`; browser and hooks never recreate rules. Findings cover missing schedules or numeric progress, old progress during activity, quiet/missing run logs, and exited processes without later logs. Written logs outweigh exited/reused process identifiers; process-exit findings replace quiet-log findings for that run. Completed, deleted and unreadable tasks yield no findings, avoiding stale reports during partial writes.

## Local transport and idle behavior

[server.py](live_gantt/server.py) binds exclusively to `127.0.0.1`. Before dispatching any method it requires exactly one Host header matching `127.0.0.1:<bound-port>` or `localhost:<bound-port>`. Fixed routes prevent filesystem traversal. Responses omit cross-origin resource sharing headers and include a same-origin content security policy. JavaScript Object Notation (JSON) serialization rejects non-finite numbers, and the browser inserts source text through text nodes.

The transport supplies fixed assets, session discovery, a selected-session snapshot and Server-Sent Events (SSE). The favicon's inline Scalable Vector Graphics (SVG) mark takes its status colors from the stylesheet. `Monitor` owns source access, cached hashes and reader counts. It starts polling only while a stream is watched; the final reader wakes and ends the worker. Explicit requests read without starting background polling. Handlers detect closed sockets while waiting and bound socket writes so stalled clients cannot retain them indefinitely. Heartbeats exist only for connected streams.

Hashes include normalized source and freshness verdict, excluding projections and seconds-since-change. Time alone can therefore emit a newly stale `state` without broadcasting unchanged models each tick. Discovery changes emit `lists`; timestamped heartbeats distinguish inactivity from lost connections. Explicit requests always recalculate projection. The server owns polling/heartbeat intervals; exported overrun time and remaining-work status let the browser advance time without duplicating scheduling.

## Browser and process lifecycle

[app.js](live_gantt/static/app.js), [style.css](live_gantt/static/style.css) and [index.html](live_gantt/static/index.html) form one buildless, offline page. The selected session provides the work context; its progress overview precedes the detailed timeline. Each unfinished milestone has a prominent server-derived percentage, progress bar, expected finish and later estimate. Milestones remain independently weighted: the browser invents neither an aggregate percentage nor an aggregate probability. Dates are estimates, not deadlines. Missing P50 is distinct from missing P90; no remaining estimates prompts estimation, while an incomplete forecast remains unavailable. Completed planned tasks are named as such, without implying that agent activity or all development has ended. Agent-only sessions show activity without fabricating a task percentage or finish date. Stale or disconnected state is visible beside the forecasts.

A listbox popover supports filtering and multi-line session identity. It includes chartable and selected sessions; All sessions adds the rest. Every filter word must match identity, project or identifier, and a browser-owned cap bounds rendered results. Keyboard navigation, Enter, Escape and `/` provide selection, dismissal and opening. `#session=<id>` persists through replacement history, reloads and hash changes; absent a link, the first chartable session is selected. Selected identity survives discovery updates.

The browser owns zoom, scrolling, visible-agent filtering and time advancement from the server's projection timestamp. Fit includes task history and forecasts, task children, detached agents and workflow runs, so historical agent-only work remains reachable. The default zoom and past/future share are browser constants. Horizontal scrolling pauses following; Follow now resumes it. Connection announcements change only when connection state changes, while a separate visual counter shows receipt age without repeated announcements. No clock-only request is needed to move the now line or projected dates.

Readable task subtitles expose state, progress, update age and observed run findings. Queued state, prerequisites and freshness messages come directly from the server. Selecting work opens docked details, moves focus to Close and restores focus to its trigger on close; Escape also closes it. Details follow the selected record across updates and close when it disappears. Raw metadata is collapsed behind a disclosure. The status key is another disclosure, keeping explanatory material available without dominating the work view. Status patterns and outlines supplement color, tabular numerals aid comparison, and reduced motion disables pulses.

Agent/run bars show descriptive activity or state. Labels fit inside the right edge or follow the bar without clipping. Old completed tasks fold by lane and milestone with count and recorded hours; finished/failed attempts fold into expandable marks, while running/silent agents retain rows. Workflows have their own lane, badge and expandable finished children. The browser owns completion recency. Only zoom and session-scoped expansion persist in guarded local storage; storage failure never prevents rendering.

[__main__.py](live_gantt/__main__.py) chooses the configuration root and loopback port, starts no source work by itself, and shuts down on interruption or termination. Transcripts reconstruct session history after downtime.

## Plugin and task planning

[The manifest](.claude-plugin/plugin.json) owns release version and identity; [the marketplace](.claude-plugin/marketplace.json) names the repository root as its sole plugin. [The live-gantt skill](skills/live-gantt/SKILL.md) is the task-metadata contract, including dependencies. Hook registration resolves scripts from the plugin root, independent of session working directory. Each hook first calls [python_support](live_gantt/python_support.py), owner of the minimum version: Codex's daemon starts hooks under macOS's Python 3.9, so an older interpreter re-runs the hook under the first well-known supported one, or exits successfully. The TaskCreate hook examines the successful tool response and supplied metadata to add advisory context for missing scheduling fields. It never writes tasks or blocks tools, and malformed input is silent.

[The freshness hook](hooks/freshness.py) assesses task files and event transcript modification time without scanning history. Stop may block with findings, guarded against recursion and repetition. UserPromptSubmit adds findings as context; PostToolUse is throttled, reading only cooldown state while throttled. One per-session JSON file under `CLAUDE_PLUGIN_DATA`, or the installed-files directory, stores cooldowns. Unwritable state is silent to avoid repeated messages. Unreadable tasks, malformed events and errors exit successfully; tasks and transcripts are never written.

[The open command](commands/open.md) checks the local server. When unavailable it explains persistent installation and asks before invoking it, allowing task-planning guidance without an installed service.

## Installation and service lifecycle

[installation.py](live_gantt/installation.py) owns platform choice, stable user paths, service rendering, replacement and removal. Entry points delegate after checking the running Python version; no package installation occurs. The service runs a copy independent of plugin caches, under `python3` from PATH when supported, else the installer's own: Codex's daemon finds macOS's 3.9 first. macOS uses user Application Support; Linux honors absolute `XDG_DATA_HOME`/`XDG_CONFIG_HOME`, otherwise standard user directories.

An update stages replacement files before stopping the service, then replaces the package as a whole so removed files cannot survive. The version marker changes only after service registration succeeds. Failure reports an error and leaves the previous marker for a later retry; installation is not a rollback transaction. An explicit `CLAUDE_CONFIG_DIR` override is saved beside the installed package and reused on upgrades unless supplied again; an explicitly empty value clears it. Installation never changes Claude Code's files.

The [launchd template](launchd/local.live-gantt.plist) owns its label and low-priority standard-process policy: background process type would starve the chart, so idleness comes from having no readers. The [systemd unit](systemd/live-gantt.service) likewise uses low-priority, non-idle scheduling, login startup and user-journal logs. Rendering supplies interpreter, working directory and logs, quotes paths, escapes systemd specifiers and disables environment expansion to preserve literal dollar signs.

[SessionStart](hooks/session_start.py) shares installer paths and markers. Only an older installed stable version refreshes/restarts the opted-in service; absent, malformed, equal or newer markers do nothing. Errors are advisory and silent, while the open command exposes them. Uninstall stops/unregisters the service and removes package, override and marker, retaining logs. Marker removal preserves the opt-out across sessions; unchanged versions do no service work.
