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
This is IDEN-5: spelling cannot replace original identity.

`ContextCliCommand --turn OLD --diff` selects OLD but searches the entire history
backwards for a different turn. With later turns present it selects a future
turn. ContextManifest already owns `changed_since`; its historical comparison
must use a predecessor from the sealed wire order before the selected manifest,
not an independently supplied chronology. This is TIME-9/BOUND-2: the original
record sequence owns ordering.

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

Draft scope publication; implementation and end validation pending.
