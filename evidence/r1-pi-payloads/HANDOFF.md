# R1 known Pi payload boundary — source complete

2026-09-28 · Pascal · parent owns integration, paired Toad and activation.

- Tree: `/home/ts/wt/comms-refactor-r1-pi-payloads-20260928`
- Branch: `codex/refactor-r1-pi-payloads-20260928`
- Production/test source: `d13c848f5720e230da39d62a96cfd218ba1a6a0f`.
- Reconciled main194 `d092997b1fd07cfb487808fa8ca8f37d611d29cd`, including R2/192 and R5/195. Merge was clean. R5's deleted ActivityLog._load had one remaining ACP test caller: it now checks the actual saved activity trail, without restoring the method.
- Draft PR: https://github.com/OpenHCSDev/agent-comms/pull/196
- Parent reports all88 existing saved sessions latest/prior bounded pages and source Toad tool-diff pilot passed. Parent is correcting its obsolete transcript race fixture; no production Toad change identified. Parent plans one fresh configured-provider queue/compaction check before activation.
- Source blockers: none. Parent's mounted Toad/history checks and live installation remain parent-owned. No provider calls, new models/helpers, installs, live writes or restarts were performed.

## Ownership and deletion closure

`pi_payloads.py` uses existing DeclaredFamily and FieldCodec to project known external records once into typed message/content/delta/usage/model/state/stats/tool-result owners. It is not another primitive codec. Text/thinking/tool deltas own their emitted behavior. Command declarations select response payload ownership; strict selected summary/probe/settings records reuse NativeWitness and PiCompactionDecision.

Deleted: PiEvent Mapping/get/item/iterator/wire mirror; Response.command_type adapter; known-event Any fields; backend._result_text; UsageAccount.positive_tokens forwarding; manual _count raw-type adapter; selected route _strict_echo and command/reason rosters; repeated known native response/message/content/usage decoding; raw watchdog/discovery/native/extension-UI/selected-child command envelopes. Full current callers use typed events/commands; no compatibility exports or constructors were added.

Unknown external events, content, deltas and commands remain opaque. Tool arguments/details remain extension-defined external data; ToolDiff alone interprets its actual edit details. Optional malformed total-token telemetry is normalized to unavailable at the boundary, preserving old display behavior; proof-bearing SummaryUsage still rejects missing/invalid/bool counters. Missing text/delta fields cannot certify a native final response. Missing assistant content remains distinguishable from an empty content list on the strict native path.

The only shared codec change preserves declaration failure detail through union failures; accepted/rejected shapes are unchanged. Existing session, journal, grants, sealed sender, proof files, process lifecycle, billing and settings owners remain authoritative. Exactly-once selected handoff still requires exact witness/model/settings/cutpoint, journal reservation, persisted proof and existing admission. No readiness observation grants input authority.

`caller-map.md` gives the complete migration surface; `changed-paths.txt` lists21 production and8 test paths. No owned_turn/input_drain/owner_compaction_commit, Activity/RuntimeInfoStore/SharedLedger production, ACP session lifecycle, Toad or live settings changes. The narrow transcripts tool-result decoder remains R1's; complete saved-transcript ownership stays R6.

## Local acceptance (overlapping batches, not additive totals)

All commands use existing integration interpreter, `PYTHONPATH=src`, `pytest -o addopts=''` (no xdist/coverage gate), bounded60–90s. CI deferred.

| Receipt | Actual result / meaning |
| --- | --- |
| current-main-consumers.log |385pass,1failure in386 cases: only obsolete R5 ActivityLog._load test caller. Includes typed boundary, streams/settlement, nominal RPC, tools/diffs, ACP and MCP. |
| final-seam-repairs.log |75pass, including that exact R5 activity test plus final tool extension-data, malformed boundary, MCP, manual compaction and settlement repairs. All concrete failures above resolved; original failed log retained. |
| current-main-native.log |83pass,1skip. Native prelaunch binding/UNKNOWN/owner changes, selected tool socket/terminal proof, manual compaction, selected summary exchange; includes BOTH actual native RPC success/provider-error fixture cases. |
| native-digest.log |1pass: separately supplies current prepared package for the previously skipped real compiled native request-digest comparison. |
| usage-owner.log |34pass: usage/context/billing/compaction cases after removing the final usage forwarder. |
| selected-repair.log + manual-selected-repair.log |58pass2skip and93pass2skip: strict selected prepare/settings/summary correlation, malformed replies/diagnostics, FieldCodec and manual readers. Optional native cases subsequently pass in current-main-native.log. |
| final-proof-boundary.log |55pass1skip: missing text/delta rejection, boolean authority rejection, opaque unknowns, native admission and selected tools/diff. Digest skip subsequently resolved above. |

