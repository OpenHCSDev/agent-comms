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

## Source closure and actual external replacement effects

Patterns IMPL-13/BOUND-2: the existing persistent borrow/resource owner and
NativeQuery transaction replace caller-owned child retirement and bare RPC
handling. NativeWitness/CompactionSource still derive the committed source;
there is no local source-validity flag, revision cache, second input receipt or
changed native writer grant. External SessionSwitchData owns the actual SDK
`cancelled` result (required bool), not domain availability. Success requires
its original correlated response, GetState capability/identity and exact file
revision. ReopenNative remains unavailable throughout external mutation.

The Python AST covers 726 production/test/tool modules with no parse omissions;
there are zero remaining `discard_for_external_write` declarations/calls.
The unchanged native SDK Acorn AST covers all268 dist JS files with no omissions.
The first parser-loading failure is preserved separately and makes no closure
claim. Syntactic attributes cannot prove runtime dispatch; the actual MRO,
request correlation, writer Future lifetime and SDK replacement were read.

The SDK owns meaningful replacement effects: teardown aborts an active response,
emits session_shutdown, disposes the old runtime, opens the saved SessionManager,
creates the replacement and invokes its existing host rebind hook. RPC also
rebinds after new_session/switch_session/fork/clone. AgentSession.bindExtensions
emits session_start and resources_discover, so that duplicate call is real
repeated work with possible extension writes. Mendel owns removal of that
second orchestration path through the existing runtime hook in the normal native
source recipe; Sch owns a later immutable build. This qualified standard-contract
batch does not edit51b or the frozen385 package and does not bypass writes by
extensions: exact committed file checks refuse/retire if either bind changes it.

RetainedNative.reload runs under the same persistent borrow lock, after the
joined external worker released global custody. Its session_writer_fence uses
the normal borrow->writer order. Failed/refused/UNKNOWN commit never reaches
reload; cancellation still joins the exact concurrent Future before child
retirement. Successful reload does not admit input by itself: the original
summary-result admission and later input fences still run. A reload without
source currentness cannot restore availability. Other force-reopen callers keep
strict retirement for actual source/identity/auth changes.
