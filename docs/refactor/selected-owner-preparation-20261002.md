# Selected owner preparation closure

Einstein owns #506 source and caller closure. Arendt owns runtime/custody and the proof contract; Mendel #510 owns stopped historical carry; Sch owns native compaction policy and payload. Content-owner correction: `2eac51df`, source/fixtures `34c5a666`; current resource integration: `93f1f5e6`. Source reasoning precedes implementation, then final validation.

## One original content owner

`InputDocument` owns mutable `StoredInput` rows. `StoredInput.context_provenance()` supplies the existing durable key/origin identity. `InputTaskFact.source` holds the original frozen row in `RetainedTaskFacts`; `StoredInput.digest` derives its exact content witness. The mistaken digest field on global `InputProvenance` is deleted. Existing historical manifest and USER-pin encodings retain their original shape; there is no optional digest, fallback reader, replacement type or original-wire rewrite.

`InputBatch` captures the admitted ordered rows and derives keys/rendered text. Persisted `SelectedAdmissionSource.originals` and `CompactionSource.pending_inputs` hold only ordered identity references, not another copy of original text or digest roster. The final rendered prompt's `input_digest` is a different fact from each original's content. Genuine native input IDs and original proof bytes remain distinct.

`RetainedTaskFacts.original_inputs()` resolves those references through the existing `ExactTaskFact`/`InputTaskFact.input_sources()` hooks. Missing or ambiguous frozen originals refuse; current mutable ledger content cannot supply absent captured evidence. `StoredInput.matches_original_source()` owns identity/content comparison. `StartedInput.proves_started()` receives that original row rather than a copied scalar digest.

`SelectedSummarySource` couples its source and retained payload for reservation, interrupted recovery and native-start checks. `InputSourceCheck` borrows that same retained payload. `ContentChangedRule`, `SelectedSummaries.reserve`, interrupted reconciliation and `SelectedSummaryAttempt.original_has_started` consume it. Final admission checks the original `attempt.request`, not a newly assembled payload or bare identity. Existing private-history coverage continues to require original native/input receipts after the same reservation check. Atomic `InputDispositions.bind_originals` prevents partial plural transitions.

## One acquired preparation resource

Selected attempts reserve once. `PrivateSendAdmission.prepare_context` runs after `TrackedTurnSession.attest` and before raw prompt admission. `SelectedSession` members own fresh/saved behavior. Saved preparation borrows the acquired native child through `PersistentPiSession.retain`; a clean skip reuses it, and actual journal mutation follows strict retirement/reopen. Shared `TurnSession.native_acquisition/open_native/resume_prepared` and the original `AsyncExitStack` own resource lifetime, including the original tool socket. No second cold preflight child or persistent session owner.

`OwnerCompactionCommit.compact_selected` supplies common selected-policy/summary/commit orchestration to saved preparation, manual compaction and adaptive admission. Existing native `CompactionPolicy`, `NativePreparationResult` and `CompactionResult` own budgets and readiness/refusal. Detached settings readers, policy overrides and enable bypasses were deleted. Distinct real operations and concurrency remain intact. No latency claim is made from source structure.

#511's declaration-owned context assembly is integrated normally at `9e3fafa9`: `TurnContext` renders `RenderedInput`; private admission forwards its text at the external prompt boundary and original contribution coordinates to tracked native input. Selected and ordinary publication use `NativeContextManifestData.record`. Integration preserves current participant/session ownership and removes the obsolete scalar launch fields rather than restoring them.

## Exact historical proof contract

`SelectedSummaryAttempt.request` is the single current typed semantic request. `source_json` is the exact immutable original native SHA proof string, never decoded by a runtime semantic reader. New reservation serializes the same request once; `TypedTable`/`FieldCodec` own storage. Original `SelectedCommitReference.require_source` and `TextDigest.of(source_json)` remain unchanged. Deleted `source()`, `envelope()` and obsolete request readers remain absent.

Mendel's certified original-prefix scan found 23 historical manifest provenance references without a digest and no native-input pins in that observed prefix. That is actual presence evidence, not a universal pin-absence claim. Restoring the existing identity format addresses the source relationship without rewriting those records. #510 authenticates original declaration transforms and preserves source bytes, native links, enrollment, states and UNKNOWN. Carry never issues a send ACK or readmits an old epoch.

## Validation and delivery boundary

The final focused source batch passed 66 checks in 5.35s. An earlier batch's 62 passes and three stale fixture failures are preserved; one removed-digest assertion and two string `FileRevision` fixtures were migrated, with no product accommodation. Existing negative source/native receipts remain intact. Logs and exact receipt: `evidence/selected-owner-preparation-20261002/source-contract/retained-content-owner-sanity.json`.

This content-owner correction deletes 27 production lines and adds 80 across nine files relative to `9e3fafa9`; it creates no types. The integrated branch delta includes reviewed producer/context ancestry and is reported separately by the AST output. The existing archive AST map records declarations, writes/checks and syntactic consumers. Native/SDK/dynamic-resolution omissions are explicit; Python parse completion is not whole-runtime proof.

Final acceptance remains one coherent installed Native6 saved configured fork through compaction, native/ACP input, original proof preservation and cleanup. It must exercise plural admission and same-child skip/strict reopen through the actual application, not a substitute fresh journal. Installation remains paused for architecture alignment; no default, original bus, provider input or native donor was changed here.

Patterns: IDEN-5 separates durable identity, captured content and original native proof; TIME-9 removes the old scalar representation beneath the ordered source; BOUND-8 keeps source behavior across reservation, recovery and admission; IMPL-12 puts shared preparation lifetime on its existing owner.

## Shared resource integration

Normal merge `cec407c2` incorporates Arendt's published #509 `1059126a`, with the current selected-session and retained-content owners preserved. `SelectedParticipant.require_current(resource)` receives a worker-owned coordination resource; saved preparation uses existing `Coordination.run_async`. Context and phase publication use the same joined worker mechanism; `93f1f5e6` closes native identity publication through it too, assigning only the returned canonical owner. The three original synchronous main-thread callers keep their own resource. No connection crosses workers.

The original raw admission now commits UNKNOWN and the exact `NativeSessionIdentity` under its existing fences, closes those resources, then yields to the one raw writer. Its native identity, acquired-operation measurements and one-use token are retained. Deleted the old `_exclusion` raw-write lifetime; no second custody state or replay path. Asynchronous selection and cursor capture retain the current awaited `SelectedSession.prepare(..., package)` factory.

The 66 passing checks above cover the earlier retained-content correction, not this resource integration. No repeated slice checks were run. Final validation stays one installed saved configured-fork workflow after the coherent Native6 artifact and #510 carry exist: detect lost plural membership/content, second cold preparation children, wrong saved session selection, changed historical proof bytes, and premature/duplicate admission. No separate fixture or serial gate is added. Existing source maps remain qualified to their recorded commits; #509's resource-family census is included by normal ancestry.
