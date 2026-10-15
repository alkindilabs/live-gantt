# Live Gantt

A local, live Gantt chart for Claude Code task lists and their agents. Pick a session from any project and see actual work, estimated work, external runs and milestone forecasts. The task list is the plan; there is no separate plan file.

Requires Python 3.11 or later. Uses only its standard library and a static page with Hypertext Markup Language (HTML), Cascading Style Sheets (CSS) and JavaScript. No installation, dependencies, build step, network services or internet access are needed.

## Run

From this checkout:

```sh
python3 -B -m live_gantt
```

Open <http://127.0.0.1:4791>. To use another port, pass `--port 4792`. The server always binds IPv4 loopback, and accepts only the `127.0.0.1` and `localhost` Host headers with that port. It sends no cross-origin resource sharing headers.

The default source is Claude Code's configuration directory. Set `CLAUDE_CONFIG_DIR` to read another configuration root. Sources remain read-only. The `-B` flag prevents Python from writing bytecode caches. Closing every chart tab stops source polling; reopening reconstructs the history from transcripts.

## Read the chart

Sessions are ordered by the last parent transcript or task-file change. A custom session title takes precedence over the first user prompt. Tasks are grouped by lane, in task identifier order. Current running work reserves its lane before pending work. Done bars use recorded start and completion times; a task without those times has a `no history` marker rather than invented dates.

Running bars end at now and have a patterned projection for the remaining estimate. Overrunning tasks use hatching. Pending tasks wait for their lane and blockers. An unknown estimate leaves the task unestimated and marks affected successors. Detached agents appear in an Agents lane when active in the visible window. Silent agents have had no transcript activity for 30 minutes and no terminal notification. External runs stay live until their `ended` timestamp is set, even if their log goes quiet.

Use **12 h**, **3 days**, **Week** or **Fit**. The view follows now until you scroll horizontally; **Follow now** resumes it. Hover or focus a task label to inspect its description, metadata, estimate and history; click to keep details open. Escape closes details. Connection age updates continuously; the browser reconnects after a disconnection. There is no animation when reduced motion is enabled.

## Task metadata contract

Agents write these fields through Claude Code's task metadata. Live Gantt never modifies tasks or transcripts. Unknown metadata keys do not affect scheduling. Malformed known values produce a warning on that task and fall back independently, so one bad field does not hide the session.

| Field | Contract |
| --- | --- |
| `weight` | Positive finite relative effort; default `1`. |
| `milestone` | Group name; an omitted name is displayed as Main. |
| `progress` | Finite number from `0` to `100`; default `0`. Used for milestone completion while in progress. |
| `live` | Object with `journal`, positive integer `planned`, and numeric `from` and `to` satisfying `0 <= from <= to <= 100`. The journal is read incrementally. Lines containing a `type` of `result` count as finished agents. Live share is `from + (to - from) * min(finished / planned, 1)`. |
| `estimate` | Object with finite elapsed hours `low`, `likely`, `high`, satisfying `0 <= low <= likely <= high`. No default estimate is invented. |
| `lane` | Row group; defaults to the milestone, otherwise Main. |
| `agents` | List of subagent identifiers, without the `agent-` filename prefix or file suffix. Matching agents become thin bars under the task. Unassigned agents remain in the Agents lane. |
| `runs` | List of objects with a text `label`, ISO 8601 `started`, `ended` timestamp or `null`, and `log` path or `null`. The end cannot precede the start. A null end means live; the log modification time provides the last activity. |

Journal and log paths may be absolute, begin with `~`, or be relative to the task list directory. No shell or environment-variable expansion is performed. Missing journals or logs produce warnings. ISO 8601 timestamps without an offset are treated as Coordinated Universal Time (UTC).

Example metadata for a task whose implementation has begun:

```json
{
  "weight": 3,
  "milestone": "First release",
  "progress": 25,
  "estimate": {"low": 1, "likely": 2, "high": 4},
  "lane": "Implementation",
  "agents": ["a81d04"],
  "runs": [
    {
      "label": "Integration checks",
      "started": "2026-10-15T10:00:00Z",
      "ended": null,
      "log": null
    }
  ]
}
```

The Program Evaluation and Review Technique (PERT) estimate is `E = (low + 4 * likely + high) / 6`, with standard deviation `sd = (high - low) / 6`. For a running task, `p = max(progress, live share) / 100`; remaining time is `E * (1 - p)`. A task past its expected elapsed duration with zero progress is marked overrunning, but its forecast still follows that remaining-time rule.

Milestone completion is the weighted share: completed tasks count fully, in-progress tasks count their explicit `progress`, and pending tasks count zero. The 50th-percentile (P50) forecast uses expected remaining durations. The 90th-percentile (P90) forecast adds `1.2816 * sqrt(sum of remaining variances)` along each controlling dependency chain, then takes the latest end. Remaining standard deviations scale by `1 - p`. These are planning approximations, not calibrated probabilities. Missing estimates, missing blockers and dependency cycles produce unknown forecasts. Completed tasks with no history do not block future work. Estimates extending beyond the representable calendar produce warnings.

## Interfaces and verification

The page uses read-only Hypertext Transfer Protocol (HTTP) endpoints:

| Endpoint | Response |
| --- | --- |
| `/api/lists` | `{ "lists": [...] }`, ordered by recent source change. |
| `/api/state?list=ID` | Full selected-session model with tasks, agents, milestones, warnings and an `observedAt` projection timestamp; an unknown list returns 404. |
| `/events?list=ID` | Server-Sent Events (SSE): `state` on selected-source changes, `lists` when the picker changes, and a `heartbeat` event plus comment every 15 seconds. A removed session emits `missing`. |

Sources are checked every two seconds only while an event stream is connected, or on explicit page/API requests. State events carry full models; unchanged sources do not emit state events. The browser advances now-dependent dates between updates. A fresh state request always recalculates the projection.

```sh
python3 -B -m unittest
```

Tests use temporary fixtures, never your Claude configuration. HTTP tests open an ephemeral loopback listener; a sandbox that denies binding sockets cannot run those tests. On this Mac, run checks at utility priority with `taskpolicy -c utility python3 -B -m unittest`.

See [ARCHITECTURE.md](ARCHITECTURE.md) for source and projection boundaries.
