# Architecture

## Sources and ownership

[providers.py](live_gantt/providers.py) joins readers under monitor serialization. Claude identifiers stay intact; Codex uses `codex-`. Failures stay isolated. Nothing makes provider requests; quota comes from local records. Web conversations are excluded; formats are private.

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

## Harness liveness

[liveness.py](live_gantt/liveness.py) verifies runtime ownership independently of transcripts, plan freshness and successful source reads. On macOS, Claude's native session registry binds its identifier to a process identifier and UTC process-start signature; an exact operating-system match is required. Codex requires the exact session writer lock to be held, an unambiguous matching open-file owner, and stable file/process identities across rechecks. Lock queries are read-only and never acquire or release the harness's locks.

`alive` proves only owner existence. `exited` requires a previously identified process to disappear or change identity. Missing, ambiguous, unsupported or denied evidence is `unknown`; file timestamps never prove liveness. Probes have deadlines, bounded output and short-lived caches; process identifiers, paths and command lines stay private. Source failures do not suppress runtime checks. Snapshots and heartbeats carry liveness separately; browser checks expire. Verified Claude phases supply busy, waiting or idle evidence; generic registry writes are not activity.

`ProviderStore` owns read-only Codex observers. They share a bounded Unix WebSocket transport to the shared daemon, read `thread/read` with `includeTurns:false` and consume native status notifications. Exact threads must be loaded; observation never resumes or takes approval ownership. The desktop app and embedded CLI sessions have no endpoint. Socket symlinks must resolve to same-user private targets.

Observers retain only request metadata and flags. Gaps require fresh evidence; timeouts discard waiting. Observers close without starting servers or sending decisions. Unsupported coverage is unverified; receipt times are not activity.

[codex_launcher.py](live_gantt/codex_launcher.py) owns isolated sessions, reached only through the explicit `live-gantt-codex` command; no shell routing replaces `codex`. It starts a private server, runs a permission preflight and launches the native remote client. Built-in profiles must match the native sandbox; custom profiles fail preflight. It never answers approvals or starts turns. Resume/fork preserve context. Owned processes and leases are cleaned up. Provider settings and terminal output stay untouched.

[isolated.py](live_gantt/isolated.py) owns bounded, owner-only runtime registrations outside provider directories. They bind a canonical socket and Codex root to a same-user process and its start signature. Missing, replaced, unsafe or dead registrations revoke isolated coverage. Observers reuse the native transport; exact-session isolated status takes precedence over the shared daemon.

## Actionable monitoring

[monitoring.py](live_gantt/monitoring.py) owns state, reason, last activity and bounded issues; reader evidence stays private. Messages, tools, logs and turns count as activity, not accounting or reads. Native phases require ownership. Failures and unresolved questions prevent completion. A later Claude transcript transition supersedes an older native observation; current contradictions still need attention.

Only current native pending approvals/input or explicit waiting proves input is needed. Codex logs omit transient approvals and some errors; recorded questions need attention, silence becomes unverified after five minutes, and liveness never proves progress. Fifteen minutes without an in-progress plan update needs attention even with a quiet transcript. Completion requires finished tasks, an idle parent and no pending/failed background work. Confirmed quota waiting cannot override continuing execution.

Snapshots use the source-read clock. Disconnects or forty seconds without monitor data become unverified; unrelated messages cannot renew it. Outages retain the last-known report.

## Local transport and idle behavior

[endpoint.py](live_gantt/endpoint.py) owns address allocation and discovery. An account holds a private lifetime lock, binds its saved port, then the default, then an operating-system-assigned port; only address-in-use permits fallback. Explicit ports bind exactly. The actual port and process identity are saved atomically with owner-only permissions and retained for restart. Discovery validates the record, lock, current-user process and exact listener before a bounded HEAD probe, then rechecks identity. Unsafe or stale records never trigger legacy fallback. Without a record, the current user's registered login service can identify a legacy listener on the default port. No discovery reads provider data; loopback remains unauthenticated. Commands, installer output and SessionStart context share this discovery.

[server.py](live_gantt/server.py) binds exclusively to `127.0.0.1` and requires exactly one Host header matching that address or `localhost` with the bound port before dispatching any method. Fixed routes prevent traversal. Responses omit cross-origin resource sharing headers and include a same-origin content security policy. JSON serialization rejects non-finite numbers; browser source text enters text nodes.

Read-only routes expose lists, selected state and quota. Event streams send initial and changed snapshots plus heartbeats; confirmed removal emits `missing`. [README interfaces](README.md#interfaces-and-verification) defines routes and fields.

`Monitor` serializes reads every two seconds for open streams and the last opened session, even with hidden tabs, until `BACKGROUND_SESSION_SECONDS` pass without a read of it; the worker then stops. Discovery errors retain listings. Failed reads preserve the model and report `sync.status: retrying`; `sync.checkedAt` advances only on successful reads, including unchanged data. Heartbeats separate source health from connection liveness. Initial failures return 503; confirmed missing sessions return 404. Reconnects receive full snapshots. Quota changes invalidate projections; unconstrained browser dates advance while reset anchors stay fixed.

## Browser and process lifecycle

[The browser](live_gantt/static/app.js) keeps account usage independent of selection. A compact session/monitor header leaves a viewport-filling workspace. Timeline, work inspector and sidebar scroll independently. Narrow screens switch between sidebar and timeline. Sidebar disclosures expand in flow; monitor details anchor to its row. Server owns staleness, pace and milestone forecasts. Passed forecasts never imply reached limits; missing P50 differs from missing P90. “Planned tasks complete” covers only the plan; agent-only sessions have no invented progress.

The picker lists recent chartable work and selection; search reaches older work. `#session=<id>` survives reloads and discovery failures through direct checks; partial listings cannot replace it. One stream serves session or quota views. Hiding closes it; resuming gets a full snapshot. Only confirmed removal clears selection.

Zoom is browser-owned. Counts never imply percentage; storage failures cannot prevent rendering. Termination stops service; transcripts preserve history.

## Plugin, installation and service lifecycle

The plugin manifest owns release identity; its skill owns task metadata. [python_support.py](live_gantt/python_support.py) owns the minimum Python version and interpreter selection. Hooks call it before application imports; interactive commands use its module launcher. Older interpreters hand off to the first supported known installation, preserving arguments, environment and input. Probes are bounded; unavailable Python leaves hooks quiet and successful but makes commands fail visibly. Hooks advise on TaskCreate without modifying tasks or blocking tools. Freshness hooks avoid history scans; Stop blocking has recursion protection and persisted cooldowns. Read errors stay silent. The dashboard never writes provider sources or sends session instructions.

[installation.py](live_gantt/installation.py) owns paths, service identity and replacement. Copies are independent of plugin caches and run under supported `python3` from PATH, else the installer's interpreter. macOS uses Application Support; Linux honors absolute `XDG_DATA_HOME`/`XDG_CONFIG_HOME` and passes the data location to its service. Updates stage before replacement and write versions after registration; reporting success additionally requires verified readiness. Registration failure retains the old marker; rollback is not transactional. Source overrides persist; empty values clear them. The Codex executable path preserves symlinks across updates. Provider settings and Claude's status line stay unchanged.

Platform services use low-priority, non-idle scheduling. Rendering quotes paths, escapes specifiers and disables environment expansion. SessionStart refreshes only older installations; absent, malformed, equal or newer markers do no installation work. Uninstall removes service, package, overrides and marker, retaining logs, capture history and the inactive endpoint record. Marker removal preserves opt-out.
