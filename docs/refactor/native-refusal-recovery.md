# Native refusal recovery (#752 takeover)

Integration owner: core-retry. Worktree: `/home/ts/wt/core-retry`.

## Owner and consumers

- `NativeTurnRun` owns committed-run identity and monotonic production. `RecoveryBudget` is the sole retry policy (three retries; 1s, 10s, 60s backoff; 120s empty wait). `NothingProduced.finished` decodes explicit provider refusal once, announces before refused message delivery, and delegates recovery to its named state. `BackoffNothingProduced.recover` increments the attempt once and removes only the exact refused assistant from model context. Original input/journal records are never resent or removed.
- `NativeRetryStart(AutoRetryStart)` and `NativeRetryEnd(AutoRetryEnd)` enter the existing declaration-derived Pi event codec and phase observation. Core never interprets provider text or counts retries. Native end closes the report window without a synthetic second Error; the final retained assistant is the failure source. Native recovery reports recovered only on its end event, not on the next assistant-start preceding another possible refusal. Real output closes recovery before a later possible terminal error, so it cannot hide that error.
- `ProviderRetryPhase` owns whether refusal reporting is held; `ErrorStopReason.apply` uses it. Its normal excursion lifecycle still owns entry, progress and exit.
- `TurnEventPublication` owns the actual publication receipt, scoped to one turn. `TurnProgress.publish_result` decides request failure from the terminal outcome and that receipt. Deleted the session-keyed `TurnRunner.emitted_errors` store, its UI writer in `AcpEventConsumer.on_error`, and both `OwnedTurn` outcome checks. Stream fault reporting remains distinct and does not catch/re-publish a completed failed result.
- Toad's existing `RequestAgentFail.show_native` (`src/toad/core/events.py`) consumes `NativeMessageFailureSource` by requiring the original transcript checkpoint, not mounting a copied tail error. Core's final Error retains this source. Toad does not own retry policy. No Toad source change is needed for these events.

## Runtime delivery

`stack/bin/prepare-pi-native` applies the existing base patches and calls `patch-native-turn-context.py`. That patch copies `native-turn-recovery.mjs` to `dist/core/turn-recovery.js`, copies its matching `.d.ts`, wires `AgentSession`'s original run and stream hooks, and declares `_nativeRun` and native retry event kinds in `agent-session.d.ts`. The package's full tree commitment covers new files as well as listed files. Build only from this owned worktree's stack; do not pip-install into the live runtime.

Checkpoint pin after normal integration of main: `a1bb81cd55b1d291cfccf74a3def70b2d62a33038679c09a0fb31615ac0f86ec`.
Build package: `stack/.pi-native-57bd231b778a6f9e/node_modules/@earendil-works/pi-coding-agent`.
Main's checkpoint extension source had changed without its declared snapshot hash/size; updated those derived facts to the actual main source, then rebuilt normally. No installed extension was changed.
`dist/core/agent-session.js`: `efcc532e9875573a8fcfcc9670ace78528cd421feca62f0736d5feb665581e8f`.

## Source evidence and limits

Read the native run/patch, Pi event codec/family, stop vocabulary, phase/watchdog, turn progress/publication, goal account, ACP notification consumer and Toad native-source consumer. Ran refactor-audit overlay across `src/agent_comms` before and after; no parse omissions reported. The first NRA scan hit its 20-second default budget during semantic-descent cache loading; a second interpreter lacked tree-sitter. The subsequent bounded NRA scan with the original tooling interpreter completed in 17.23s with 305 source-index files and 12 findings, using the three changed owner modules and `src/agent_comms` as dependency context. No zero-findings or behavioral-proof claim is made. Dynamic native emit callbacks and subscription dispatch were traced manually. Native JavaScript/TypeScript and Toad were read semantically, not included in that Python AST scan; that is an explicit coverage limit. The JavaScript external event taxonomy remains an upstream protocol boundary, not a new Core string dispatch.

Actual searches:

```
rg -n 'emitted_errors' src/agent_comms     # no matches after deletion
rg -n 'native_retry_start' stack/native-turn-recovery.mjs
# one emission, in RefusedNothingProduced.announce; no second Backoff emission
rg -n '_nativeRunHadTrackedInput' <built-package>/dist/core/agent-session.{js,d.ts}
# no matches
rg -n '_nativeRun:|native_retry_start|native_retry_end' <built-package>/dist/core/agent-session.d.ts
# declaration field plus start/end union facts
```

## Verification checkpoint

- Own native build and pinned-package validation passed.
- Focused recorded-protocol tests: **2 passed** initially; both passed again in the final three-test batch (202.20s overall: **2 passed, 1 failed**), under `taskset -c 4-9 nice -n 10`, Python 3.14.2, `PYTHONPATH=src`, own package in `PI_COMPACTION_TEST_PACKAGE`. Removed inherited AGENT_COMMS variables and PI agent/session identity only in test subprocesses; original sessions untouched.
- Success exercises three native retry announcements, refused messages, final answer, actual Core child pipes/codec/ACP delivery/goal-origin settlement: one answer, no request-failed update, goal remains active. Exhaustion exercises one final Error with native provenance and one failed Done.
- Actual verified native CLI, local HTTP, Core child custody and saved history: seeded a fresh isolated session, sent a new input, received three empty 503 refusals with unmodified 1s/10s/60s waits, then one successful answer. Three native retry events, no Core Error, saved journal prefix preserved, one user commit for that input. This path is demonstrated, not inferred from fixtures.
- The continuous run then sent a distinct new input and proved the real 120s silent-wait abort and native re-request. It did NOT finish successfully: the HTTP fixture selects the next reply at TCP connection acceptance, so a native preconnected replacement socket consumed the success reply before the actual request. The retained journal contains the original silent abort and final Connection error. This is a concrete fixture custody defect owned by core-retry, not evidence of accepted silent recovery. Repair needs request-based response selection in the existing `LoopbackProvider`, then a fresh isolated run; never replay this input.
- Both attempted fixture-repair edits returned `Tool admission outcome UNKNOWN; inspect before retrying`. Inspection shows neither edit in either file. The uncertain admissions are preserved; no edit/tool replay was performed. Need codex-shepherd's explicit disposition before repeating those operations. Stop at draft; no live relink, installed Toad UI acceptance or merge claim.
- #772's separate drain-readiness correction remains with Lead in `activity.py`/`input_drain.py`. This branch adds no readiness policy and makes no changes to that boundary beyond integrating main.

Scratch owner core-retry: `/home/ts/.cache/agent-scratch/core-retry` (AST outputs); `.test-artifacts/core-retry-final` retains the unresolved HTTP fixture run and its isolated native journal/proof unchanged. Native build `57bd231b778a6f9e` remains needed for acceptance. Children/server were closed in the test finally path; no matching native CLI/pytest remains. Earlier AST outputs/builds are superseded and can be retired after borrower checks. No live input was replayed; blocked goals were not changed.
