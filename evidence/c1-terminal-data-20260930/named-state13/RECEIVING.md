# C1 item 5 receiving receipt

**43 production lines deleted, 131 added, eight production files.** This is the owned correction after `166c97a80b964eb7e213a46a12fe8bb9b7142256`, not the unrelated merged cohort's deletion count. The parent contributes fresh-file state changes separately.

Apply IDEN-3, IMPL-10 and BOUND-1: native omission may be legal JSON, but omission used to choose behavior after decoding is an internal state. Moving an old check into another method does not close it.

## Per-site source facts and receiving disposition

| Lead at frozen source | Origin and actual contract | Receiving change / owner |
| --- | --- | --- |
| `pi_events.Response.require_request`: `self.data is None` | New C1 method. Native `RpcResponse` has successful prompt/abort acknowledgements without data, failed responses without data, and data-bearing queries. A missing payload must not pass a selected query or capability preflight. | `MissingData` in existing `PiResponseData`; decoder handles external omission/null once. `require_payload` owns refusal. Correlation, snapshot identity invalidation, GetState, stats, catalog and attestation consumers migrated. `EmptyData` remains a distinct existing empty-object payload. |
| `pi_payloads.AssistantMessage.tracked_end`: content `None` or string | Guard moved from original `TrackedTurnSession.assistant_end`; native `AssistantMessage.content` is a REQUIRED array, unlike `UserMessage.content`. This is malformed external input, not an admissible assistant state. | Assistant overrides the declaration with required tuple content. FieldCodec rejects missing/null/string at ingress; terminal shape guard deleted. Original `PiStopReason` owns the terminal verdict and provider failure detail; optional error text no longer competes with stop reason. |
| `pi_payloads.StateData.matches_model`: `self.model is not None` | New C1 public relation, consumed by parent's `FreshRuntimeRule`. Native `RpcSessionState.model?` is externally optional; consumers previously rebuilt missing-model state in three AgentInfo projections and native preparation. | Original `PiModel` becomes one declared family: `UnreportedModel` and `ReportedModel`. No model identity exists on the unreported member. Selection, identity matching and display queries derive from the member. StateData, GetState, native preparation and original FreshRuntimeRule public relation share it. |
| `pi_summary_payloads.SelectedModel.from_runtime`: `observation is None` | Two internal manual/adaptive compaction callers. `AgentRuntimeInfo` is our mutable runtime observation, not incoming optional JSON. This helper already existed before the current 458 production delta. It reconstructs selected model metadata. | **Open shared closure, Arendt owns both callers** via the prepared canonical native model observation. No local absent-observation wrapper or new summary flags. Original model capability supplied by this checkpoint; no edits to shared summary files here. |
| `native_tools.EditTool.result_diff`: `result is None` | Old `ToolDiff.from_result` absence guard moved into EditTool. Extensions may omit tool output, and partial/native tool results cross a boundary with opaque extension-defined details. Missing result affects text and evidence projection afterward. | `MissingToolResult` / `ProvidedToolResult` in the original result owner. ToolExecutionEnd and ToolExecutionUpdate hold nonnullable results and project text directly. EditTool delegates evidence projection to the result member; saved ToolResultMessage constructs the provided member. Old consumer probes deleted. |
| Tracked nullable evidence / selected revision | `input_event`, `context_event`, `terminal_error`, optional selected source/revision existed at `6feb634b` before these C1 edits. 458 changed revision's type to canonical FileRevision, not its absence policy. Arendt's `self.evidence` is acquired under the existing ExitStack at InputCommitted, never initialized None; it is the actual read resource. | No new nullable acquired evidence. **Existing internal milestone/outcome/selected-start state debt remains explicit** under Einstein's tracked-data closure coordinated with Arendt's original admission/custody owner. It is not exempted as optional external JSON and no readiness claim is made for its removal. |

## Entire caller closure in this checkpoint

Production files: `pi_payloads.py`, `pi_events.py`, `pi_vocabulary.py`, `native_tools.py`, `pi_commands.py`, `native_attestation.py`, `turn_stats.py`, `turn_runner.py`.

Boundary normalization is the only place that interprets external null for the new response/model/result members. Internally FieldCodec encodes their derived family kinds; no union is decoded by record shape. Neither a FieldCodec subclass nor a second codec, store, progress bit, seen list or registry was added. The two new family roots are abstract, so constructing an unnamed root is disallowed.

