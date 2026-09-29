# Saved native startup: ready-pipe/deadline race

## Scope and state

Zero production lines deleted; seven added to the existing `ProgressWatchdog.read` owner. No new runtime state, timeout, retry, codec, admission route or registry. Original intermittent App-fork trigger remains **unresolved**; this patch fixes an actual independently reproduced failure mechanism consistent with an overloaded owner loop. Parent owns paired installation/App acceptance. Carver owns timeout stderr/typed NotSent presentation.

The native stdin request, correlated `PendingAttestation`, saved identity, retained custody, cancellation/stop and original-input permission remain with their current owners. `IMPL-13`: fix arbitration at the shared watchdog, do not create a second native reader. `BOUND-1`: actual native response still passes the sole typed decode/attestation path. `TIME-9`: no wrapper or alternate format.

## Actual evidence

- Parent original RED: same configured SolHigh saved private fork, 41,333,135 bytes, native preflight13.001s, durable NotSent, no original replay. Original diagnostics cannot establish native stall versus owner-loop starvation.
- `configured-preparation.json`: three actual managed cold preparations, canonical credentials/config copied privately, same saved source/model/thinking/project. PASS1.68–1.76s; no prompts/provider calls; loop gaps <=51ms. Disposable fork/config removed; original source stat unchanged.
- `loop-starvation-pipe-red.json`: unchanged watchdog; deliberately block only Python owner loop14s. Independent thread observes actual native stdout ready1.639s. Native startup timings590ms excluding imports. Watchdog still destroys child as preflight timeout13s before reading response. FAIL preserved, no prompt.
- `loop-starvation-pipe-candidate.json`: exact same real saved-fork experiment, unchanged13s budget. Native stdout ready1.771s; when owner loop resumes, watchdog drains already-scheduled read continuation and actual identity/capability attests. PASS14.141s overall, no prompt/provider call. Original source stat unchanged.
- `loop-starvation.json`: earlier reproduction without independent readiness observation; preserved as weaker evidence.

## Fix

`asyncio.wait` can resume its timeout waiter before an already-readable pipe's task continuation after event-loop starvation. Before cancelling outstanding reads on an empty `done` set, yield **one scheduling turn** and collect tasks actually completed. This adds no wall-clock wait budget. A silent native child still fails, and all bytes use the existing reader and attestation. No input can start from timeout metrics or pipe-readiness observations.

## Remaining acceptance

Actual pinned local-provider regression: source ready-pipe/silent-child checks **2 passed10.26s**; pending-read starvation **1 passed4.63s**. Noneditable wheel with unchanged production fix: all three affected actual native cases **3 passed14.60s** (`installed-native-regression.log`). Initial test-launch attempts lacked pytest/metaclass-registry and stopped before behavior; logs preserved. Parent final installed App-fork path remains separate. Need identify the original worker's blocking operation, not infer it from this controlled reproduction.

Read-only trace: `TranscriptReplay.replay` already offloads saved-page reading with `asyncio.to_thread`; its typed metadata encoding/socket JSON serialization runs on the owner loop. Normal tail pages are bounded, so this does not establish a41MB synchronous serialization. Continue measuring actual ACP subscription rather than assuming full-history loading blocks.

## Actual concurrent owner subscription

`actual-owner-subscription.json`: real `CommsAgent` owner and Unix socket `SubscribeRuntimeRequest`, simultaneously with actual saved-native preparation on the same Python event loop. PASS5frames/79,674bytes, ready4.899s; native attested1.598s, maximum sampled loop gap97ms. No prompts/provider calls; private config/session/socket cleaned, source stat unchanged. This exercises the actual subscription handler and saved transcript/metadata projection but **not** the separate installed Toad App or a fresh worker entrypoint. Parent owns that final path. It did not reproduce the original intermittency.

## Delivery

PR359. Configured real history remains only in the original source and temporary diagnostic forks, all diagnostic forks removed after their runs. Source tests seeded new small histories using loopback responses. Global immutable d396 package untouched. Carver directly notified about preserving captured stderr on timeout; his presentation files were not edited here. Initial interpreter/setup failures remain failed receipts, not counted as tests.
