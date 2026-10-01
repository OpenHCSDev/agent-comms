# Snapshot pending messages into one native handling and response workflow

Integration owner Kepler. Persistent WT:
`/home/ts/wt/comms-pending-message-batch-execution-20261001`.
Base Core1304d0657975020c148a83f40d3b1550d6e70355. Parent owns activation.
Latest refactor-audit archive100fbe8 read; declaration-owned inheritance and
polymorphism, one source/proof/receipt mechanism, no competing semantic stores.

## Explicit user priority and required journey

At agent work start, capture all currently pending eligible original messages
from the canonical collector/assignment owner. One batch is handled and answered
in one native input/provider workflow. This is not an ACK-only batching change
or a loop that repeatedly prepares and sends single-message native prompts.
Arrivals after snapshot remain for the next batch. Each original author, target,
audience, source proof, notification and terminal handling stays identifiable.
Never automatically replay Started/UNKNOWN. Cancellation and refusal preserve
original dispositions, not reconstructed queued claims.

## Whole ownership scope to close

- Canonical pending assignment collection and frozen delivery/source capture.
- SelectedParticipant and SelectedExecution turn/lease/native input ownership.
- Original input document, source binding and per-original native proof.
- Consideration/handling result families and original per-message receipts.
- Batch response publication and each original dependency/notification relation.
- ACP/worker callers and their original readiness/lifetime/cancellation boundaries.

The current SelectedExecution declaration says one sealed assignment. Change
that owned relation coherently; do not bolt a serial per-message loop around it.
Existing ChannelInputBatch proof and original collector/assignment types must
be reused/extended where applicable (BOUND-2, IMPL-13, IDEN-5).

## Required implementation order

Semantic owner and whole-caller analysis first. Extend existing nominal owners,
delete replaced authority and consumers in the same change, then batch source
sanity and the continuous installed journey at the very end. No TDD, test-first
reasoning or discriminator-first design. Historical results below are retained;
they do not establish readiness of changed source.

## Parallel seams

Arendt owns the runtime lifecycle fact map and integration; Schrodinger alone
owns compaction. Payload budget belongs to the existing retained-context payload
type across reservation, intent and recovery. This PR adds no parallel budget,
context, lifecycle, session, custody or cursor family.
Kepler owns batch collection/selection/source/handling/response integration;
request shared native/source methods before overlap. Singer owns read-only
batch/amplification timing. Agenttools currently absent: parent relay or existing
PR source claims are required; no peer acknowledgment assumed.

Regression3 source8229c8b3 and exact12/12 installed discriminator transferred
back to Heisenberg275; its remaining installed viewport gate belongs to him.
Existing named progress/proofs are preserved, not abandoned.

## Acceptance before Ready

One continuous actual installed native/ACP/bus journey: busy-channel pending
wave, work-start snapshot, one native batched handling/answer, per-original
correct receipts/notifications/audiences, late arrivals next batch, cancel and
UNKNOWN nonreplay. Existing private roots/localhost provider boundary only;
real Core/native/ACP/application path, no UI/protocol substitutes. Keep resource
budget bounded and publish working checkpoints before optional broad tests.

## Historical first prototype checkpoint

Canonical pending collection crosses the existing 100-row page boundary in one
read snapshot. The selected participant captures original receipts together;
one triage/native full reservation carries all same-route original assignments.
The existing execution membership owns handling/publication; immutable
`SelectedNativeSources` owns which original claims were in that native input.
It contains no disposition/queue/turn copies. Reservation defers all triage
members atomically; Started/UNKNOWN sources cannot become pending again.
Common owner context and message bodies render once, not per-message duplicated
context. One relevance decision considers the batch, and full work produces
one useful combined answer with original frozen response audiences.
Historical source proof projects the one original native context/reference onto
each included source, without inventing per-source inputs/cursor identities.
Native tools and operator plans retain their original source grant.

Actual source sanity: real Core bus/coordinator, 101 originals crossing the SQL
page boundary, one original reservation/binding, late arrival outside the
snapshot, all unproven reserved originals excluded, second reservation refused.
PASS. This does not establish installed native/ACP response readiness.

