# #493 source ownership receipt; #489 lifecycle dependency

Source reviewed: `33f9118c477a921c963a512f8fabb2b3fe7cd5fd`.
Receiving integration owner: Arendt in #489. This receipt changes no production
source and makes no whole-lifecycle readiness claim.

## Existing-owner search

[source-ownership-search.json](../../evidence/native-evidence-reader-custody-20261001/source-ownership-search.json)
records actual declarations and every lexical symbol reference across all 347
authored Python/native source files in its declared scope, including each
enclosing Python declaration and file hash. Each of the 14 named types occurs
as a declaration exactly once. This establishes declaration locations; it does
not establish that another function cannot independently decide the same fact.
References in the artifact are search candidates, not inferred call targets.
Tests and compiled dependencies are excluded from this source receipt.

## Facts, determining owners and dependent consumers

| Fact | Existing determining declaration | Storage, proof or resource | Decision and consumers |
|---|---|---|---|
| Bytes belong to the acquired original journal and its unchanged prefix | `native_pi.PrivateEvidenceRead` | Open descriptor, original prefix/hash and filesystem observation | `rows`, `verify_snapshot`; consumed by `NativeEntry.open_evidence` and `NativeEvidenceRead.observe`. Rechecked on each observation, not granted by a prior decoded row. |
| Decoded entries of that acquired source | `native_entries.NativeEvidenceRead` | Descriptor-bound decoded bytes | `require_path`, `observe`, `close`; consumed by tracked turn, native context and prompt corroboration. No context result or disposition is stored here. |
| Lifetime of borrowed readers for one corroboration operation | `native_entries.NativeEvidenceScope` | `ExitStack`; at most one source reader | `for_source` closes on source switch; exit releases the reader. Created by historical reads and cursor read/advance, passed through coverage methods. Does not authorize input, coverage, recovery or publication. |
| Live native assembled input/context identity | `native_pi.NativeContextProof` | Original input/context events and immutable `NativeContextJournal` corroboration | `read_evidence` compares journal/header/tracked entry; `corroborates_input` binds the observed live proof. Consumers: `NativeSendStage.verify`, historical inputs, tracked context verification, continued-session verification and recovery. Borrowing bytes never substitutes for the original context record. |
| Exact reserved source prompt | `native_prompt_binding.PromptBinding` | Original prelaunch binding row | `expected_prompt_matches_journal` uses the original digest/tracked input. `NativeSendStage.require_bound_prompt` retains root, source, owner and execution checks; historical/recovery readers corroborate rather than mint a binding. |
| Original native reservation and committed context | `native_runtime_input.NativeRuntimeInput` and inherited `NativeInputReference` | Original coordinator input row, immutable context group | `NativeSendStage.pending_input`, reservation rules and context commit remain unchanged. Historical readers cannot promote an unrecorded or uncertain row. |
| Historical source had this original native proof | `historical_native_inputs.HistoricalNativeInput` family | Read projection joined from frozen delivery, claim, original input and binding | `read_historical_native_inputs` checks frozen membership, owner/path, exact context and prompt. `SourceCoverage.native_inputs`, wake-mode proof and cursor owner consume it. The projection grants no live admission. |
| Entire frozen addressed prefix is covered | `proven_source_coverage.SourceCoverage` | `ProvenSourceCoverage` is the ephemeral result, not another high-water authority | `read`, `_selected_proven`, `prefix` retain original checkpoint, audience, sealed receipt, wake-mode and historical proof decisions. Missing/UNKNOWN proof still stops the prefix. Consumers: cursor read/advance and existing source readers. |
| Current owner may publish this monotonic cursor | `cursor_owner.CursorOwner` and `native_source_cursor.NativeSourceCursor` | `CurrentNativeCursor` is the SQL projection of proved original source/input relations | `require_live`, `require_participant`, `matches_prefix`, `admits` and `_require_source` retain owner, admission, exact proof and monotonic checks. `_publish` keeps original CAS. Consumers: `CursorPublication`, selected-result status and current-cursor metadata. Cursor position grants no work or replay. |
| Cursor publication ordering | `cursor_publication.CursorPublication` | Connection-owned delivery/dedup resource | Always rereads the original cursor proof; it is not a second cursor store or input authority. #493 does not change its producer or consumers. |

## Optional resource boundaries are not nominal state closure

`SourceCoverage.read/evidence/prefix`, `read_historical_native_inputs`,
`NativeContextProof.read_evidence`, `read_tracked_input_digest` and
`expected_prompt_matches_journal` accept a borrowed resource or acquire one for
an independent invocation. The `None` branches select acquisition lifetime;
they do not represent unknown input, missing proof, absent coverage or permission.
The repeated acquire-and-recurse implementation is still an acquisition concern
to close at the existing resource owner in #489 (IMPL-13). It is not evidence of
polymorphism and is not described as a completed nominal state refactor.

## Dependency and retained boundaries

#493 changes acquired byte reuse in seven production files. It does not change
coverage membership, native reservation/context storage, cursor authorization,
source fencing, frozen audience or replay policy. These unchanged determining
owners remain necessary even when every class has one declaration.

#489 receives the whole lifecycle pass, including `continued_private_session`
and `attempt_recovery`, which still acquire context/prompt/journal evidence
separately. Close these through existing owners with all consumers together,
not local reader argument patches. Search existing families before proposing
types; source ownership analysis precedes implementation, validation is last.

Kepler #490 changes source assignment membership: FULL membership is derived
from original `ExecutionAssignmentLink`; TRIAGE requires its preexecution frozen
membership. Removing `assignment_id` from original input/reference/cursor rows
and schema 4→5 is not a disposable cursor-only reset. Preserve original live and
UNKNOWN reservation/proof references through the jointly reviewed storage seam.
Sch #494 owns retained-content framing and its reservation bound. #489 consumes
that original typed source and existing budget policy; neither checkpoint creates
another content, budget, session or cursor authority.

Original journals, UNKNOWN dispositions and existing receiving receipts remain
protected. No tests, provider calls or public mutations were performed to write
this semantic receipt. Historical results in the earlier checkpoint remain at
their documented strength and are not used to choose the ownership design.
