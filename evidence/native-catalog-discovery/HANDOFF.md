# S2 G4/G5 native catalog discovery closure

Owner: Wegener. PR316. Parent owns merge/install; no live changes. Production checkpoint8d997dcc; merged current main0931c47d in1abd07ff with no owned-file overlap.

## Review state

Implementation and actual native acceptance complete. The lexical class-size ratchet reports four increases, explained below; this is NOT a zero-increase ratchet receipt. Four unrelated existing turn-runner fixture failures reproduced on unchanged311 are recorded for Dalton. No CI wait, compatibility restoration or hidden assertion relaxation.

## Ownership and deletion

- Existing ConfigOptions owns bounded catalog subprocess lifetime and existing catalog caches. Its ModelConfigOption/ThinkingLevelConfigOption declarations own selected catalog projection and unavailable results. The existing Model presentation value now lives with configuration, with no backend re-export.
- PiRpcChannel.request writes once, uses existing PendingRequests for command+ID correlation, consumes through the existing fragmented-record decoder, and discards the request on every outcome. No parallel pending map or command roster.
- BoundedRun retires the actual native child on normal response, refusal, cancellation or EOF. Existing AttachedChild.discard_stderr drains without accumulating output; its task is cancelled/joined with the query.
- Deleted backend.discover_models and backend.discover_thinking_levels entirely; deleted both duplicate response-match loops and their oversized per-reader buffer settings. Channel framing accepts complete records across the normal transport buffer; no new hard size cap.
- Migrated all catalog consumers and test hooks. Deleted the obsolete shell-script model-discovery test; actual pinned Pi now proves that behavior.
- No stored schema, durable history, prompt admission, replay, send-boundary, tool presentation or attestation changes. NativeAttestation identity-owner refinement belongs to the next scope that touches it; this slice does not.

Production:107 lines added,115 deleted (net -8). backend.py1064 ->961. Tests:236 added,31 deleted; growth supplies previously absent actual native catalog and resource-lifetime coverage plus one new-command experiment, replacing unsupported fake discovery.

## Executed local evidence

`PI_COMPACTION_TEST_PACKAGE=$PWD/stack/.pi-native-5fdef596596173bd/node_modules/@earendil-works/pi-coding-agent`, `PYTHONPATH=src`, runtime-cursor-recovery Python, pytest `-o addopts=''`; owned basetemps under .artifacts/native-catalog-discovery.

- **corrected.log:9 passed in24.94s.** Native catalog/auth refresh: five actual pinned Pi children, catalog cache hit launches none, changed actual auth/config reread, selected model retained, new reasoning model exposes supported levels. Large actual catalog:71 local models,157691 aggregate identifier bytes including available environment-provider catalogs; response exceeds normal64KiB stream buffer and completes. Cancellation: actual child has received the request, paused read is cancelled and child reaped. Every case preserves fixture saved history and records zero localhost provider requests. Includes six guard/auth/session caller checks beyond the three actual native cases.
- **correlation.log:55 passed in2.38s,4 deselected.** New PiCommand subclass with existing ModelsData traverses TCP/JSON decode and existing pending registry with no production registration edits. Wrong ID, wrong command and unrelated event do not settle it. Matched200KB result, refusal, malformed JSON and EOF each make one request and leave no pending request. These are recorded peer protocol checks, not additional actual Pi cases. Existing Pi RPC and non-failing turn-runner callers also pass.
- Combined:64 distinct passing tests, including three actual native cases. No paid provider calls or installed/live claim. Parent owns combined installed acceptance.
- Ruff on changed source/caller/new tests and git diff --check pass.

### Retained failures, with actual strength

first.log:57 pass,7 fail. Two new native assertions wrongly assumed environment-available providers would be absent; corrected to assert this fixture's local providers without discarding the actual catalog. Old auth fixture lacked required canonical private root/package; migrated to existing canonical_agent and a real temporary project directory. These three exact cases now pass in corrected.log.

Remaining four cases in tests/test_turn_runner.py are unchanged-base failures, recorded for Dalton in PR316 comment:

- test_uncaught_failure_feedback_once_even_after_done[None/error/done]: expected one forwarded error, observed zero. Same three assertions reproduced on source0a1f126d in baseline-failures-corrected.log.
- test_compaction_fault_reaches_acp_client_without_original_send: fixture names nonexistent saved.jsonl; NativeSessionPreparation fails before its mocked adaptive hook. Same failure on source0a1f126d in baseline-compaction.log.

Baseline harness red receipts also retained: baseline-failures.log omitted conftest (four setup errors), initial baseline-failures-corrected.log lacked copied stack manifest for the compaction case. Final baseline-compaction.log supplies actual base resources and reproduces the matching production exception. These harness failures are not acceptance evidence. Baseline source copy was disposable and is removed after checks.

## Audit and ratchet

NRA command: nominal_refactor_advisor src/agent_comms --context-root src --parse-workers1 --analysis-workers1 --scan-budget-seconds150 --json --raw-findings --json-payload full. Actual CLI uses separated option/value tokens. Raw JSON remains in .artifacts/native-catalog-discovery/nra-after.json; summary committed.

Reused prior full-context fc73cb8d baseline: git diff confirmed its production source equals this slice's0a1f126d base. Before21 raw, after21 raw (20 semantic_mirror_without_descent,1 repeated_builder_calls). No new emitted finding at this boundary. Existing presentation.py ThreadView finding references ModelConfigOption as one family authority; its code and finding persist unchanged. No mapping_read, unmodeled_record_shape or redundant_type_check findings emitted. CLI provides no scan_status or analyzed/omitted counts, so these are detector outputs, not a global coverage/zero-debt claim. Authored owner refactor, not NRA replay-proved equivalence. Main312/315 integration was disjoint; this audit measures this slice before that unrelated merge.

Packaged ratchet measured8d997dcc vs0a1f126d: only class spans increase: ModelConfigOption+18, ThinkingLevelConfigOption+9, ConfigOptions+25, PiRpcChannel+23. All other metrics unchanged. The removed procedural loops now belong to existing semantic authorities, as S2/S7 and the current assignment request. Creating artificial capability classes merely to evade this measurement would conflict with the no-facade/no-carving instruction. Kept the honest measured result for parent review, without changing the ratchet or adding exemptions.

## Cleanup

All test children exited and fixture teardown asserts retirement. Owned baseline source copy, fixture trees and NRA cache removed; cleanup.json records bytes. Passing and failed logs, raw audit/ratchet, source, branch and immutable canonical5fde package retained. Parent/shared checkouts and live owners untouched.
