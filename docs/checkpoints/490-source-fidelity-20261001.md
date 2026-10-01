# #490 source-fidelity census

## Authority and order

Read the checksummed Zenodo record 22782549, publication 2026-09-29:
main sections 6.2/6.5 and Corollary 6.3; supplementary sections 4.6/4.8.
The local architecture plan's Source fidelity / Operational Decision Procedure
and the installed nra-refactoring skill supply the repository application method;
the newer Zenodo publication owns the paper claims.

Batching reduces operations, not the number of required semantic answers.
Outside routes remain independently supplied even when generated together.
Under the stated positive post-batching charge q and feasible adoption cost
Ka + ra*n, ra < q gives crossover floor(Ka/(q-ra))+1. No measured q or adoption
cost is asserted here. Neither local agreement nor class count proves closure.

Order: existing-owner/caller analysis, coherent implementation and deletion,
then bounded validation; configured installed workflow acceptance remains last.

## Actual deletions in this increment

Four production files, 12 added / 34 deleted relative to abb7fea8:

| Independent implementation/decision removed | Existing owner now supplies it | Consumer closure |
| --- | --- | --- |
| Two separately writable route-count choices of plaintext versus structured response | SelectedSourceBatch uses one Message-array grammar for every cardinality, decoded by FieldCodec | SelectedPrompt.full and SelectedAttempt.run; no plaintext fallback or new format class |
| Two redundant copies of the terminal-result capture algorithm (three bodies become one) | CoordinatedTurn.capture | IgnoreSelectedTriage and RejectedTriageOutcome supply their dispositions; full attempt supplies CompletedAssignment and original receipts |

Disposition cases remain distinct requirements, supplied by the existing
triage members and full attempt. The reduction is two repeated algorithms,
not deletion of the failed/ignored/published cases. Likewise route membership
checks remain necessary; deleting the grammar branches does not prove routing.

The existing NativeInputExecution.from_columns owns SQL-to-execution meaning.
NativeRuntimeInput.execution and PromptBinding.execution forward to that owner
with different error diagnostics. An attempted ancestor-property promotion
would shadow HistoricalNativeInput's already decoded execution field. Arendt
reviewed that third consumer and withdrew the promotion: no new capability,
cached property, column copy, historical wrapper or error fallback was added.
These explicit representation routes are retained, not counted as deleted.

## Required relation and recovery of the selected original

| Question | Declaration/source authority | Every identified consumer/case | Recovery / remaining obligation |
| --- | --- | --- | --- |
| Which originals belong to this native input? | NativeInputExecution.source_membership_sql hooks and shared source_assignment_ids | TriageNativeExecution and FullNativeExecution; PrivateNativeSend reservation/require_sources; PromptBinding acquisition; historical reader; CodingToolOwner.for_original; selected tool broker | FULL joins original ExecutionAssignmentLink. TRIAGE uses immutable pre-execution TriageNativeSources because no execution exists yet. No competing FULL source table. |
| Which original was actually accepted? | WakeAssignment plus original sealed ClaimBatchReceipt and CommittedDelivery | SelectedParticipant.source; historical reader; SourceCoverage._selected_proven | Join original message reference, root, recipient and source sequence; then journal context and expected-prompt equality. Matching independently built identities alone does not prove acceptance. |
| Which context was executed? | Original NativeRuntimeInput and prelaunch PromptBinding plus recorded NativeContextProof | historical evidence, coverage, CursorOwner._matches_input / require_input / admits, NativeSourceCursor | Immutable input/proof/binding corroboration remains mandatory. Arendt owns unresolved public-saved versus private-path custody/context guards; this checkpoint changes none of those predicates. |
| Which original may a tool handle? | Original native execution membership and CodingToolOwner | for_original and selected_tool_broker | Each requested WakeAssignment must belong to the original native input, not a fresh singleton execution. |
| Which route and audience may receive each answer? | Original committed message via derive_exact_reply_target; ResponseObligation | SelectedSourceBatch.targets, ResponseConversation.capture, selected frame, prepare/publish, failure notices | ResponseConversation checks original source references and frozen recipients for that obligation. Plural lifecycle APIs/storage remain Arendt's unfinished contract. |
| Which response was actually published? | Original PublicationReceipt | CoordinatedTurn, both foreground CLI serializers, NativeRuntimeInput.published_replies, TranscriptReceipt reader | Receipts replace copied result message ID / exact target. All committed obligations must settle before suppression/terminal recovery; plural owner integration is still required. |
| Which budget may reject input? | Native selected-model ContextBudget; separate OptionalAwarenessProjection resource | SelectedPrompt.full, native model admission, optional projection render | Fake Python 32KiB model budget / remaining-capacity calculation deleted; optional 16KiB attachment bound remains its separate existing resource. |
| Can this original be replayed? | Original admission/outcome, live owner generation and committed native reference | PrivateNativeSend, historical proof, SourceCoverage, CursorOwner, recovery | Historical proof does not grant a new send. Started/UNKNOWN and the six settled IGNORE originals remain untouched. |

Source recovery is the chain input -> execution-selected membership -> original
WakeAssignment -> sealed committed delivery -> recorded journal proof and bound
request. The expected prompt contains the whole captured batch. One native
input may therefore prove multiple originals without inventing per-original
native inputs, cursors or another live source authority.

## Previous deletions retained; not inflated into this increment

Removed copied singleton assignment/source/message fields from native input,
cursor/reference/context/binding projections; removed binding source rule;
removed copied response target/message fields from selected result; removed the
fake prompt budget. The transient competing SelectedNativeSources FULL table
and our superseded 193-line carry installer were removed from this branch's
development history: they are not claimed as baseline-main deletions.
Singer495 remains the only preservation-carry owner.

Pinned production baseline 590c406ce08a2586f592462334f1c93463dcf749:
29 files, 613 added / 339 deleted after this increment. Line totals are physical
observations, not independent-decision counts. The earlier receipt's floating
"current main" totals and one-route plaintext statement are superseded here.

## Search and explicit open edges

Whole src/tests/tools search finds no SelectedNativeSources, selected-result
response_message_id readers, obsolete CoordinatedTurn failed/ignored/published
calls, fake _MAX_PROMPT_BYTES or the two len(self.targets)==1 decisions.
One declaration each of NativeInputExecution, NativeInputRecord,
SelectedSourceBatch, CoordinatedTurn, Message and TriageNativeSources.
The two irreducible source_membership_sql implementations are family hooks,
not independent copies of their shared join algorithm.

SelectedParticipant still captures only the first original route; SelectedAttempt
still calls scalar ExecutionStore.create / require_wire_response and unpacks one
reply/receipt. These are NOT complete pending-all-routes closure. Arendt must
supply original sources= creation, plural obligation/intent/receipt methods,
per-target publication and complete-terminal recovery. Then this owner removes
the subset filter and migrates ALL corresponding runner consumers together.
Singer preserves original schema/cursor/outcome/proof identities at cutover.

No Ready/installed/whole-workflow claim. No public mutation, owner restart,
unknown-input replay, new provider call or competing lifecycle/compaction edit.
Sch owns 499/compaction; parent owns 284/cutover. Their source-fidelity receipt
must cover their own complete required relation, not inherit this scoped census.
