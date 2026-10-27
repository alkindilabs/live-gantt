---
name: live-gantt
description: Use when creating or updating Claude Code tasks with TaskCreate or TaskUpdate for work spanning more than a few steps, or when starting background agents or external runs that belong to a task.
---

# Live Gantt task planning

Use Claude Code's task list as the plan. Create tasks with an estimate and lane so Live Gantt can schedule them, then keep status, progress, agent assignments and external runs current through TaskUpdate. Read the tool's current schema before calling it; the examples below describe tool inputs.

To open the chart, use `/live-gantt:open` and give the user the address returned by its shared discovery command. The service selects an available port, so do not assume port 4791. Discovery verifies the current operating-system user's server without requesting provider data. Sources and discovery do not combine local accounts; loopback access is not user authentication.

## Task metadata contract

Agents write these fields through Claude Code's task metadata. Live Gantt never modifies tasks or transcripts. Unknown metadata keys do not affect scheduling. Malformed known values produce a warning on that task and fall back independently, so one bad field does not hide the session.

| Field | Contract |
| --- | --- |
| `weight` | Set relative effort when tasks have unequal scope. Positive finite value; default `1`. |
| `milestone` | Set the delivery group when creating the plan; an omitted name is displayed as Main. |
| `progress` | Update as work advances. Finite number from `0` to `100`; default `0`. Used for milestone completion while in progress. |
| `live` | Set when a batch journal can measure progress. Object with `journal`, positive integer `planned`, and numeric `from` and `to` satisfying `0 <= from <= to <= 100`. The journal is read incrementally. Lines containing a `type` of `result` count as finished agents. Live share is `from + (to - from) * min(finished / planned, 1)`. |
| `estimate` | Set before starting or queueing every schedulable task; revise when scope changes. Object with finite elapsed hours `low`, `likely`, `high`, satisfying `0 <= low <= likely <= high`. No default estimate is invented. |
| `lane` | Set the serial work stream when creating every schedulable task; distinct lanes can run in parallel. Row group; defaults to the milestone, otherwise Main. |
| `provider` | Optional `claude`, `codex` or `external`; omitted values inherit the session provider. A task with only external `runs` and no `agents` defaults to `external`. Set this to the provider actually doing the work; it controls which shared-account quota can delay its forecast. |
| `agents` | Update after starting background agents that belong to this task. List of subagent identifiers, without the `agent-` filename prefix or file suffix; workflow agents' identifiers work too. Matching agents become thin bars under the task. Unassigned agents remain in the Agents lane, and unassigned workflow agents under their workflow run. |
| `runs` | Update when an external process starts or ends. List of objects with a text `label`, International Organization for Standardization (ISO) 8601 `started`, `ended` timestamp or `null`, `log` path or `null`, and an optional positive integer `pid` of the process. The end cannot precede the start. A null end means live; the log modification time provides the last activity. |

`blockedBy` belongs to the task itself, not its metadata. It is a list of prerequisite task identifiers. Set dependencies with `TaskUpdate(addBlockedBy=[...])`. Work in one lane is serialized in task identifier order; explicit blockers express dependencies across lanes. Add blockers before starting dependent work. Missing blockers and dependency cycles prevent forecasting.

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

Milestone completion is the weighted share: completed tasks count fully, in-progress tasks count their explicit `progress`, and pending tasks count zero. The 50th-percentile (P50) forecast uses expected remaining durations. Before quota pauses, the 90th-percentile (P90) forecast adds `1.2816 * sqrt(sum of remaining variances)` along each controlling dependency chain, then takes the latest end. Remaining standard deviations scale by `1 - p`. These are planning approximations, not calibrated probabilities. Missing estimates, missing blockers and dependency cycles produce unknown forecasts. Completed tasks with no history do not block future work. Estimates extending beyond the representable calendar produce warnings.

## Quota-aware forecasts

