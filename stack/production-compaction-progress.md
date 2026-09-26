# Production compaction successor

Base: merged PR48 `d0ced47fbec39ac4d110c9f7395442524363871d`.
This is **not deployment approval**. The adaptive trigger remains disabled.

## Executable authority/lifetime foundation

- `ThreadRegistry.guard_owner_compaction` uses the existing attestation
  checks, but retains the registry lock throughout the caller's critical
  section. Existing `attest_owner_compaction` now delegates to it and still
  returns only a point-in-time audit snapshot.
- Registry lock order is registry → native session writer. Registry methods
  cannot be called recursively inside the guard.
- `_store_lock` yields its descriptor. POSIX release is by **last close**, not
  explicit `LOCK_UN`, so an inherited native child's descriptor keeps registry
  writers excluded even if Python dies or unexpectedly unwinds.
- Internal `run_authority_child` is a single-attempt, maximum-30-second POSIX
  transport. It inherits that FD, kills/reaps the direct child on timeout or
  cancellation, and never retries. Its command must be a trusted, non-forking
  helper retaining the FD until exit. No runtime caller exists yet; an FD
  number or JSON receipt alone is not an authenticated native protocol.
- Windows inherited authority is deliberately unsupported/fail-closed in the
  transport; ordinary registry locks and audit attestations remain portable.

Provider-free tests exercise real competing stop/heartbeat/goal registry
writers, exception unwind with inherited authority, positive child completion,
transport timeout, KeyboardInterrupt, and parent SIGKILL followed by child
mutation while authority is still held. These tests use Python children, not
native Pi. They prove the lifetime primitive, **not native commit integration**.

Reproduction (focused suite does not exercise whole-repo coverage):

```sh
TMPDIR=/var/tmp PYTHONPATH=src env -u AGENT_COMMS_THREAD python -m pytest \
  -n0 --no-cov -q tests/test_owner_compaction_authority.py \
  tests/test_owner_compaction_gate.py tests/test_declarations.py \
  tests/test_concurrency.py
```

Result: **148 passed, 1 skipped** on Linux/Python 3.11.
Unsetting `AGENT_COMMS_THREAD` isolates a legacy test which only clears
`PI_AGENT_ID`; it does not affect the new authority tests.

## Still required before the first complete native commit slice

1. Durable unique-operation intent BEFORE dispatch, outcome handling and
   exact-ID reconciliation under native writer fence. Crash-ambiguous intents
   must prohibit retry; stale lock removal must not resolve them.
2. Trusted native bridge origin, retained FD protocol, native CAS integration,
   session/correction currency, and actual native crash/race tests.
3. Exhaustive conflicting-writer inventory, particularly send/publication
   coupling (the authority lock alone does not make split transactions atomic).

Then canonical all-writer deployment, operator recovery, real adaptive/ACP
integration, and isolated end-to-end tests remain. The independent hard-context
backstop is unchanged. No installed-package edits, provider calls, or live
activation have occurred. Independent review is mandatory before merge.
