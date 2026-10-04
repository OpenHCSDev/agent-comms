# Queued input reservation ownership

Base merged655 main7cca965a. Existing QueuedInput owns ingress identity,
reservation and handoff; InitialInput owns original-only clear/retirement
semantics. InputDrain duplicates the wire-lock/ExitStack/AsyncExitStack
reservation lifetime in initial and following producers. The original
NativeBackendFixture has a third copy for actual OwnedTurn acquisition.

QueuedInput.capture currently calls InputDispositions.record, which reduces
the exact published InputDocument to bool; follow-up acceptance then rereads
the whole document under its original wire lock. This is one receipt with
its original storage owner, not a second observation requirement. Existing
record_originals already returns that exact document and enlists rollback
BEFORE publication.

Extend existing strict reservation behavior at InputDispositions across
reserve_turn and QueuedInput; capture returns its exact original row. Extend
QueuedInput with the shared async reservation context, retaining wire custody
and worker-joined rollback through caller handoff. InitialInput owns initial
publication/dispatch/settlement; migrate every old run_owned_input caller,
including original saved-SDK fixture. No wrapper left on InputDrain.

Following input keeps its existing prompt/provenance/control policy, distinct
pending queue vs retained source, one-use admission and exact durable UNKNOWN.
No native/provider/package loan; source first, final changed installed
reservation/cancel/actual SDK path only after a fresh explicit holder purpose.
No new type/store/queue/codec; no mixin carving or size-only closure claim.
Parent HistoryViews/CoordinationSnapshot/notifications/activity unaffected.

Original Package before.json parses full production/tests; omissions and
dynamic ambiguity explicitly recorded. Pattern IMPL-12 shared lifecycle,
original OpenHCS44/45 precedent.

## Implemented complete family

InputDispositions.reserve_originals extends its existing exact-document
publication with strict original refusal; reserve_turn and QueuedInput.capture
share that owner. Conditional record remains its distinct idempotent/bool
contract for existing callers. QueuedInput.capture returns the original
published receipt, not a reread or new stored state.

QueuedInput.reserve owns the one wire/worker rollback scope. Following input
capacity is checked inside that scope BEFORE reservation and uses the original
32-awaiting-start policy; InitialInput's existing original/following distinction
supplies the hook because an initial input is not a following queue member.
InputDrain no longer duplicates capacity computation or reservation lifetime.

InitialInput.run owns acceptance through dispatch and retirement. Its actual
cleanup is enlisted before reservation transfer and before the async scope
exits, closing the previous cancellation gap. InitialInput.finish derives
retirement through existing InputDrain.finish_original_inputs, the SAME joined
wire/write owner consumed by finish_turn_inputs. Loop grant retirement is in
finally after that joined write, including cancellation; it does not clear
UNKNOWN, native bindings, goals or receipts.

All run_owned_input consumers migrate, including the embedded hard-exit
private child in test_acp_input_disposition (ordinary AST cannot resolve code
inside a string; source inspected and parsed explicitly). No forwarding method
is retained on InputDrain. Synchronous capture consumers take the third original
receipt result. NativeBackendFixture.original_input consumes reserve and transfers
the original ExitStack into its original turn resources, then RELEASES wire
custody BEFORE native acquisition. No new fixture framework or lease/witness.

Changed-source AST compilation and whitespace check passed. No test, package,
SDK/provider/public operation run for656. Existing540 and086 purposes remain
closed. Final bounded installed reservation/refusal/cancellation + actual saved
SDK/OwnedTurn cleanup awaits fresh purpose; prior655 evidence cannot qualify
this expanded source.
