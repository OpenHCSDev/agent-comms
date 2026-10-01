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

## Coherent plural builder checkpoint (supersedes unfinished API above)

Arendt transferred the complete plural builder to Kepler, including all normal
publication and recovery callers. Native custody/session/context remains Arendt's
independent ownership; Singer495 owns the stopped-owner preservation carry.
No wait for all of #489 is needed to review this implementation.

This increment changes 21 production files: **372 added / 435 deleted** against
2e0d55bc. Against pinned baseline590c406c, the whole production branch is
44 files, 965 added / 754 deleted. These are physical line counts, not proof
that every semantic duplication has disappeared.

| Required fact/algorithm | Existing declaration owner and consumers | Replaced independent decisions deleted |
| --- | --- | --- |
| Original input membership and committed route | ExecutionStore.create(sources=), WakeAssignment.require_committed_source and ExecutionAssignmentLink | Caller-supplied assignment IDs and execution-level exact_target; first-route selection/filter in SelectedParticipant |
| Original answer obligations | Existing ResponseObligation keyed by execution plus original target | Scalar execution target and deferred single-obligation generated columns/FKs |
| Freeze all answer bodies before any append | RecoverySnapshot.require_publishing_intent and existing prepare_fenced_response | Singleton intent assumptions; SelectedAttempt freezes all original route bodies before publishing |
| Original append dispatch and receipt | Existing PublicationIntents, PublicationReceipts and PublicationAppendDispatches composite keys | Execution-only receipt/dispatch reads and updates, first-target fallback |
| Complete one native attempt | RecoverySnapshot.finish_publications, existing execution/attempt families and declared SQL finality guard | Publisher-local terminal mutation algorithm; completing claims/pointer after only one route |
| Receipt presence and original ID/sequence validity | ResponseState.validate_receipt shared default; PublishedResponse hook | Snapshot's copied ID/sequence comparison and projection's independently rederived published/presence relation |
| Frozen intent envelope | PublicationIntents.validate_receipt, called by ResponseState.validate_publication | Snapshot-owned nine-field envelope comparison |
| Recovery and projections | RecoverySnapshot plural original rows; recovery_reader; recovery_projection; existing family publication hooks | Scalar snapshot fields, SQL one-obligation join, 0/1 receipt count, scalar projected publication; duplicated unavailable-schema literal in recovery gateway |
| Tool action grant | CodingToolOwner.for_original, used by existing BatchSelectedAction.mode/apply | Selecting an action grant against the first source's owner instead of the requested original |

Read every validate_publication/validate_receipt implementation before changing
the shared hook. Unpublished ResponseState members reject a receipt through the
shared default; PublishedResponse supplies the irreducible original reference
check. Snapshot calls the complete intent/receipt validation. The redacted
projection calls the same receipt validation and translates IntegrityViolationError
to its existing unavailable diagnostic; it does not read/fabricate intent payloads.
Route membership checks remain a separate original-claim relation.

Partial publication retains the original current attempt, engaged original
claims, frozen bodies and route receipts. Only all-successful original
obligations allow completion and pointer release. Retry authorization requires
all original obligations to permit retry; no Started/UNKNOWN native retry or
replay of an already appended route is introduced.

### Source/caller searches

Whole src/tools searches return no execution.exact_target, require_response_target,
snapshot.obligation, snapshot.publication_intent, snapshot.publication_receipt or
assignment_ids= create callers. SelectedAttempt, private_send_stage, attempt
start/store/recovery, recovery reader/projection and optional awareness use the
original plural rows. Normal CLI continues to serialize original receipt tuples;
history, cursor and tool membership recover original ExecutionAssignmentLink
through existing NativeInputExecution. No new registry or source family was added
in this increment.

One declaration each remains for NativeInputExecution, NativeInputRecord,
SelectedSourceBatch, CoordinatedTurn, TriageNativeSources, ResponseObligation,
ResponseState and PublicationIntents. Receipt identity comparison is declared
only by PublishedResponse; envelope comparison only by PublicationIntents.
ResponseState's shared algorithm invokes those hooks. Existing SQL constraints
protect storage writes; they are not a second consumer-side algorithm.

The tests tree still has obsolete scalar fixtures/create calls, including
coordination_store, optional awareness and admission verifier. These remain
explicit migration work, not a production compatibility alias or a clean
whole-tree claim. Existing PublishedResponse receipt references remain stored
under their original schema contract; this checkpoint centralizes their validity
algorithm rather than silently dropping preserved data. Singer's carry must
preserve all original IDs, outcomes, context, session and proof references.

### Sanity evidence and release boundary

Source imports initially exposed an execution_store -> selected_source_batch ->
native/tool -> coordinator cycle; SelectedSource is now a type-only import.
AST/import sanity and fresh declared SQLite schemas then passed. Existing real
private bus + SQLite single-route Tx1/Tx2/lost-ack controls passed (2 cases, 0.87s).
The new mixed-route source control passed (0.70s): missing second intent refuses
the first append; one route receipt leaves the attempt active; final receipt
completes all originals and releases the single pointer. These controls use the
real bus and original accepted deliveries, not protocol/UI mocks.

The last shared-receipt control run's output was not retained across compaction;
it is not counted as an additional pass. No installed native/ACP/tool multi-route
acceptance is claimed. Final consumer migration, preserved carry, coherent staged
pair and actual configured continuous journey remain required before Ready.
No public mutation, original replay or new provider call occurred.

### Published checkpoint and final shared-receipt sanity

Production checkpoint: 369b10be7c5c7a35124556686a1ce80b2bf56ccb. After the
receipt-owner closure, one retained final run passed 3 controls (12 deselected)
in 1.54s. Exact output: `.artifacts/plural-builder-369b10be/receipt-owner-controls.log`.
This final run is counted; the earlier lost output remains unclaimed above.

Frozen source-derived DDL/digest receipt for Singer495:
`docs/checkpoints/490-plural-source-ddl-369b10be.json`. Full generated definitions
are preserved in `.artifacts/plural-builder-369b10be/{coordinator.sql,response.json,native.json}`.
This is declaration-source evidence, not migration execution or installed proof.

Remaining private-bus admission-verifier and optional-awareness producers now
pass original SelectedSource witnesses to create(sources=), rather than supplying
copied assignment IDs/targets. The existing actual-native coding-provider
fixture now emits the single canonical Message-array grammar through FieldCodec;
it was not executed and is not a native acceptance claim. These are 3 test files,
14 added / 12 deleted; production source/DDL is unchanged from369b10be.
