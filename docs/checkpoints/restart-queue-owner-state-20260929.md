# C2 restart queue and C3 owner state

Owner: Mendel. Baseline: Core 2c1ca485. Source assignment:
`plans/cleanup-2026-09-29.zip` README, C0, C2 and C3.
PR414 merged at 207c17e8, tests only. C2 checkpoint follows independently.

## C2 acceptance

Replace the entire restart queue's refusal sentence dispatch with refusal
behavior owned by lifecycle types. Extend existing Identity, activity and command
owners. Decode queued records once into their declared types and derive inherited
environment policy from existing runtime declarations. Delete the eight-key
environment roster, raw queue record consumers and retired generation terminology.
Verify actual queued restart on an isolated private fixture, including continuous
native attachment and new loopback input after replacement. Preserve uncertain
input dispositions; never restart a live owner or mutate the live root.

Patterns: IMPL-1, MEMB-3, BOUND-1, BOUND-2, TIME-1.
The checkpoint adds more than it deletes because queue/refusal/environment facts
now have declarations and the fixture exercises a real continuous owner handoff.

Implemented: one `QueuedRestart` declaration and state family decoded by the
existing FieldCodec; typed `OwnerRestartSelection` delegates to the registry's
existing owner/process identity check and separately fences admission generation.
Lifecycle/registry guard exceptions own pending/stale queue disposition.
`RestartEnvironment` declares watcher inheritance once, reusing private launch
variable declarations. `ParentedProcess` owns watcher launch. The original
owner environment is passed into the existing lifecycle launch, without mutating
the current process environment. Tool output is encoded at its boundary.

Actual acceptance: `queue-acceptance.log`, five passed in 42.89s, serial. Existing
native backend/localhost fixture seeds a real SDK session. Installed Python and
candidate source launch separate ACP and owner processes, hold an actual provider
exchange busy, queue through the real inotify watcher, retire the exact owner,
reattach and send a uniquely new native input. Three native input IDs/provider
posts are distinct and history is preserved. Additional controls prove cancel
does not signal, a changed owner makes its queued selection stale, and an injected
launch fault after real retirement leaves uncertainty without automatic replay.
Existing real restart metadata/history test also passes. Exact owner and watcher
identity cleanup is asserted. This does not activate or modify the global runtime.

Persistent evidence owner: Mendel, directory
`/home/ts/.cache/agent-scratch/comms-restart-queue-cleanup-20260929`.
Preserve the failed first-run assertion evidence, native sessions and uncertain
queue receipts. The first-run defect was a test marker substring collision;
the native history already held all three distinct inputs.

Per-function/per-file ratchet receipt: `c2-ratchet.json`. Queue StringDispatch
subjects/arms 2/13 -> 0/0; TypeSwitch 0/0 -> 0/0. Other touched production files
remain zero for all four measures. Permanent site guards protect typed records,
declaration-owned environment names, refusal behavior and process environment.

The queue is runtime attempt state with one current declared format. No reader
for the deleted raw format is added, and no live queue/history is rewritten.

## C3 pending acceptance, owned here in full

After the PR414 checkpoint and C2 integration, close foreign state reconstruction
in coordination_response.py, owner_lifecycle.py, goal_actions.py, backend.py,
turn_watchdog.py and goal_management.py through existing behavior owners.
Close all five assigned families: goals.py resolution, maintenance_barrier.py
phase lifecycle, relationships.py edits, thread_status.py command control
capabilities and acp_failure.py external payload decoding/rendering.

Coordinate backend.py, goal_actions.py and thread_status.py directly with the
parent's canonical OwnedTurn backend state and Toad permission/status/cancel
contract. No private active_turn probes, reserved-flag projections or symptom
branches. Re-measure actual post-S14 source instead of using the old 41-chain
snapshot. Record per-function and per-file StringDispatch/TypeSwitch subjects
and arms; classify external taxonomies at their decode boundaries.

Patterns: IDEN-3, IMPL-1, IMPL-3, IMPL-5, MEMB-2, BOUND-1, BOUND-2.

## Shared ownership and exclusions

Mendel owns C2 owner_lifecycle refusal types and the complete C3 scope above.
Parent owns C0 sealing, FieldCodec builders, typed_table A13 review and the
canonical Core/Toad turn integration. Einstein owns C1 Pi vocabulary, including
threads.py and pi_events.py; these files are excluded here. Kepler owns TC2 ACP
SDK. Arendt owns Core416 large-context native accounting and usage acceptance.
C4 remains parent-owned after C1 and this scope. No new workers.

All C3 acceptance transfers intact to Core421, the separate owner-state follow-up
draft at `/home/ts/wt/comms-owner-state-cleanup-20260929`.
A C2 checkpoint is not completion of pending C3 acceptance. Einstein additionally
reported the retired `SelectedSummaryAttempt` import in the independent
`test_selected_summary_exchange` child fixture; Mendel owns a separate tests-only
follow-up preserving actual retained-native framing/progress/UNKNOWN negatives.
