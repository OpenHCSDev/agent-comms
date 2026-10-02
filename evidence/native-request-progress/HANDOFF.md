# #454 adapter contribution — integration owner Arendt

Base #453 `60ecf89f`, reviewed native761; #453 remains frozen. This source contribution is tracked by existing #454, not an independent release.

## Integration

- Normal-integrate these exact source changes into #454. Arendt already owns `native-request-observation.mjs/.d.ts`; retain his current helper changes plus **only** this `observeStream` extension (no overwritten clock/session/diagnostic changes).
- Builder applies `native-request-progress-adapters.patch` **after** #453 generation policy patch and original #454 observation helper copy. The helper source copy must include `observeStream`; budget source copy includes `send(attempt)` forwarding.
- Arendt owns original provider-retry callback attempt and observer extension/declaration. Producer callbacks and Google normalization forward that argument and the existing `onRequestProgress`. This contribution does not change retry limits, delays or ownership.
- Build/pin a fresh immutable combined native artifact through original recipe; matching Core wheel carries new manifest. Native761 and the accepted #453 wheel remain unchanged. No live/default/root/config actions here.

## Proof and limits

`local-transport-receipt.json`: real OpenAI/Anthropic HTTP and Codex WebSocket provider adapters, seven bounded controls; awaited response callback120ms separated from provider headers, original attempt0/1, budget negotiation0/0 with same messages, throwing observer unchanged, accepted cancellation aborted, iterator early-return release. Zero paid calls; this does not prove installed ACP/native full workflow or attribution of the historical129.898s gap. Arendt owns that #454 installed integration gate.

`production-diff.json`: per-module hashes of the unverified producer control snapshot, zero-fuzz checked patch and changed-JS syntax. This is **not** a promoted native manifest/fulltrust receipt. Ten exported native stream adapters, not an invented eleven-API installed proof. Google SDK raw headers are unavailable; WebSocket connected is not HTTP headers. Bedrock dispatch precedes SDK send, whose internal socket attempts remain SDK-owned/unobserved.

Stream end means original iterator consumption/custody closed, including abort/failure; no success, native commit or delivered-input assertion. Headers are recorded before awaited onResponse. First event is raw parsed provider event before visible-text/message projection.

## Ownership/deletion

Patterns IMPL-12/IDEN-7: reuse original request observation and stream custody resources, not repeated first-event state or an extra status/timing store. External transport fields are declaration-local adapter points, no dispatcher/catalog. Adapter patch deletes25 lines/adds96; budget forwarding deletes2/adds2. Existing Arendt helper is copied unchanged except one iterator extension and its declaration. Public durable schemas/routes/config unchanged.

Persistent control scratch owner Mendel: `/home/ts/.cache/agent-scratch/comms-native-request-progress-adapters-20260930`; provider-module before/candidate copies plus raw receipt and patch log, immutable dependencies referenced read-only. Local HTTP/WebSocket server resources closed on success/failure; no workers or native owners launched. Source #453761, stable761, original native journals/UNKNOWN and prior proof roots protected.
