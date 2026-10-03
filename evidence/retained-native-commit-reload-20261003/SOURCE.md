# Retained native source after an external compaction commit

Mendel owns the PersistentPiSession/native custody, query/response and selected
summary commit consumers. Sch owns receiving; no new native build is needed for
the existing SDK `switch_session` contract. Original input/UNKNOWN/source stays
protected. This reuses the current isolated checkout and installed holder.

Current source discards the idle SDK child before the separate FD-authorized
writer. That prevents its old in-memory SessionManager from serving the changed
file, but requires a new process, package/header acquisition and runtime startup
for the following input. SDK AgentSessionRuntime.switchSession already opens
the selected file, disposes the old runtime and rebinds the new runtime in the
same process. Use that capability rather than treating disk mutation as an
unconditional process-death requirement.

PersistentPiSession owns the borrow lock and external-write resource lifetime.
ReopenNative denies the old source while the resource owns the child. Keep the
writer/journal/global lock/inherited descriptor algorithm unchanged. A known
committed operation derives its exact new witness through CompactionSource.
The same idle child performs the SDK switch and original GetState attestation;
NativeWitness checks the exact committed file revision before/after reload.
Only this successful observation permits RetainedNative. Refusal, UNKNOWN,
cancellation, changed source or failed reload retires the exact old child and
preserves strict reopen and the original operation evidence. No retry/fallback.

Extend the original SwitchSession declaration with its external sessionPath and
cancelled response. Compose existing NativeQuery for its transaction and
GetState; no new reader or response registry. Delete discard_for_external_write
and its sole commit call. Keep other forced-reopen callers, which own genuinely
lost/changed identities rather than a known commit.

The original13.562/13.696 second next-preparation gaps and98.141 second provider
span remain separate measured facts. This source batch removes compulsory
process retirement/new startup, not provider reasoning. Final checks follow the
complete source migration: query/cancel/refusal resource correctness and one
affected real configured saved-source compaction -> distinct input -> settlement.
