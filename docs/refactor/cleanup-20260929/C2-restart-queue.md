# C2: The restart queue

**Repository:** agent-comms at `1f5b1f3a`. **Index:** [README.md](README.md). **Patterns:** IMPL-1, MEMB-3, BOUND-1, BOUND-2, TIME-1.

## What is wrong

`restart_queue.py` arrived today in **#400** (`feat/queued-owner-restart`), the day's largest single source of debt in agent-comms: +19 string-keyed reads, +9 string comparisons, +8 foreign probes and two string dispatches.

- **Failures compared by message text:** `step` decides on `reason` by comparing it with human-readable sentences (`"Idle owner changed before restart fence."`, `"Owner selection changed …"`). Rewording a message silently changes behaviour (IMPL-1).
- **Retired vocabulary returns:** one of those sentences is `"Owner epochs changed before restart."`; epochs were replaced by generations in round 1 (TIME-1).
- **An environment roster as literals:** `enqueue` compares keys against eight variable names (`AGENT_COMMS_ROOT`, `AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE`, `HOME`, `PATH`, …) to decide what a restarted owner inherits (MEMB-3).
- **Raw records:** `step` reads `record["incarnation"]`, `["interpreter"]`, `["name"]`, `["state"]` from an unmodeled dict; `enqueue` reads `previous["incarnation"]`, `["name"]`, `["state"]`, fields that `cli_commands.py::ActivityCliCommand` already declares (BOUND-2).

## Target

- **`RestartRefusal`, a family** whose members own their message and meaning (`OwnerChangedBeforeFence`, `OwnerGenerationChanged`, `OwnerSelectionChanged`); `step` decides by member, and the text is only rendering.
- **Generation vocabulary** in every message and name.
- **`RestartEnvironment`, one declaration** of what a restarted owner inherits, as Toad's T8 did for the terminal.
- **`QueuedRestart`, a typed record** decoded once, reusing the thread incarnation and activity types that exist.

## Guards

No comparison against a refusal message; no environment variable name outside `RestartEnvironment`; no `record["…"]` reads in the module.

## Done when

The module's two string dispatches, its raw reads and its epoch wording are gone.

## Dispatch

> **`ac-c2`:** Complete C2 per `docs/refactor/cleanup/C2-restart-queue.md`. The module is one day old and has one owner, so take it whole.
