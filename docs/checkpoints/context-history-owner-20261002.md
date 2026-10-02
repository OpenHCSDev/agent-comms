# Historical context inspection closure

Owner: Singer. Base: main `1282a4228c7fa2841fab99443478c0f9b3d28a07`.
Original scope: retained-task-memory/S5 historical manifest inspection and the
parent 15-GOAL-CHECKLIST consumer closure. PR502 is already merged and frozen.

## Source findings and existing owners

`WireLog.context_manifests` compares a recorded `manifest.thread` directly with
the current incarnation. RegistryDocument.rename preserves birth and original
aliases; ThreadIncarnation.resolved already owns that relation. A rename thus
hides original context observations from the CLI without deleting their rows.
The query must derive membership from that existing owner at one registry cut,
preserving every recorded name, turn and provenance in its returned originals.
This is IDEN-6: query membership was keyed by spelling instead of original identity.

`ContextCliCommand --turn OLD --diff` selects OLD but searches the entire history
backwards for a different turn. With later turns present it selects a future
turn. ContextManifest already owns `changed_since`; its historical comparison
must use a predecessor from the sealed wire order before the selected manifest,
not an independently supplied chronology. This is IMPL-5: comparison chronology belonged to the manifest, while the CLI
independently chose a contradictory predecessor.

Existing-owner search found ThreadIncarnation, RegistryDocument.rename,
RegistryProvenance, WireLog, ContextManifest and ContextCliCommand. No new class,
identity registry, compatibility reader, retained payload or storage is needed.
WireRecord.context_manifests remains the declaration-owned observation decoder.
Producer/native manifests and their frozen proof fields remain original.

## Claimed closure

Extend WireLog's original historical query under wire -> bus -> registry lock
order; derive identity through ThreadIncarnation.resolved. Extend the existing
ContextManifest chronological comparison and migrate the CLI plus all five
direct fixture callers in the same change. Arendt owns runtime/custody; Sch owns
compaction policy. Neither implementation is part of this scope.

Source semantics first, coherent implementation and deletion second, one batched
sanity check and actual installed historical CLI journey last. The journey uses
an owned private wire with original sealed manifests, rename, reopen and a
replacement birth. No native/provider prompt, original-input replay, public
mutation, default cutover or full S2 research claim.

## Checkpoint

Production source `130fb5fc` replaces the stale identity equality and the CLI's
independent predecessor decision across three production modules. No new class,
store, alias or optional lifecycle state was introduced. All five direct fixture
calls migrated; the existing installed journey now has a historical-only mode.

Final batched sanity: four tests passed in 2.71 seconds, including original
message/index behavior, renamed original manifests, same-turn continuation,
chronological historical diff, first-turn refusal and replacement-birth isolation.

Actual installed console journey: six `agent-comms context` subprocess queries
passed with PYTHONPATH removed. Recorded turns survive rename and alias lookup;
OLD-turn diff uses its earlier predecessor, same-turn renamed continuation is
skipped, latest diff remains correct, first-turn comparison refuses, and a reused
name with a different birth cannot inherit old manifests. A cold Comms reopen
returns the exact originals. Sealed wire bytes remain unchanged:
`d3888873e4a367013a81fd59679979e4e6e7a6e936c5f4ea96ffe87840f73f58`.
This is installed historical CLI qualification; no native dispatch, provider,
public store mutation, original input replay or default activation occurred.

Owned artifacts: `.artifacts/runtime-context-history504` is the bounded ordinary
15-package Core/ACP validation environment; `.artifacts/context-history-installed`
contains the private sealed fixture and original receipt. Source and installed
logs remain under `.artifacts/context-history-*-validation.log`. Durable small
receipts are published in `evidence/context-history-owner-20261002/`.

The parent owns integration and default cutover. Full S2 general constraints,
non-selected native retention and repeated model-recall evaluation remain outside
this historical-query closure, with their original determining owners.
