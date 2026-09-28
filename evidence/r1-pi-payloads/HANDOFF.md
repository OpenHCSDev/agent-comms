# R1 known Pi payload closure — active implementation, not handoff-ready

Tree `/home/ts/wt/comms-refactor-r1-pi-payloads-20260928`, branch
`codex/refactor-r1-pi-payloads-20260928`, base main189 `5090a20`.
Parent owns R4 integration and activation; Darwin owns R2. No live/provider work.

Implemented in working tree: PiPayload field-derived external projection into
strict FieldCodec, typed message/content/delta/usage/state/stats/result records;
PiEvent Mapping/wire mirror removed; command-owned response payload decoding;
stream/discovery/watchdog/inputs/native/tool-consumer migration; nominal delta
behavior and typed ProviderUsage; strict selected-summary response records.

Pending before publication: complete selected prepare/settings/summary command
and response caller closure, remove remaining known command Any, migrate current
tests/usage boundaries, resolve focused stream failures, test native admission,
cancel/tools/summary/receipt seams; inspect complete combined diff and rescan.
No owner_compaction_commit, input_drain or owned_turn edits. A narrow
transcripts.py tool-result decoding call migrated because ToolDiff now accepts a
typed PiToolResult; no history/index redesign. R1 stores remain original owners.

NRA baseline full context79detectors, complete,0findings: exact command/results
retained. Semantic typed-boundary edits are authored patches, not a claimed
NRA equivalence proof. `.artifacts/r1` contains disposable transformation scripts.

Local progress: nominal46pass. Initial mixed stream run60s timed out with stale
constructor failures; subsequent backend first-failure run timed out on malformed
list because overbroad decode catch swallowed it. Fixed catch to preserve original
pi_invalid_rpc_event/reaping. Backend repair then32pass and one existing invalid
stats assertion mismatch; decide malformed telemetry semantics at boundary and
finish current case before wider tests. Logs retained. No broad suite is green.


Checkpoint update:46 nominal and58 selected-boundary/FieldCodec cases pass
(2 optional native tests skipped). Native/manual combined prior batch92pass before
selected diagnostic mismatch, repaired in selected-boundary batch. Backend has
reached93+ cases; broad60s partial batches retained, investigating remaining
settlement/caller effects. Main193 (includesR4/190 andR2/192) is being reconciled.
Shared FieldCodec change only preserves the declaration's error message through
optional/union decode; accepted/rejected values unchanged. No parallel codec.
