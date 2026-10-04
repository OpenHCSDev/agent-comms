# S4: Original SDK full-history selection and admission

## Working scope

The SDK `EntryStore.uncompactedMetadata` owns the original full-history entry
selection. The private constructor currently retains its chosen entries but
drops that original full-history reference. Python's message-entry counts and
the constructor label cannot replace the SDK's selection.

Retain that original selection as `JournalProvenance` at the same acquired
`NativeWitness`. All constructors, installation observations, original probe
readers and scorer consumers use the existing owners. `RecordedConditionInstallation`
validates both original selections against the acquired ancestry and derives
their equality. Delete the message-count completeness flag.

Join this selection to the complete constructor/request prefix and the original
request's `ContextBudget` admission. Report SDK-estimated full-history admission
separately from actual provider-token capacity, HTTP bytes and a matched study.
Missing historical selections remain unavailable; no reconstructed evidence.

Before-edit Python AST: existing refactor-audit parser over src/tests/tools;
raw `.artifacts/s4-full-history-admission/owner-before.json`. SDK entry selection,
SessionContext conversion and budget semantics read directly in original stack
source. Lexical sites do not prove dynamic resolution.

## Implemented family

The SDK full selection is now acquired once and shared across all four
constructors; full-context reuses those same entry IDs. The installed observer
retains the original reference. `RecordedConditionInstallation.require_selection`
validates chosen and full references with the same ancestry algorithm, including
present invalid references beside missing evidence. `source_selection` derives
ordered equality; the Python message-count completeness flag is deleted.

`RecordedNativeProbe.full_history_admission` joins that result with the complete
bound request prefix and original native admission. ScoredScenario and the
configured receipt consume that measurement. Missing rounds remain unavailable;
smaller/changed contexts are observed separately from full-history admission.
Provider capacity and overall condition/study construction remain unqualified.

The real SDK record exposed a boundary defect hidden by authored internal-form
records: a nested Pi manifest has no internal family tag. The existing installation
record now inherits `PiPayload`, uses its original `from_wire` normalization and
exports the nested manifest's native representation. Every raw-record ingress
migrated; no extra codec or manifest type.

Original SDK serialization acquisition is shared by whole-value validation and
the NativeMessages projection. Whole-value disagreement still refuses; it cannot
be swallowed to gain whole-request credit. The message question authenticates
all original digest/length metadata and every selected message value independently.

## Actual authored SDK result and preserved negatives

Four final authored controls passed in .37s. They detect missing/bad selections,
changed complete prefixes, unavailable binding/budget/rounds, external Pi decoding
and message evidence being confused with whole system/tool value agreement.
The existing JS fixtures/inspector parse successfully.

One original immutable086 SDK journey ran with `--installed-condition-loop
--full-history-partition`. One controlled SDK stream, zero submitted prompts or
providers, no user/donor reads. The original constructor installed the SDK's five
uncompacted entries. Four complete message parts are preserved through the original
transform, converter and onContextReady request. Original request/session/input
hashes agree. The same SDK journal and transform restore unchanged; both child
processes are absent. Whole native tree verifies unchanged and the 086 loan is
**HAND BACK**.

This is not an overall clean gate: the controller timed out after 45.033s waiting
for the debugger to disconnect **after** the SDK wrote its complete result. The
raw timeout is retained. The inspector also mistook the constructor's legitimate
preview for a request, and its return-by-value expansion changed executable tool
callbacks into objects. The whole-value reader correctly refused the latter.
The independently authenticated message projection above remains valid; whole
system/tool capture is **unqualified**. Future inspector capture now selects the
owner's request ID and crosses the original JSON boundary. Those source corrections
were not rerun; no second stream hid the negatives.

Exact sources, raw hashes and original handback: [receipt](../../evidence/s4-full-history-admission-20261004/receipt.json).
Python AST covers 736 modules without parse omissions. JavaScript source was read
semantically and syntax checked; an AST grammar was unavailable, not silently
included in the Python coverage or claimed as dynamic-resolution proof.

Source/private measurement and actual SDK **message partition/history-reference**
checkpoint only. The controlled stream did not execute a provider budget admission.
No enrolled input/custody, configured matched arms, HTTP, provider-token capacity,
model recall, billing, full study or full S4 claim. Runtime/native source is unchanged.
The 30-pair/USD75 study remains unapproved. Full S4 stays active and unfinished.
