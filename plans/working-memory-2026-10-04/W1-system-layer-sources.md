# W1: The system layer records its source files

**Index:** [README.md](README.md). **First.** **Pattern:** BOUND-8.

## What is wrong

`SystemLayerSegment` holds pi's entire system prompt as one string and renders it straight into `provider["systemPrompt"]`. Its `contributors` stay empty: the only `contributors=` assignment attaches agent-comms' own segments to the native data, never the files inside the system prompt. A typed context family is flattened at its boundary, and the model, the explorer and Tristan are left to guess which file said what.

## Target

- **Contributors by exact span.** agent-comms knows which files pi loads: pi's base prompt for the running pi version, `.pi/APPEND_SYSTEM.md`, the `AGENTS.md` chain, and the loaded skills. For each, locate its content in the captured system prompt as an exact span and record a contributor: `FileProvenance` with the path, the file's revision, its hash, and the span. Pi's base prompt is recorded the same way, against the pi version.
- **Unattributed text is visible.** Any part of the system prompt no source accounts for becomes an unknown-origin contributor, shown as such.
- **The same split for Codex threads,** applied to the developer message in the rollout files the importer already reads.

## Done when

`agent-comms context <thread>` lists the system layer's sources with paths, revisions and spans, and unattributed text appears as its own entry.