Native raw records, `role`/`type`/stop spellings, outbound native commands, selected-summary RPC and actual journal bytes are unchanged. Named kinds belong to decoded Python observations, not a new native ABI. Existing internal FieldCodec observation projections now contain the explicit member kinds; no existing native journal is rewritten. Opaque tool details remain opaque; optional `patch`, `diff`, `firstChangedLine` are external extension metadata. Null primitive validation and external optional scalars are not silently reclassified as our lifecycle states.

Exact native declaration hashes and production counts are in `receipt.json`. Relevant contract evidence is native593 `rpc-types.d.ts:153-166,166-236`, pi-ai `types.d.ts:312-336`, and pi-agent-core `types.d.ts:413-418`.

## Verification and frozen evidence

`source-focus16.log`: **53 passed in 1.54 s**. Existing real JSON-line decoding/stream and saved/ACP tool-diff checks, new named absence / required-content cases, original FreshRuntimeRule refusal cases, exact selected-summary attestation cases and the existing vocabulary guard extended to forbid nullable reconstruction in these consumers. This is source evidence, not the new installed user journey.

The former tool fixture's malformed assistant records now explicitly carry their required empty arrays. No product fallback was added to tolerate a fake missing array.

Frozen Core `ea8aa4eaee1ce3eed9aef19cd44010b592f83b26`, Toad `c0750340f069c90be2c65bb8f8d7f8affd32d02a`, stage and original installed11/12 inputs/raw/UNKNOWN receipts remain untouched. Installed12's five functional controls passed, but its physical frame observer and canonical bus observation failures remain recorded. Kepler is examining the same raw raster; Heisenberg/Sch own the actual domain/return workflow defects. Whole BUS is not Ready.

Required next installed acceptance: one new meaningful paired native/ACP/Toad cohort after parent fresh-file states and shared summary/domain corrections. No original input retry, paid replay, public package mutation or default activation occurred here.

## Tracked-data continuation after the receiving review

The inherited milestone/outcome gap above is now corrected in the second source checkpoint: **28 production lines deleted / 108 added**, two files relative to published `34622579`. This count includes the one-line stop-reason callback migration; do not add these repeated-line deltas to the first checkpoint to manufacture a final net count.

`NativeCommitObservation` is a named pending/observed observation of the **original** typed event object. It stores no copied IDs, process, source, admission or context digests. Original InputCommitted capture still happens before the acquired-reader operation; a repeated input fails before another FD opens. Context receipts may arrive first, and later same-input context receipts replace the earlier receipt exactly as before. Context proof still requires original prompt admission, original immutable events, canonical attestation and `_verify_context` over the original acquired reader. No new read policy, clock, watchdog, store or retry authority.

`TrackedTerminal` owns pending, completed, ambiguous and failed terminal data. The old nullable error slot and parallel final-message list are deleted, including every consumer. Failure remains dominant over later tool/final observations, tool rounds discard a preceding valid reply, multiple final messages are ambiguous, and the unique authoritative terminal must equal the original streamed chunks. Original native journals keep all records; dropping an in-memory ambiguous answer list does not delete source evidence. PiStopReason calls the original session's new terminal effect, without a copied error verdict.

`tracked-state-focus20.log`: **41 passed in 1.51 s** through actual decoder/stream/saved projection plus state relation tests and guards. `tracked-state-focus19.log` preserves the failed test's wrong exception-class assertion; the product continues to raise original NativePiUnavailable. These are source checks, not an installed native journey.

Arendt owns the disjoint selected-start resource pair and the parent's newly granted prepared-model caller closure. The frozen ea8/c075 install remains untouched. The producer owner will integrate those state closures, current main and substantive 252/460 domain corrections before ONE affected real native/ACP/Toad journey; this source checkpoint does not declare readiness or replay old attempts.

## Current main ACP crossing and next user journey

Normal merge of main `03e9e4b6` incorporates 463 typed ACP error decoding and 465 original proxy retirement. No C1 production change is made by this integration. The existing Pi/terminal payload, FieldCodec JSON shape and C1 guard crossing passes **34 in0.89s**; `merged-source22.json` records the exact source and scope. This supports source preparation only.

The next newly coherent installed native/ACP/Toad gate must show the original input in submission for an idle agent and queue for a busy agent **before delivery**. Once physically presented, the same original input must remain visible once through its transfer into native user/chat, joined by original ACP and native IDs. Kepler owns the actual incremental ANSI oracle, Heisenberg owns the duplicate caption/source publication correction, Arendt owns selected-start/prepared StateData, and Einstein owns the sole installed joint gate. No old uncertain input or frozen gate is replayed. Current live253 does not include these queue/backend candidate changes.