Critical remaining closure: actual native busy-wave gate and mixed-route answer
publication. Existing executions/obligations/publication keys permit one exact
target; the current useful channel checkpoint groups all captured originals
matching the first route and leaves other routes pending. It is not yet the
user's whole all-pending mixed-target acceptance. Do not publish the same body
across channel/DM audiences or loop singleton native calls to hide this gap.
Existing native inputs require explicit operator source-membership attestation
before activating this new relation. No old-record fallback reader or automatic
rewrite is provided; original native rows/journals remain unchanged.

## Canonical source relation correction (current, source only)

Patterns IDEN-5, BOUND-2 and IMPL-13. Delete `SelectedNativeSources` and its
independent installer. Delete the scalar source assignment from
`NativeRuntimeInput`, `PromptBinding`, `NativeInputIdentity`,
`NativeInputReference`, `NativeInputContext` and `CurrentNativeCursor`, plus
`NativeBindingSourceRule` and stage `anchor`. These fields cannot truthfully
identify an input containing several originals.

Existing `NativeInputExecution` owns membership projection and reservation.
FULL derives it from the existing `ExecutionAssignmentLink`; it stores nothing
new. TRIAGE records the pre-execution input relation in `NativeRuntimeTable`,
through the single native schema. History, notifications and selected tool grants
query that same declared family. Query results decode existing `WakeAssignment`
rows; an additional source-member DTO was removed during source review.

### Existing-owner search and why triage needs a relation

Actual source searches performed before retaining a new table:

```
rg -n '^class (InputBatch|ChannelInputBatch|ScheduledTurn|ClaimBatchReceipts|ClaimBatchMembers|ExecutionAssignmentLink)' src/agent_comms
rg -n 'ScheduledTurn|InputBatch.capture|ChannelInputBatch|read_historical_native_inputs|_selected_proven' src/agent_comms
rg -n '^class (NativeInputExecution|TriageNativeExecution|FullNativeExecution|NativeRuntimeInput|CurrentNativeCursor|CursorOwner|SourceCoverage|TriageNativeSources)' src/agent_comms
rg -n 'SelectedNativeSources|selected_native_sources|NativeBindingSourceRule|reference.assignment_id|cursor.assignment_id|binding.assignment_id' src tests tools
```

The first two searches found and led to reading these owners:

- `routing.ScheduledTurn.take_batch`: transient ordinary input grouping, with
  per-route grouping; it owns no selected native reservation identity.
- `channel_input_batch.InputBatch.capture` / `ChannelInputBatch`: proof of
  ordinary InputStore keys plus assembled prompt, captured by `OwnedTurn`.
  It is not a durable selected-native input-to-assignment relation.
- `cohort_schema.ClaimBatchReceipts` / `ClaimBatchMembers`: frozen recipients
  and claims for one committed wire message. A pending wave has many of these
  cohorts; cohort membership cannot identify a single triage input snapshot.
- `ExecutionAssignmentLink`: original many-to-one execution membership, reused
  by FULL. TRIAGE has no execution yet and must not invent one to record an
  IGNORE or uncertain outcome.

Only the missing relation, `TriageNativeSources`, is new. It records input ID and
ordered original assignment IDs, with immutable insert/update/delete ownership,
one-owner membership, and rejection of previously reserved originals. It copies
no disposition, source body, audience, current cursor or FULL membership.
The native stage family is declared once in `native_input_record.py`, the full
membership once in `coordination_tables/assignments.py`, and the triage relation
once in `triage_native_sources.py`. The final search finds no replaced class,
installer or singleton binding/reference consumer in source/tests/tools.

### Coverage and cursor crossing

`read_historical_native_inputs` joins each original through the stage relation
to its original `WakeAssignment` and sealed cohort delivery. It returns a
per-original proof pointing at the SAME original native input/context.
`SourceCoverage._selected_proven` still validates that original assignment
against its frozen recipient and inherited `SourceProofRequirement`.
`CursorOwner._matches_input`, `matches_prefix` and `admits` compare original
physical input reference, participant identity and send admission. Removing the
copied assignment makes the reference source-independent; `injected_seq` stays
the actual last proven original source. No separate live execution, seen list,
coverage algorithm or proof cache is introduced. Arendt/493 owns integration of
its retained evidence/custody reader changes against this relation.

