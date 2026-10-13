# Native provider-refusal recovery (#752 takeover, #774)

Integration owner: core-retry. Worktree: `/home/ts/wt/core-retry`.

## Shipping scope and owners

The 120-second empty-wait abort and `WaitingNothingProduced` are deleted. Silence is not evidence of provider refusal. Any future empty-wait change needs measured time-to-first-event from saved diagnostics and its own review; it is not a dependency for this refusal fix. No old failed/UNKNOWN input is replayed.

`NativeTurnRun` owns whole-run production and tracked request continuation. `RecoveryBudget` declares three retries with 1s/10s/60s backoff. Any text/thinking delta or tool start prevents further recovery. A retry removes only the exact failed assistant from model context; the saved assistant and original user commit remain. One `finishRetry` emission reports completed re-request count from the request index. Cancellation of the first backoff therefore reports zero actual retries. Core observes that count; it never increments or infers it.

Tracked and untracked runs call `AgentSession._isRetryableError`, which excludes context overflow and invokes the existing `pi-ai/compat.isRetryableAssistantError` implementation in `pi-ai/dist/utils/retry.js`. The tracked `ProviderRefusal` sentence list is deleted. The shared classifier gains 529 among transient statuses and 402 among non-retryable responses; its existing quota/billing exclusions take precedence over transient patterns. Native retry eligibility still requires a failed assistant with no production or tool start. This is continuation of the committed native run, not a new journal input or replay of uncertain tool/execution operations.

`ProviderRetryPhase` owns the interim reporting window. `NativeRetryStart/End` enter Core's existing declaration-derived event family; their `.d.ts` declarations travel in the same package. End exhaustion returns an empty tuple, not a dummy async generator. The final retained assistant supplies the single failure source.

The completed `Done.outcome` resolves one `FailedNativeTurnOutcome` from the terminal, original input document and raw native provenance. The outcome **contains** a `RequestFailure`; it no longer inherits the wire record. The record owns the reported failure and whether notification was published, while the outcome owns source, failed-input notice and publishing/terminal behavior. Notification and request failure consume the same resolved object. The external `agentCommsFailure` record shape is unchanged except for optional native retry information on the failure. `PromptFailureReceipt`, `failure_receipt`, the publication companion's store, its duplicate classification/read, the notification consumer's duplicate classification/read, and terminal fallback computation are deleted from this family. Transport faults retain an already resolved failure rather than rebuilding it.

`ProviderRetryResult` declares retry-end fields and presentation once. `AutoRetryEnd` composes that capability; the completion passes its redacted native observation to the resolved failure. All failure actions use its attempt/finalError summary plus their specific next step. Hardcoded not-retried claims are deleted from failure and input-cancellation feedback. Quota display no longer regex-classifies rate limits or too-many-requests as quota. Display categories grant no retry authority.

Toad's existing `RequestAgentFail.show_native` consumes `NativeMessageFailureSource` by requiring the original transcript checkpoint instead of mounting a copied tail error. The class rename also requires `src/toad/acp/agent_controller.py` to use `RequestFailure.from_error`. Existing Toad owner `toad-ui-2` has that concrete paired migration; Core and Toad must be installed together without an alias. No live relink has been performed by this author. Lead retains #772/#779 readiness and #780 settlement work; no competing readiness policy is introduced.

## Boundary evidence and new-case cost

Pi's `AssistantMessage` declares `stopReason` and `errorMessage`, but no structured HTTP status/error-code field. Actual retained local 503 failures have role/content/api/provider/model/usage/stopReason/errorMessage/timestamp (and native IDs); the numeric status and JSON error body are inside errorMessage. Thus both native run kinds reuse Pi's existing text classifier rather than claiming structured status is available.

Classifier probes against the built shared implementation: 429 rate limit true; 529 overloaded true; the actual local 503 overload format true; 402 provider-returned-error/insufficient-credits false; 429 insufficient_quota false. These are controlled probes, not claims that today's real provider returned every listed status.

New retryable provider case: before, a genuinely new signature could require **two** edits (SDK classifier and tracked sentence list); after, **one** shared classifier edit. A provider using already classified status/wording costs **zero** implementation edits and no Core/UI retry decision.

Actual searches:

