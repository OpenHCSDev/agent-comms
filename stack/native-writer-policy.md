# Native session writer policy (PR48 blockers 3 & 4)

Status: LOCAL-ONLY design record for the disposable prototype
(`stack/patch-native-session-writer-prototype.py`, patched disposable SHA
`d9f0e3b3e6ff8975a13d6a5f07cb88b13e399003d8725c409829edd4425afbe6`).
Nothing here is wired into the installed Pi package, ACP, or PR48 runtime.

## Blocker 3 — single-writer contract

The prototype routes **every** `SessionManager` mutation path through one
canonical-path cross-process lock per session file:

- `_appendEntry` (all appends: messages, model changes, compaction, branch
  summaries), `flushInputDurably`, `_rewriteFile`, and the guarded
  `appendCompactionIfCurrent` all execute inside `pr48WriterLock`.
- Under the lock, each mutation rechecks the on-disk revision against the
  revision recorded when this `SessionManager` instance loaded the file
  (`_pr48LoadedRevision`). Any drift → refuse **before** mutation.
- Contract tests: `stack/test-native-writer-exclusivity.mjs` (8/8
  deterministic) proves that four concurrent processes aligned on one loaded
  revision produce exactly one committing writer; every other writer refuses
  with a typed fail-closed error; the file stays valid JSONL with unique IDs
  and intact parent links; nothing is interleaved or torn.

### Caller policy (production shape, not yet wired)

`SessionManager` itself never retries. A caller may classify refusals:

| Refusal | Meaning | Retry policy |
| --- | --- | --- |
| `Native session writer lock unavailable` | Pure pre-write lock contention; no bytes written | Bounded pre-write retry is safe (fixture uses 200 × 2 ms) |
| `Native session writer changed` | Another writer advanced the file after our load | **Never retry blind**; reload the session and re-derive all evidence |
| `Native compaction commit outcome unknown` | Bytes may have reached disk before an error | **Never retry**; treat commit as unknown (blocker 5 handles recovery) |

The live runtime must additionally enforce a *canonical single writer per
session* (one `SessionManager` per session file per process, coordinated
through registry ownership) so bursts do not interleave at message
granularity. Cross-process safety here is fail-closed, not throughput
optimization.

## Blocker 4 — stale-lock policy

- The lock file (`<session>.pr48-writer.lock`) is `wx`-created and always
  unlinked in a `finally`. A crash between create and unlink leaves it.
- There is **no automatic stale-lock stealing**, ever: a writer that finds
  the lock present refuses with `writer lock unavailable`. This is
  fail-closed by design (a live unknown holder is indistinguishable from a
  crashed one without operator knowledge).
- The only sanctioned recovery is an **explicit operator action**: verify the
  holder is gone, delete the lock file, then reload the session and re-derive
  all evidence before the next append. The prototype proves this in the
  `crash-lock` probe case: refusal while the lock persists → operator
  `unlink` → recovered append commits.
- Recovery does not repair session content; it only re-enables the writer.
  Any compaction whose outcome was unknown at crash time stays unknown
  (blocker 5 responsibility).

## Non-goals

No provider calls, no runtime/ACP wiring, no installed-package edits, no
push. The native hard-context backstop (`_runAutoCompaction("threshold",
false)`) remains fully independent of this policy.
