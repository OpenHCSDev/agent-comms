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

## Whole source checkpoint97fc173d

Original after Package:316 production/365 tests, zero omissions. All former
run_owned_input/pending_followups consumers removed;37 declaration/call sites
recorded, including the embedded original child source. InputDrain486 lines
(previous547); this comes from shared reservation ownership and original-only
lifecycle semantics, not methods moved into a size mixin. Current production
95 deleted/114 added across four existing modules. NativeBackendFixture
uses the same original reserve owner;13 related fixture consumers migrate.

One focused control was added AFTER implementation for the concrete scope-exit
cancellation gap: original input transferred but dispatch not entered. It must
retain its one NotSent row, no native binding and no live queued grant. Existing
owner readmission/postdispatch failure controls stay intact. None of these
checks has run for656 yet; final bounded installed batch remains required.

No full C4 live/latency claim from AST or source size. Parent's current
HistoryViews/presentation/diagnostics scopes remain disjoint.

## Normal main654 union and last strict caller

Normally joined actual merged mainb1a91fb5 (654) at218c48f0. All four reviewed
656 production files remain byteexactc98; activity/history_views/presentation
match mergedmain exactly. The original scheduled reservation acknowledgement
loss control expected the retired per-caller error text; migrate ONLY its
phrase to the shared strict owner. Exception type, original acknowledgement
loss/NotSent/unchanged-byte assertions remain intact. No tests run as design.

Final proposed batch: original publication-ack loss/scheduled duplicate;
three original captured-dispatch refusal/failure/transfer-cancel cases; exact
foreign origin refusal; one savedSDK source/OwnedTurn acquisition-release
control via migrated NativeBackendFixture; one real native steer (initial +
following reservation, samelease, terminal/socket/childcleanup). These prevent
concrete changed-family failures. No unchanged655 queue projection matrix or
new provider reproduction. Sch656086 grant remains conditional on fresh
Bohr656540 issued purpose; no previous holder/SDK grant reused.

## Installed batch01

The source-equal normal wheel `5f1667ce64c58d67e153850be91dfb669ea9e347218f8d845937a1c12c778a9c` contains all347 Git/local/ZIP-equal assets, including the three declared forced resources. Core-only install and ten-package compatibility check passed. The actual installed seven-case batch returned **6PASS /1FAIL in14.192s**.

The failed readmission control correctly refused the changed owner before dispatch and persisted the original as `NotSentInput`. Its obsolete assertion still expected `accepts_reservation` after original terminal cleanup. `InitialInput.finish` invokes the original `InputDrain.finish_original_inputs`/`InputAttempt.finish_unbound` behavior; no native binding means `ReservedInput` retires to `NotSentInput`. The corrected consumer now asserts that exact original leaf/text and no queued grant/turn task. Production and wheel are unchanged. The failed log/XML remain intact; only this changed assertion needs a successor execution purpose, with no repeated six passing cases or SDK/provider run.

Actual savedSDK source/OwnedTurn acquisition and foreign/future-source retention passed with zero posts/proofs. Actual RuntimeProxy steer passed with two localhost posts/three proof rows. Controller2949111/start61534760 and native2951126/start61535716,2952896/start61536100 are absent; all recorded groups and owned sockets empty. All347 installed assets and92 original nonCore/bin/env keepers remain exact. No external/paid/public/replay. `EXECUTION-HANDBACK01.json` returns native086 READ/execution and requests Bohr's independent whole holder closure. NotReady until the single corrected consumer is qualified.

## Final scoped Ready

The execution-only successor ran **only the corrected readmission consumer:1PASS0.878s** on the unchanged installed wheel. No package write/reinstall, native child/input/provider/public/replay occurred. The original01 six passes remain qualified and its failed raw log/XML remain unchanged; they are not retroactively rewritten as a seven-pass run. The cumulative seven changed cases are now qualified at their individual installed scope.

`EXECUTION-HANDBACK02.json` attests controller3089287/start61581507 absent/groups[]/sockets[], all347 installed/Git assets equal,92 keepers unchanged, ten distributions and actual directURL unchanged. WholeTHIN540 package/build/import/read/execution claim is returned to Bohr; native086 execution was already returned after01 and this successor did not renew it. Required original authorizations remain immutable. Production/stack/pins/pyproject are unchanged from installed c656 through the final evidence checkpoint.

This closes the original reservation/initial dispatch resource family: rollback belongs to InputDispositions before publication; QueuedInput owns strict reservation/capacity; InitialInput owns dispatch-through-terminal cleanup and live binding transfer; TurnRunner and all fourteen original direct/embedded fixture consumers derive those owners. No mixin/class carving, compatibility method, new lifecycle state, copied delivery engine or cleanup store. InputDrain486 is a source consequence, not the behavioral proof. This acceptance does not claim UI/public/provider-speed/full-channel performance or a gain against the historical13/98s gaps. The final required Debt ratchet must pass at the exact publication head before merge.