```
rg -n 'ProviderRefusal|WaitingNothingProduced|emptyWaitMs' stack/native-turn-recovery.mjs
# no matches
rg -n 'native_retry_end' stack/native-turn-recovery.mjs
# one emission, NativeTurnRun.finishRetry; actual count is state.attempt - 1
rg -n 'ACPFailure.from_error' src/agent_comms/{agent_event_updates,turn_progress}.py
# no matches; FailedNativeTurnOutcome.resolve resolves once
rg -n 'dispositions.read' src/agent_comms/{agent_event_updates,turn_progress}.py
# only the existing completion snapshot
rg -n 'not retried|was retried' src/agent_comms/{acp_failure,input_attempt}.py
# no hardcoded retry claims
rg -n 'emitted_errors' src/agent_comms
# no matches
```

NRA before/after covers the changed Python owners with the entire Core package as dependency context. Native TypeScript compiler AST queries enumerate declarations, methods, imports, checks, writes, callback/new-expression consumers across published-before recovery source, working-after source, AgentSession and the SDK classifier: 95/83/332/27 sites, no parse diagnostics. Native before evidence was reconstructed from the published checkout after the edit, rather than represented as a pre-edit scan. The owning relationships were read before implementation. Dynamic subscription/extension dispatch is manually resolved; AST is evidence, not behavioral proof. Toad's consumer migration and its acceptance remain with the existing Toad owner.

## Build and acceptance

Built only with this checkout's `stack/bin/prepare-pi-native --update-pins`. Dependencies/resources were reused and 138 MB shared, not installed into the live runtime. Matching JS/d.ts and shared SDK classifier are covered by the full-tree pin.

- Build ID: `80e4e27498c683bc`.
- Package: `/home/ts/wt/core-retry/stack/.pi-native-80e4e27498c683bc/node_modules/@earendil-works/pi-coding-agent`.
- Full tree: `e9947ed69d0d650a532aedc1050fbb11affdc0877cc56e29d36bcfab089065c3`.
- Pin validation and build syntax checks passed. No .pi/APPEND_SYSTEM.md change remains relative to main.
- Previous ownership checkpoint: four checks passed, including real native/Core/ACP delivery and a notification-transport exception; classification once and a distinct later input succeeded without replay.
- Current source/name/native-pin acceptance is being finalized. Initial focused batch had 29 passes and two fixture failures: the existing HTTP fixture supports error responses only for 400/503, so setting 402 incorrectly served 200. The negative test control now uses its supported 400 response with the shared classifier's explicit insufficient_quota exclusion. This does not repair or repeat either old UNKNOWN fixture edit.

Home headroom/swap remained critical (8 GiB free, 19.2 GiB available RAM, 13.4 GiB swap used). Checks reuse one native package and run bounded local HTTP sessions serially; no fleet, paid provider, scientific dataset or large rebuild is launched. Scratch owner core-retry: `/home/ts/.cache/agent-scratch/core-retry` for AST results, `/home/ts/.cache/agent-scratch/core-retry-native-build` for native preparation (builder retires its run directory), `.test-artifacts/` for small isolated verification roots. Original failed-run evidence remains immutable.

## Retained UNKNOWN attempt: exact cause, no replay

Read-only canonical inspection found old execution `wirev16cd5deafa5c002b84cce678a4e7dfae71eddb06dc69163e346a7684b06262b6b` already failed with `released_native_unknown`; its replay assessment retains replay_safe=0, side_effects_possible=1 and retry_authorized=0. The active pointer belonged to the new authorized instruction. Ledger backend_done/process_dead flags do not independently prove an old physical PID's death; the retained input row has no PID. No input, cursor, diagnostic, slot or child was cleared/restarted by inspection.

Exact cause is confirmed in the retained owner log: I externally released two live file claims in bus row 3477 while `CodingToolOwner` still held their original admissions (claims 3389/3413). Subsequent fixture edits reused those cached admissions and were refused with `Wake claim operation was already consumed` **before file writes, after consumption of the new tool slots**. This was my lifecycle error. The two original consumed slots (`c8b3358a5f283fbe7010b013fcabc78c`, `2781ea34ff640f06da80b7f05bd74667`) and both UNKNOWN results remain untouched. Neither operation/input was replayed. Claim lifecycle stays with the actual live owner, not an external publisher or shell. Explicit disposition is required before repeating either old fixture repair; deleting the silence feature does not dispose of those admissions.
