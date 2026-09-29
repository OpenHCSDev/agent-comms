# C1 Pi vocabulary closure

Owner: C1 worker. Draft Core PR [417](https://github.com/OpenHCSDev/agent-comms/pull/417).
Persistent worktree: `/home/ts/wt/comms-cleanup-c1-pi-vocabulary-20260929`.
Scope: every C1 requirement and the C0 site rows assigned to C1. Current main was integrated normally; the dirty live checkout is untouched.

## Requirement receipt

| Requirement | Declaration and migrated consumers | Acceptance evidence |
| --- | --- | --- |
| Shared stop vocabulary | `pi_vocabulary.py::PiStopReason`; `pi_payloads.py::AssistantMessage`; event application, tracked-turn settlement, native failed-terminal recovery | Pi payload/RPC tests, native lifecycle acceptance pending |
| Compaction and thinking vocabularies | `CompactionReason`, `ThinkingLevel`; typed event/state/native-entry/thread fields; config catalogs, command/UI serialization, fresh launch and selected-runtime admission | Registration inheritance, fresh-session and coordinated-runtime tests; native catalog acceptance pending |
| Decode external input once | `PiPayload` projects external extension shapes and invokes existing `FieldCodec`; typed user/assistant/absent message effects; no codec subclass | Payload/RPC tests and declaration guard |
| Native input custody and callbacks | Existing `TurnSession` owns public admission, original/followup start, notification, forwarding and output queries; message cases delegate; `InputForwarding` and `NativeAttestation` expose derived facts | Backend caller suite; retained native original/queued/reopen acceptance pending |
| Summary cases own response | `SelectedSummaryData.response/settle/require_result/manual_summary/adaptive_summary`; shared witnessed-source validation; RPC envelope delegates; manual/adaptive callers consume case methods | RPC/guard tests; installed native failure and ACP compaction acceptance pending |
| Imported formats own their cases | `import_records.py` declares OpenCode source/part and Codex record/item families; existing `ImportBuffer` and reverse scan retain bounded checkpoint recovery | Existing import saved-history, missing-request and large-prefix tests |
| Delete replaced code and prevent recurrence | Central string/type switches, selected-result mirror, nullable summary compatibility properties and callback probes removed; source guard rejects vocabulary comparison bypasses and summary switches | Package ratchet and exact deletion receipt below |

No C1 type needs custom `WireValue`: existing field metadata and declared families preserve their external scalar/record forms. PiPayload's explicit boundary methods delegate FieldCodec; making them codec callbacks would recurse. No alternate serialization mechanism was introduced.

## Current verification

- Source-focused payload, RPC, import, registration, mentions and family guards: **96 passed in 2.10s** on the candidate before the final guard assertion.
- Caller suite including backend: **284 passed, 1 failed in 28.64s**. The failure expects `prompt was not sent` in a preflight error. The production preflight line is unchanged from current main; the exact reproducer was sent to the parent for the feedback owner's disposition. This suite is not reported green.
- Earlier expanded fresh-session/coordinated-runtime run: **176 passed, 2 skipped**. Those skips are not native acceptance.
- Retired `test_selected_summary_exchange.py` fails before assertions because its child imports removed `SelectedSummaryAttempt`. Mendel owns tests-only PR422 to replace that fake child with the retained actual-native fixture, preserving protocol negative coverage. No compatibility export was restored.
- Noneditable `[acp]` candidate is staged in owned persistent scratch. Actual installed native/ACP acceptance is **pending the serial fixture slot**, with Arendt's urgent PR416 run taking priority. No live readiness or installation claim is made.

## Deletion and ownership accounting

Relative to integrated current main `a565cada`: **497 production lines deleted across 25 changed production files**, with 1,085 added declaration/effect lines. This is deletion of replaced dispatch and ownership code, not a claim of net line reduction. `git diff origin/main --numstat -- src/agent_comms` is the exact file-by-file receipt. Recompute if main changes before integration.

Previous committed package-ratchet result: 10 fewer string-dispatch subjects / 36 fewer arms; 2 fewer type-switch subjects / 7 fewer arms; 42 fewer foreign absence probes, with no increased measure. Final current-main measurement follows the last source checkpoint.

## Coordination and resources

Parent owns C0 module seals and TurnProgress/OwnedTurn. This worker alone edits TurnSession's input methods. Arendt owns PR416's native output budget and streaming summary feedback; terminal response/settlement changes were agreed directly. Mendel owns PR422's obsolete fixture retirement and C2/C3.

Owned scratch: `/home/ts/.cache/agent-scratch/comms-cleanup-c1-pi-vocabulary-20260929` (22 MiB at checkpoint), containing staged wheel environment and bounded test logs. Latest resource check: home19.8 GiB, RAM13.6 GiB, swap11.1 GiB; warnings reported to parent. No extra helpers or native fleet was launched. Native acceptance runs serially against localhost fixtures, cleans owned child processes, and never replays uncertain input.
