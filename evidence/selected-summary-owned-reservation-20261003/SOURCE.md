# Selected summary uses its acquired reader and joined worker

SelectedSummarySlot reserves under wire/InputDocument/SQL resources directly on
the event loop. Its continued-source verifier opens/decodes the full native file
again, despite OwnerCompactionCommit already holding NativeEvidenceRead through
that same summary operation. Failure/refusal/UNKNOWN settlement also writes SQL
synchronously while owning the original native response reader.

Pass that existing acquired reader through SelectedSummaries/PrivateInputs into
NativeEvidenceRead.borrow. It still verifies original bytes on each observation;
no source proof, coverage flag or interpretation is cached. Independent coverage
callers acquire their own reader. Run the complete reserve/settle/UNKNOWN SQL
operations through existing Coordination.run_worker, joining them under original
summary/native locks; no open SQL connection crosses workers.

Reserve remains before any native command. Cancellation joins the reservation
worker before resource release; a persisted reservation remains blocking even
if its result did not return. No input/summary replay or false terminal success.
Exact source/input/enrollment/raw UNKNOWN/fork coverage decisions remain intact.
A borrowed reader is an optional execution resource, not nullable domain state.

Existing native SessionContext condition recipes and request budget remain
Arendt-owned/disjoint; this changes no native package/provider/options. Actual
original98-second provider duration and4.972-second gap are separate facts; this
source batch removes repeated decoding/event-loop work without attributing either.

NRA AST spans726 modules/no omissions; source-before.json and semantic readings
cover owner declarations, all reserve/coverage/selected RPC consumers. Pattern
IMPL-5 repeated implementation, original owned resource lifetime. Final batch
will check original coverage/input refusals, join-before-close cancellation and
actual native summary result/read ownership in the reused installed holder.
No new environment/worktree/framework/provider probe or public changes.
