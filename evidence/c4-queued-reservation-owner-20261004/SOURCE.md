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
