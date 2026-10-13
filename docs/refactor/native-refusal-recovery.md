# Native refusal recovery (#752 takeover)

Integration owner: core-retry. Worktree: `/home/ts/wt/core-retry`.

## Owner and consumers

- `NativeTurnRun` owns committed-run identity and monotonic production. `RecoveryBudget` is the sole retry policy (three retries; 1s, 10s, 60s backoff; 120s empty wait). `NothingProduced.finished` decodes explicit provider refusal once, announces before refused message delivery, and delegates recovery to its named state. `BackoffNothingProduced.recover` increments the attempt once and removes only the exact refused assistant from model context. Original input/journal records are never resent or removed.
- `NativeRetryStart(AutoRetryStart)` and `NativeRetryEnd(AutoRetryEnd)` enter the existing declaration-derived Pi event codec and phase observation. Core never interprets provider text or counts retries. Native end closes the report window without a synthetic second Error; the final retained assistant is the failure source. Real output closes recovery before a later possible terminal error, so it cannot hide that error.
- `ProviderRetryPhase` owns whether refusal reporting is held; `ErrorStopReason.apply` uses it. Its normal excursion lifecycle still owns entry, progress and exit.
- `TurnEventPublication` owns the actual publication receipt, scoped to one turn. `TurnProgress.publish_result` decides request failure from the terminal outcome and that receipt. Deleted the session-keyed `TurnRunner.emitted_errors` store, its UI writer in `AcpEventConsumer.on_error`, and both `OwnedTurn` outcome checks. Stream fault reporting remains distinct and does not catch/re-publish a completed failed result.
- Toad's existing `RequestAgentFail.show_native` (`src/toad/core/events.py`) consumes `NativeMessageFailureSource` by requiring the original transcript checkpoint, not mounting a copied tail error. Core's final Error retains this source. Toad does not own retry policy. No Toad source change is needed for these events.

## Runtime delivery

`stack/bin/prepare-pi-native` applies the existing base patches and calls `patch-native-turn-context.py`. That patch copies `native-turn-recovery.mjs` to `dist/core/turn-recovery.js`, copies its matching `.d.ts`, wires `AgentSession`'s original run and stream hooks, and declares `_nativeRun` and native retry event kinds in `agent-session.d.ts`. The package's full tree commitment covers new files as well as listed files. Build only from this owned worktree's stack; do not pip-install into the live runtime.

Checkpoint pin: `94dc7802f9ad857d6718b4caae9874f3779c6e3b0126878827390bcdfa32ddf7`.
`dist/core/agent-session.js`: `efcc532e9875573a8fcfcc9670ace78528cd421feca62f0736d5feb665581e8f`.

## Source evidence and limits

Read the native run/patch, Pi event codec/family, stop vocabulary, phase/watchdog, turn progress/publication, goal account, ACP notification consumer and Toad native-source consumer. Ran refactor-audit overlay across `src/agent_comms` before and after; no parse omissions reported. The NRA full scan hit its 20-second default budget during semantic-descent cache loading; it is explicitly incomplete, not proof of ownership. A second interpreter lacked tree-sitter; no zero-findings claim is made. Dynamic native emit callbacks and subscription dispatch were traced manually. The JavaScript external event taxonomy remains an upstream protocol boundary, not a new Core string dispatch.

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
- Focused recorded-protocol tests: **2 passed** (5.87s), under `taskset -c 4-9 nice -n 10`, Python 3.14.2, `PYTHONPATH=src`, own package in `PI_COMPACTION_TEST_PACKAGE`. Removed inherited AGENT_COMMS variables and PI agent/session identity only in test subprocesses; original sessions untouched.
- Success exercises three native retry announcements, refused messages, final answer, actual Core child pipes/codec/ACP delivery/goal-origin settlement: one answer, no request-failed update, goal remains active. Exhaustion exercises one final Error with native provenance and one failed Done.
- Recorded protocol is not real provider recovery. Real HTTP empty refusal and silent-wait recovery, retained saved history and installed user entrypoint acceptance remain to be completed before readiness. Stop at draft; no merge/install claim.

Scratch owner core-retry: `/home/ts/.cache/agent-scratch/core-retry` (small AST outputs); `.test-artifacts/core-retry` (disposable isolated test roots). Native build checkouts under this worktree remain needed for acceptance. No live input was replayed; blocked goals were not changed.
