# PR316 corrected ownership: local R0/T4 passes

Owner: Wegener. Production1efe934a; actual setting-response followthrough9bec4e22. Current main317a1d24d3a merged in7d0576d8; no production overlap. Parent owns merge/install, live untouched.

**This supersedes the earlier review state below.** The independent class-size rule is a local guard, not CI: it now passes with zero increases and no exemption or guard change.

## Complete semantic destinations

- ConfigOption now declares discover as abstract behavior. CatalogConfigOption instances own the entire per-option catalog/cache, credential revision, lock, generation, ACP projection and shared choice validation. Concrete model/thinking declarations inherit that implementation and own discovery/selection/persistence. This replaces ConfigOptions' separate model/thinking caches and cache methods; generation is derived from actual option instances. No second hand-maintained option roster.
- CatalogQuery is a PiCommand subclass that owns the complete read-only native-query lifecycle: managed launch, one write, existing pending correlation, response interpretation, stderr drainage and child retirement. Existing GetAvailableModels/GetAvailableThinkingLevels inherit it. PiRpcChannel.request is deleted and the channel returns exactly to its pre316 framing/correlation responsibility.
- SettingCommand owns the result class and shared response interpretation of existing SetModel/SetThinkingLevel. Deleted duplicate callbacks and the option consumer's separately paired result type. Query/setting abstract bases are excluded from wire family membership; concrete command names and payloads stay native-owned.
- Deleted the displaced Model projection entirely: options use existing ACP SessionConfigSelectOption. Deleted backend discovery functions, ConfigOptions.discover/models_for/thinking_levels_for, root model/thinking cache fields and stale caller mock. All current callers changed, no aliases or forwarding entry points.

Production versus0931c47d:235added/243deleted, net-8. backend1064→961. Tests314added/50deleted: actual catalog/settings acceptance, shared protocol new-case and current caller coverage replace unsupported discovery and duplicate option implementation. No source-format migration or reset.

## Local guard and actual path

Packaged debt_ratchet --root src/agent_comms --base0931c47d --head1efe934a: **zero increases**. Existing class spans: ConfigOption-14, ModelConfigOption-3, ThinkingLevelConfigOption-6, ConfigOptions-16, GetAvailableModels-1, GetAvailableThinkingLevels-1, SetModel-8, SetThinkingLevel-8; PiRpcChannel unchanged. New semantic owners are measured as new declarations, not fabricated zero baselines. Full measurement retained locally; ratchet-correction-summary.json committed.

**68 distinct passing checks, four actual pinned native cases**, zero provider prompts:

- owned-catalog.log:60pass25.21s. All three actual native catalog/auth/large-response/cancellation cases rerun on corrected ownership; auth/model caches remain selected correctly, all children retire and saved input stays unchanged. Also exercises new query subclass over recorded TCP plus existing RPC/config/auth/new-option family contracts.
- native-settings.log:2pass3.14s, one is a repeated guard. New actual Pi case: set_model success, set_thinking_level success, set_model refusal; real decoded replies traverse SettingCommand to existing result events with exact request IDs and native refusal reason. No provider prompt. This is native response-to-result acceptance, not a claim of a live in-flight user model change.
- acp-settings.log:3pass1.25s. Existing ACP persisted/selectable configuration, unknown-model rejection, and active backend confirmation/rejection checks. These use recorded confirmation events; complemented by actual native case above.
- turn-caller.log:4pass1.83s, the four assigned baseline cases deselected. Removed obsolete discovery mock; non-failing orchestration callers still work.
- Changed-file Ruff and git diff --check pass. Parent retains combined installed acceptance. No full-suite-green or deployed claim.

## Four baseline failures have a concrete fix assignment

Assigned to **Dalton**, original-scope/PR319: [assignment with exact cases and reproduction](https://github.com/OpenHCSDev/agent-comms/pull/319#issuecomment-5882174054). Acceptance is code-bearing current-native preparation/ACP error-feedback coverage, exact receipt, no weakened assertions/compatibility. Three feedback variants must preserve exactly-once error delivery; compaction case must preserve original-not-sent/provenance with actual existing saved history.

Acknowledgment is **not yet received**. This sidecar exposes no direct Codex agent messaging tool; live bus lookup has no Dalton participant. Parent was asked to relay to Dalton thread01a0e9a0-1787-7860-b9b8-ef08bb2368fb. Do not interpret the GitHub assignment as verified active work. The four bodies and owned_turn/turn_runner production remain exclusively Dalton's to prevent duplicates.

## Current NRA and cleanup

Used /usr/bin/python importing installed NRA mainab85aa0b, with the complete core and explicit src context, one parse/analysis worker. Baseline0931c47d and corrected production both233 source files and112 raw findings:56 redundant_type_check,33 unmodeled_record_shape,22 semantic mirrors,1 repeated builder. The changed boundary retains only existing backend._tool_title raw-record lead (tool presentation remains separate) and unchanged presentation.py family evidence mentioning ModelConfigOption. No new raw lead in query/catalog ownership.

Initial default20s runs reported deadline_exceeded and are retained as failed coverage attempts. Explicit150s bounded scans finished in23.164s/17.714s. Successful full/raw CLI omits scan_status/analyzed/omitted counts; no invented full-detector coverage or global zero-debt claim. Summary and failed statuses committed, full raw graphs retained locally. Authored semantic refactor, not NRA-equivalence proof. This replaces the earlier scan with the older NRA installation; its21-findings receipt below remains historical only.

Owned fixture/source-cache copies were removed after all commands and native children finished; r0-cleanup.json records bytes. Source, branch, canonical5fde native package, raw audits and all passing/failing receipts retained. No shared checkout or live process changes.

---

# Historical first316 checkpoint (superseded)

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
