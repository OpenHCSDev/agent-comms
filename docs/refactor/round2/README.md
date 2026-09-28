# agent-comms refactoring, round 2

Plans for the debt that landed after round 1, and for the legacy code still on `main`. Written against `agent-comms` `main` at `15a4d00` (#225).

**Put this directory in the repository at `docs/refactor/round2/`**, so every agent's worktree has it and the dispatch messages' paths resolve.

## Read in this order

1. **[00-RULES.md](00-RULES.md): binding on every agent, overriding everything else.** No backwards compatibility, no converters, aggressive deletion, total completion, tests only where they protect behaviour.
2. **[01-INDEX.md](01-INDEX.md):** where things stand, the surfaces, their order and crossings.
3. **[02-SHARED-ABSTRACTIONS.md](02-SHARED-ABSTRACTIONS.md):** every mechanism more than one surface uses, defined once (A1 to A14, and the testing patterns).
4. **[03-COORDINATION.md](03-COORDINATION.md):** who starts when, the cutover procedure, decisions, and the addendum for surface agents' prompts.
5. **The surface files:**

| File | Surface |
|---|---|
| [L0-legacy-sweep.md](L0-legacy-sweep.md) | Delete every legacy and compatibility path; end every dual path with one path |
| [R0-R1-stop-the-inflow.md](R0-R1-stop-the-inflow.md) | The required CI check (ratchet and guards), and NRA's raw-record detectors |
| [S13-child-supervision.md](S13-child-supervision.md) | One child-process mechanism (A12), no bare-PID path |
| [S12-typed-tables.md](S12-typed-tables.md) | One row type per table with derived schemas (A13) |
| [S10-pi-boundary.md](S10-pi-boundary.md) | The small remainder of the pi boundary |
| [S9-compaction.md](S9-compaction.md) | Compaction: one settings record, helpers out of strings (A14) |

## What the owner does

Answer **D22** in the coordination file; do the cutover installs, one per completed step; read `#refactor` for status. Nothing else.
