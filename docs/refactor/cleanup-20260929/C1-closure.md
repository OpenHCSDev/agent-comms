# C1 Pi vocabulary closure

Owner: C1 worker. Draft Core PR [417](https://github.com/OpenHCSDev/agent-comms/pull/417).
Persistent worktree: `/home/ts/wt/comms-cleanup-c1-pi-vocabulary-20260929`.
Scope: every C1 requirement and the C0 site rows assigned to C1. Current main including PR416 was integrated normally; the dirty live checkout is untouched.

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

- Source-focused payload, RPC, import, registration, mentions and family guards: **96 passed in 2.10s** on the final candidate; the final strengthened family guard additionally passed all 3 cases.
- Caller suite including backend: **284 passed, 1 failed in 28.64s**. The failure expects `prompt was not sent` in a preflight error. The exact same assertion also fails against an isolated copy of current main `350b14a9` (1 failed in 0.35s), confirming it is pre-existing. The reproducer and both results were sent to the parent for the feedback owner's disposition. This suite is not reported green.
- Earlier expanded fresh-session/coordinated-runtime run: **176 passed, 2 skipped**. Those skips are not native acceptance.
- Retired `test_selected_summary_exchange.py` fails before assertions because its child imports removed `SelectedSummaryAttempt`. Mendel owns tests-only PR422 to replace that fake child with the retained actual-native fixture, preserving protocol negative coverage. No compatibility export was restored.
- Noneditable `[acp]` candidate is staged in owned persistent scratch. Actual installed native/ACP acceptance is **pending the serial fixture slot**, with Arendt's urgent PR416 run taking priority. No live readiness or installation claim is made.

## Deletion and ownership accounting

Relative to integrated current main `ab3397a6`: **501 production lines deleted across 25 changed production files**, with 1,085 added declaration/effect lines. This is deletion of replaced dispatch and ownership code, not a claim of net line reduction. `git diff origin/main --numstat -- src/agent_comms` is the exact file-by-file receipt. Recompute if main changes before integration.

Final committed package-ratchet result against `ab3397a6`: 10 fewer string-dispatch subjects / 36 fewer arms; 2 fewer type-switch subjects / 7 fewer arms; 42 fewer foreign absence probes, with no increased measure. All measured deltas are nonpositive. This is a syntactic debt measurement, not live acceptance.

## Coordination and resources

Parent owns C0 module seals and TurnProgress/OwnedTurn. This worker alone edits TurnSession's input methods. Arendt owns PR416's native output budget and streaming summary feedback; terminal response/settlement changes were agreed directly. Mendel owns PR422's obsolete fixture retirement and C2/C3.

Owned scratch: `/home/ts/.cache/agent-scratch/comms-cleanup-c1-pi-vocabulary-20260929` (22 MiB at checkpoint), containing staged wheel environment and bounded test logs. Latest resource check: home19.8 GiB, RAM13.6 GiB, swap11.1 GiB; warnings reported to parent. No extra helpers or native fleet was launched. Native acceptance runs serially against localhost fixtures, cleans owned child processes, and never replays uncertain input.

## Exact production file receipt

| File | Added | Deleted |
| --- | ---: | ---: |
| `src/agent_comms/backend.py` | 76 | 2 |
| `src/agent_comms/config_options.py` | 4 | 3 |
| `src/agent_comms/coordinated_runtime.py` | 2 | 1 |
| `src/agent_comms/fresh_private_session.py` | 3 | 2 |
| `src/agent_comms/import_records.py` | 366 | 0 |
| `src/agent_comms/importing.py` | 31 | 134 |
| `src/agent_comms/native_attestation.py` | 6 | 1 |
| `src/agent_comms/native_entries.py` | 6 | 10 |
| `src/agent_comms/native_pi.py` | 2 | 1 |
| `src/agent_comms/owner_compaction_adaptive.py` | 1 | 18 |
| `src/agent_comms/owner_compaction_manual.py` | 3 | 10 |
| `src/agent_comms/pi_commands.py` | 2 | 1 |
| `src/agent_comms/pi_events.py` | 71 | 181 |
| `src/agent_comms/pi_payloads.py` | 133 | 27 |
| `src/agent_comms/pi_summary_payloads.py` | 128 | 8 |
| `src/agent_comms/pi_vocabulary.py` | 199 | 0 |
| `src/agent_comms/private_send_admission.py` | 2 | 1 |
| `src/agent_comms/selected_pi_summary_rpc.py` | 6 | 59 |
| `src/agent_comms/session_lifecycle.py` | 2 | 1 |
| `src/agent_comms/threads.py` | 5 | 11 |
| `src/agent_comms/tools.py` | 4 | 3 |
| `src/agent_comms/tracked_turn.py` | 20 | 20 |
| `src/agent_comms/turn_inputs.py` | 7 | 6 |
| `src/agent_comms/turn_runner.py` | 2 | 1 |
| `src/agent_comms/turn_usage.py` | 4 | 0 |

## PR416 integration checkpoint

Merged normally as `7e593e0a`. The sole conflict was the selected summary method signature: both PR416's `reason` argument and C1's `SelectedSummaryData` return were retained. Source/text progress callbacks and the case-owned terminal/settlement methods remain intact. The candidate was rebuilt noneditably; product imports resolve to the owned installed environment's site-packages with PYTHONPATH removed. The same 96 focused cases passed against that installed artifact in 2.78s.

Native package for serial acceptance: `/home/ts/wt/comms-post-cancel-session-custody-20260929/stack/.pi-native-7817b54ec2534555/node_modules/@earendil-works/pi-coding-agent`. Arendt owns the priority native/UI slot and was contacted directly. Parent controls the old-owner cutover; this worker does not attach or mutate live owners.

## Installed native acceptance correction

First serial installed/native run: 9 passed, 15 failed in 154.07s across 24 cases. Real queued/reopen, provider failure, disconnect/noReplay and native catalog cases passed. Every failure came from C1's adaptive result methods importing SelectedNativeSummary or SelectedSummaryDecline from the wrong module. Those imports now point to their existing declarations in owner_compaction_runtime; no declaration was duplicated or relocated. The installed candidate was rebuilt, and only the 15 affected continuous ACP/goal cases are rerunning. Initial failure evidence is retained in owned scratch; no readiness claim relies on that failing run.

ACP/native argument test expectations now assert the typed ThinkingLevel declaration internally while keeping the scalar wire assertions unchanged. The first supplementary ACP/config run omitted its required native-package environment, causing fixture setup failures; it is not a product result and will be rerun with the exact package pin serially.