### Explicit schema carry required before activation

Native schema 4 becomes 5. Live originals cannot be reset or dropped. Kepler
owns the one-shot source-format export/writer contract, Arendt reviews the
lifecycle/source relation, and the parent stopped-owner installer alone executes.
The carry must freeze after the remaining response-route relation is settled:

1. The original interpreter validates its own schema and reads original inputs,
   cursors and prompt bindings. Preserve DBs, sidecars, journals and UNKNOWN
   witnesses as originals; the target never decodes old journals.
2. Old FULL membership must equal the originally proven scalar source and its
   existing execution membership. Reject extra unproven members rather than
   silently promoting their coverage. Old TRIAGE membership is that original
   singleton source, even when unproven or uncertain.
3. Stage a copied target DB and binding sidecar. Project only removed source
   fields out of the new records. Preserve every input/execution/attempt ID,
   owner/token/admission, outcome/verdict, context/session/reference,
   expected-prompt digest and clock exactly. Cursor input and source sequence
   must still join the original membership; no proof or admission is generated.
4. Attest all surviving cells, all original memberships, and all non-native
   coordinator state before the installer publishes the coherent pair. Keep
   original DB/journal hashes. No restart between DB and sidecar publication.

The one-shot carry is implemented in `tools/cutover/native_source_carry.py`,
not yet executed or activation-ready. No runtime old reader,
compatibility alias, automatic rewrite or reset is provided. Parent and Arendt
received the concrete contract on PR493; no acknowledgment is assumed.

### Remaining route ownership, not hidden by same-route PASS

All-pending mixed-route answers require one execution/attempt with original
response obligations per route. `ResponseObligation` already owns route,
`canonical_publication_key` already identifies `(execution, target)`, and
`ResponseConversation` already owns original audiences. Current
`ExecutionRecord.exact_target`, singular `RecoverySnapshot` response fields,
execution-link target triggers and terminal checks duplicate/enforce one route.
Request Arendt's lifecycle integration/extension at that seam. Kepler owns
response publication and caller closure. Do not add an alternate batch execution
registry, send the same response body into different audiences, or loop native
calls per original. Same-route prototype evidence stays historical, not Ready.

### Validation status

No test, build or native journey has run on this source-shape correction.
Complete the nominal family/route/carry implementation first; batch validation
and the actual affected installed native/ACP journey last. The earlier five
originals plus late-arrival installed native PASS (21.199 s) belongs to source
`3e14c2ee`, whose wheel and original fixture remain preserved separately.

## One-shot source carry implementation (not executed)

`prepare_original(root)` runs with the authentic schema-four interpreter. It
strictly reads original input/cursor declarations and the existing sidecar
owner, checks original binding/input/source and cursor references, and exports
raw SQL cells plus source file witnesses. No original journal decoding,
process/native calls, `Comms` initialization, schema install or public mutation.
Old multi-source prototype tables are explicitly refused, not silently carried
as another source truth.

`carry_into(staged_root, packet, expected_stage_non_native_digest)` runs with the
target on the parent's disposable COPIED coordinator. The mandatory reviewed
stage digest allows Arendt's separately owned lifecycle carry to run first;
native carry must leave that reviewed non-native state unchanged. It refuses
the original root and hardlink aliases. It replaces only the copied native
schema, copies every surviving RAW SQL cell and strictly acquires it with the
current declarations. Existing `NativeInputExecution.record_sources` creates
triage membership or verifies FULL against existing execution links. It refuses
additional unproven FULL members and retains uncertain reservations as-is.
The current `NativeRuntimeSchemaMeta.create_schema` owns schema creation for
both bootstrap and cutover; there is no second DDL installer or version owner.

The original binding rows are projected into a separate current sidecar artifact
using existing `create_sidecar_file` / `sidecar_connection`. The old binding file
is not replaced by this tool. Output names both artifacts and original counts;
parent alone assembles/publishes them under stopped-owner custody. A failure
cannot activate anything and preserves originals plus the failed copied stage.
Parent must preserve its original journal/witness archive, integrate the final
response-route/lifecycle migration, and execute the one affected installed
journey after coherent source closure. No test or migration run was substituted
for design. Delete the transient cutover tool once that carry is accepted.
