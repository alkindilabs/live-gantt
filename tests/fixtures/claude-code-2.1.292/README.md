# Claude Code 2.1.292 compatibility fixture

This is one invented session with three tasks in two lanes, a completed assigned agent and a running unassigned agent. Its task files, parent transcript, subagent metadata and subagent transcripts preserve the field shapes observed in Claude Code 2.1.292. Every identifier, timestamp, path, prompt, description and result is invented; no personal session content is included.

The compatibility test reads these files through `SourceStore` and then projects the normalized tasks. It checks session discovery, creation and status history, progress, agent association and terminal notifications, and a dependency forecast across lanes. The task creation result text is part of the compatibility contract, as well as the structured fields.

For each new Claude Code release, inspect the same file and record shapes, update this fixture with invented values and run the compatibility test. Keep the full transcript envelopes so format changes remain visible even when the parser currently ignores a field. Keep timestamps fixed so running-agent and forecast assertions stay deterministic.