Actual native fixture package reused read-only:
`/home/ts/wt/comms-refactor-integration-20260928/stack/.pi-native-aef88838db0496c2/node_modules/@earendil-works/pi-coding-agent`.
`stack/test-native-selected-compaction-summary.mjs` blocks network globally, uses fake SDK event streams and the actual compiled RpcMode. Success preserves source session bytes and reservation; error retains402 detail and UNKNOWN/no admission. This is actual local protocol acceptance, not paid-provider or installed-production acceptance.

MCP tests reused an existing node_modules via an owned temporary symlink; no duplicate install. Failed initial receipts retain the old launcher/package mismatch (captureCompactionWitness absent), invalid base64 fixture, missing manual import and the queued test accidentally invoking plain-output mode. These were repaired; native-fixture-diagnostic.log is historical, not an outstanding source failure.

### Reproduction

```sh
PYTHONPATH=src timeout 90 /home/ts/wt/comms-refactor-integration-20260927/.venv/bin/python -m pytest -o addopts='' tests/test_pi_payloads.py tests/test_backend.py tests/test_backend_settlement.py tests/test_pi_rpc_nominal.py tests/test_channel_coding_tools.py tests/test_tool_diffs.py tests/test_acp.py tests/test_mcp_relay.py -q
PI_NATIVE_PACKAGE_DIR=/home/ts/wt/comms-refactor-integration-20260928/stack/.pi-native-aef88838db0496c2/node_modules/@earendil-works/pi-coding-agent PYTHONPATH=src timeout 60 /home/ts/wt/comms-refactor-integration-20260927/.venv/bin/python -m pytest -o addopts='' tests/test_selected_summary_exchange.py tests/test_native_prompt_binding.py tests/test_selected_tool_native_fake.py tests/test_manual_compaction.py -q
AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE=/home/ts/wt/comms-refactor-integration-20260928/stack/.pi-native-aef88838db0496c2/node_modules/@earendil-works/pi-coding-agent PYTHONPATH=src timeout 20 /home/ts/wt/comms-refactor-integration-20260927/.venv/bin/python -m pytest -o addopts='' tests/test_native_prompt_binding.py::test_native_request_digest_matches_real_pinned_module -q
```

## NRA coverage and exact successful invocation

`nra-current-main.json`: exact_compact_global,79detectors analyzed,0omitted,complete,0findings. This is coverage evidence, not semantic equivalence or blanket completion. Authored semantic boundary patches are not claimed as an NRA replay proof. Final narrow guard/usage and fixture repairs are covered by their local receipts.

```sh
cd /home/ts/wt/comms-refactor-r1-pi-payloads-20260928
timeout 165 /home/ts/code/projects/nominal-refactor-advisor/.venv/bin/python -m nominal_refactor_advisor src/agent_comms/pi_events.py src/agent_comms/pi_commands.py src/agent_comms/pi_payloads.py src/agent_comms/pi_summary_payloads.py src/agent_comms/pi_rpc.py src/agent_comms/turn_usage.py --context-root src/agent_comms --parse-workers 1 --analysis-workers 1 --no-cache --scan-budget-seconds 140 --json --json-payload loop
```

Retain all failed receipts. Own disposable transformation scripts, exited Python/test caches and dependency symlink are cleaned at handoff; no worktrees, shared package environments or other agents' artifacts removed. R3 remains Darwin's; R6/R7 remain queued in the original audit. Parent owns serial rollout and any paired saved-history consumer changes.
