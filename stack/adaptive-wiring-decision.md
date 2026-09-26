# PR48 adaptive wiring decision (blocker 5)

Status: DECISION RECORD, local-only. No runtime, ACP, or installed-package
changes; no provider calls; nothing pushed.

## Decision

**Do NOT wire the adaptive trigger into the runtime yet.** The native
writer-CAS patch (`stack/patch-native-session-writer-prototype.py
--production`, patched SHA `536f29b6…`, see
`stack/patch-native-session-writer-production.sha256`) is production-quality
for the *writer boundary* but the adaptive path stays dormant until every
gate below is live.

## Production patch path

- `python3 stack/patch-native-session-writer-prototype.py --production
  <disposable>/dist/core/session-manager.js` applies the same CAS/lock
  transformations and **strips the `PR48_PROBE_FAIL_AFTER_WRITE` fault
  hook**; expected result SHA is pinned in
  `stack/patch-native-session-writer-production.sha256`.
- The probe matrix runs against the production patch with `--production`
  (all cases except the injected unknown-write case, which is unprovable
  without the hook and remains covered only on the prototype patch).
- Integration into `stack/bin/prepare-pi-native` is deliberately **deferred**
  to a separate reviewed change; this worktree never touches the installed
  package.

## Preconditions before any adaptive wiring (all mandatory)

1. **Owner bridge live**: `ThreadRegistry.attest_owner_compaction` (blocker 1)
   must run inside the *owner process* at commit time, not be self-issued.
   The current bridge passes a JSON receipt over a pipe; production needs the
   same-process or same-lock handshake so the receipt cannot be replayed
   after the owner stops.
2. **Writer coordination live**: the runtime process must be the canonical
   single writer per session (per `stack/native-writer-policy.md`); unpatched
   Pi writers must be impossible in the deployed configuration.
3. **Send/commit coordination**: an owner-attested compaction commit must be
   coupled atomically with its bus publication; unknown-outcome commits must
   have a defined reconciliation, never a retry.
4. **Operator recovery runbook**: stale-lock recovery (blocker 4) documented
   for operators; no automation beyond the explicit unlink step.

## Hard backstop

`_installNativeCompactionBeforeProvider` and `_runAutoCompaction("threshold",
false)` stay byte-for-byte independent of adaptive eligibility. Missing,
stale, or malformed adaptive evidence can only ever *decline* an adaptive
compaction; the hard threshold path must never consult it. This is preserved
by the patch (no changes to those code paths) and re-proven by
`stack/test-native-auto-compaction.mjs` against the patched copy.

## Attestation reach boundary

The JSON attestation consumed by the disposable JS bridge is **pre-handoff
evidence only**: Python issues it under the registry lock, but JS cannot
recheck the registry after the pipe handoff, so the receipt must never be
treated as commit-time proof. The future commit-time owner check lives in
the **Python owner process**, immediately before (and ideally inside the
same critical section as) the native commit call — a re-run of
`ThreadRegistry.attest_owner_compaction` under `_store_lock` adjacent to the
writer-locked `appendCompactionIfCurrent`, with no replayable bearer token
in between. Until that same-process/locked handshake exists, owner-scoped
commits must continue to fail closed.