Fresh shared-account quota can delay projected work and its lane or dependency successors. A confirmed 100% reading pauses work until reset. A fresh predicted exhaustion time inserts a pause until reset, conditional on current consumption pace; overlapping pauses merge. Stale or unknown quota adds no delay, while a reached limit without a reset makes the affected finish unknown. Recorded activity and completed work never move.

Use `provider: "external"` for work independent of provider capacity. Model or feature limits are not applied without task attribution. Do not change provider metadata to hide a pause: it must describe who performs the work. Live Gantt does not switch models or providers automatically. Milestone estimates retain the combined-variance calculation and account for quota pauses in both expected and later finishes.

## Freshness

The plan must never be silently stale. Live Gantt reports a finding when:

1. an unfinished task lacks a valid `estimate` or a `lane`;
2. an in-progress task has not been updated for 15 minutes while the session is active (its transcript changed in the last 5 minutes);
3. an in-progress task has no numeric `progress`;
4. a run without `ended` names a `log` that is missing, or that has not changed for 10 minutes while the session is active;
5. a run without `ended` declares a `pid` that is no longer running, and its log, if any, shows no life.

A Stop hook blocks the end of a turn while findings exist (at most once per 5 minutes), and prompts and tool calls carry a reminder. Fix each finding with TaskUpdate: set the missing fields, record real progress, or close a run that finished, failed or hung by setting its `ended` time. Updating progress only to silence a reminder makes the chart lie; record what was observed.

## Two parallel lanes

Plan a release in an Implementation lane and a Documentation lane. The first two tasks can run in parallel; the final check joins both lanes. Use the identifiers returned by TaskCreate in subsequent calls, rather than assuming these example identifiers.

1. TaskCreate for the implementation, returned as task `1`:

   ```json
   {
     "subject": "Build the export",
     "description": "Implement the export and verify its output.",
     "activeForm": "Building the export",
     "metadata": {
       "milestone": "First release",
       "weight": 3,
       "estimate": {"low": 1, "likely": 2, "high": 4},
       "lane": "Implementation"
     }
   }
   ```

2. TaskCreate for task `2` with subject `Write the usage guide`, a concrete description, activeForm `Writing the usage guide`, and this metadata:

   ```json
   {
     "milestone": "First release",
     "weight": 1,
     "estimate": {"low": 0.5, "likely": 1, "high": 2},
     "lane": "Documentation"
   }
   ```

3. TaskCreate for task `3` with subject `Check the documented export`, a concrete description, activeForm `Checking the documented export`, and this metadata:

   ```json
   {
     "milestone": "First release",
     "weight": 1,
     "estimate": {"low": 0.25, "likely": 0.5, "high": 1},
     "lane": "Implementation"
   }
   ```

4. Call TaskUpdate with `{"taskId":"3","addBlockedBy":["1","2"]}`. Start tasks `1` and `2` with separate TaskUpdate calls setting `status` to `in_progress`. Mark each `completed` when its work finishes; start task `3` only after both prerequisites finish.

If an agent runs the implementation, set task `1`'s `agents` to its returned subagent identifier. Keep all current identifiers when replacing that list. Update progress from observed work, never elapsed time alone.

## Record and close an external run

Before starting a process that belongs to task `3`, mark the task in progress. After launch, record the actual start time, an informative label and, if available, its log with TaskUpdate:

```json
{
  "taskId": "3",
  "metadata": {
    "runs": [
      {
        "label": "Export integration checks",
        "started": "2026-10-15T10:00:00Z",
        "ended": null,
        "log": null
      }
    ]
  }
}
```

Record the process identifier as `pid` when you have it, so an exited process is noticed even if it never writes a log. When the process exits, replace that run's `ended` with the actual completion timestamp, for example `2026-10-15T10:18:00Z`, preserving its label, start and log. Preserve other runs when sending the updated list. Close failed or cancelled runs too; retain the task as in progress if corrective work remains and describe the result in the task. Mark the task completed only when all its work is done. A quiet log does not close a run.
