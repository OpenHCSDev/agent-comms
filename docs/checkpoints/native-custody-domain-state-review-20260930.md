# Native custody domain state review

Receiving owner: Arendt. This reviews the exact source6feb634ba184d94d763028406152fff07027cccc
to c5f9bb45b358a0e76aaca9943b5ab50ebcf99b23 and its declaration consumers.
There is no authoritative count of18 new None sites. That number belonged to
retained-context mutation/corruption controls. No frozen production edits or
public inputs/restarts occurred during this review.

## Classification and required closure

| Declaration / original caller | Classification | Required relation / owner |
| --- | --- | --- |
| native_input_record.py:54 NativeInputIdentity.execution_id/attempt_ordinal | Extracted original nullable SQL triage/full fields; not permission or absent process. Triage has no execution; full requires a complete execution/ordinal pair. Internal identity must not carry an unchecked partial full record. | NativeSendStage already owns triage/full behavior. Native record boundary must derive complete stage identity from that owner, not treat partial fields as a valid identity. Arendt coordinates Mendel's stage builder. |
| native_input_record.py:104 NativeInputContext.reference | **Unclosed internal context state.** It probes only input_id/session_id then creates a required reference with potentially absent assignment/stage/request generation. | Original NativeInputReference must enforce a complete recorded-context value; original SQL context owner must distinguish reserved/no-context from recorded context and refuse partial groups. All cursor/history/reference consumers close through that declaration. Arendt owns correction. |
| native_source_cursor.py:214 expected reference | Optional historical proof is an actual read result; None currently also represents the cursor's no-injected-context case in comparison. | Complete reference / no recorded context belongs to the original context owner. Missing historical evidence must not mint a recorded reference or establish committed source. CursorOwner.matches_prefix/require_recorded_input/admits and NativeSourceCursor._require_source must consume the same relation. |
| native_pi.py:337/read_evidence and :910/_verify_context evidence parameter | Absence of an acquired resource. Cold caller acquires through NativeEntry.open_evidence; tracked caller passes its ExitStack-owned reader. Not model/input state or proof cache. | Existing resource owner verifies original prefix/current named inode/private ancestry and original SQLite proof on every observation; closes on refusal and ExitStack cancellation. Keep original ownership; no new state family needed for a call argument selecting acquisition. |
| native_pi.py read_evidence request_generation | Existing optional query bound: omission asks the journal owner for its selected/latest record. Not an uncommitted reference. | NativeContextRecord itself requires positive native-safe generation, exact committed input/session and digest; a lookup argument must never be copied into that record. |
| owner_lifecycle.py:122 sent_owner_admission_generation | Original durable absence means no recorded sending epoch, **not proof no bytes were sent**. The new release consumer branches on this semantic distinction. | Declared recorded/unrecorded admission witness should own release fencing. Unrecorded needs exact stopped receipt; recorded needs an including same-incarnation epoch. Both preserve UNKNOWN and grant no retry. Arendt owns native witness projection; no inferred NotSent. |
| tracked_turn.py:60/:114 selected_revision | Original optional fresh enrollment resource, newly typed FileRevision. Existing factory couples fresh_selected/revision but callers still carry two optional fields. | Einstein owns tracked TurnSession startup declaration. Selected startup custody must carry its original FreshPrivateSession plus FileRevision together; ordinary startup must not masquerade as missing selected proof. No enrollment reconstruction from stat. |
| pi_summary_payloads.py SelectedModel.from_runtime, manual:62/adaptive:71 | **Internal optional cached AgentRuntimeInfo plus configured model reconstruction**, not external JSON absence. Current rejection prevents fallback but authority still comes from runtime presentation metadata. | Canonical prepared native StateData.model / ReportedModel owns model identity/context window. Arendt closes both summary callers through Einstein's public model projection; deletes from_runtime, no runtime-info mirror. |

## Confirmed partial-reference witness

Mendel's bounded private probe copied a completed462 fixture coordination store.
UPDATE current_native_cursor SET request_generation=NULL on its existing
injected row succeeded: SQLite CHECK request_generation>0 evaluates NULL and
does not reject. CurrentNativeCursor.one(...).reference returned a
NativeInputReference(request_generation=None). Original request generation1.

Sanitized witness:
`/home/ts/.cache/agent-scratch/comms-goal-wait-certified-reply-20260930/none-context-shape-probe.json`.
Original store/journal unchanged; provider/public calls0. NativeRuntimeInput
mutation was correctly refused by its existing frozen-identity trigger. Original
NativeSourceCursor._require_source compares the durable historical proof and
rejects the malformed cursor. This proves incomplete internal construction;
it does not prove an authority bypass, accepted corruption or replay.

Required fix is a complete original context declaration and its full caller
closure, not an extra caller guard or another anonymous nullable chain. Preserve
the SQL layout/native/durable history. Tightening SQL declarations must be
classified with the existing schema owner before a future quiet cutover; no
current-format reader or production conversion is introduced. Existing tested
tool-latency/resource gate remains accepted at its actual scope, not evidence
that every domain-state obligation has closed.
