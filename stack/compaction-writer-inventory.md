# Compaction conflict inventory (production successor, not activation approval)

## Registry authority

The complete `ThreadRegistry._save_unlocked()` call inventory at this checkpoint:

| Writer | Serial boundary | Relevant invalidation |
| --- | --- | --- |
| `register` | registry `_store_lock` | goal/status/metadata/epoch replacement; admission when ownership changes |
| `_claim_turn_unlocked` | callers `claim_local_turn`, `claim_live_turn_with_epoch`, `claim_live_turn_with_admission` hold registry lock | active turn and epoch |
| `finish_claimed_turn_with_fence` | registry lock | active turn release and completed-turn fence |
| `rename` | registry lock | canonical identity, aliases, turn/owner metadata |
| `unregister` | registry lock | stopped status and admission/epoch invalidation |
| `archive` | registry lock | presentation/lifecycle state and admission/epoch |
| `begin_delete` / `remove` | registry lock | deletion and ownership removal |
| `heartbeat` | registry lock | last-seen; revival changes admission/epoch |

`Comms.update_goal` ultimately uses registry `register`; it does not own a
separate unguarded registry write. The retained guard reloads and checks full
expected Thread equality plus active status, actual PID, executable role,
claimed turn ID/epoch, and active exact goal ID/revision.

Source-audited direct registry lock boundaries outside the class are
`operations.py` (`mark_dm_view_read`, an identity read held through marker CAS),
`claim_admission.py`, `coordination_response.py`, and private-protocol
initialization in `declarations.py`. This table does not claim those surrounding
multi-store transactions are atomically coupled to compaction merely because
they share a registry lock.

## Other authorities and lock order

There are **two distinct session locks**:

1. `session_fence.session_writer_fence` is a Python executor-lifetime lock.
   `backend.stream_agent_events` holds it BEFORE calling ACP send boundaries.
   Those boundaries acquire wire/registry locks. The external compaction helper
   must therefore acquire the same executor slot FIRST, nonblocking; taking it
   while holding registry authority can deadlock an active backend.
2. The patched native `.pr48-writer.lock` is the per-entry file mutation fence.
   It comes AFTER registry authority in the compaction path.

Current external bridge order: executor slot → wire → bus → registry → input
ledger → native-entry lock. All five Python descriptors are inherited until
native child exit. An idle persistent Pi process is not ruled out by the
executor slot; runtime integration must explicitly close/reopen it around an
external helper commit.

The **ingress boundary** respects existing wire → bus → registry order. Evidence:

- `Comms.send_message` takes wire, then `MessageBus.publish` takes bus and calls
  `_prepare_message_unlocked`, which obtains a registry snapshot.
- Private claim publication and keyed response paths similarly use bus then
  registry, with explicit already-locked snapshot variants.
- ACP `send_boundary` takes wire through final input validation and stdin write;
  `InputDispositions.record`/`bind`/`started` have their own ledger lock.
- Input receipt rows are UNKNOWN until exact native user-start evidence. They
  are not replay requests, and their ledger is not owned by the registry lock.

Therefore **never acquire bus or executor locks from inside the existing
registry guard**. `_boundary` acquires the combined order above. Real-process
raw bus publication, Comms.send, input ledger record, and registry lifecycle
competitors are blocked through native mutation. The real-native SIGKILL
barrier additionally checks that wire, bus, input, registry and executor locks
all remain excluded after parent death until native child exit. Independent
review and full runtime call-graph integration are still required.

## Native writer findings still blocking deployment

These are source-audit findings against the disposable `8ec0b8f1…` artifact.
The successor `patch-native-writer-coverage.py` addresses them in an isolated
`41a94b37…` artifact; this is not deployment or independent-review clearance.
The dormant claim that every native mutation already shares the writer boundary
was insufficient.

| Entry path | Identified gap | Adversarial control |
| --- | --- | --- |
| `loadEntriesFromFile` | Load-time missing-newline repair calls `appendFileSync` outside the native writer lock. | Hold the native lock while loading a missing-newline/torn-tail file; prove no bytes change. Require explicit safe recovery or strict refusal, never silent repair. |
| `SessionManager.forkFrom` | Destination header and copied entries are written directly outside the common writer lock. | Block source/destination writers at deterministic barriers; prove complete durable destination or typed refusal, no partial accepted fork, and unchanged source. |
| `SessionManager` constructor | The patch refreshes `_pr48LoadedRevision` after loading entries. A writer between parse and that stat can bless stale memory with a fresh disk revision. | Interpose a real external append after parsing but before constructor completion; prove refusal or coherent reload, never stale-tree append. Include persisted preloaded-entry input. |
| `setSessionFile` / `_setSessionFile` | The same post-load refresh can bind a stale loaded tree to a newer disk revision. | Repeat the parse/stat race during file switching; ensure loaded revision belongs to the exact parsed snapshot, and stale mutation leaves disk unchanged. |

The first two require mutation coverage beyond `_appendEntry`, `_rewriteFile`,
`flushInputDurably`, and guarded compaction. The latter two require correct
snapshot provenance, not merely a final current stat. The new patch rereads
persisted preloaded arrays, checks the captured revision before indexing and at
constructor/switch completion, and preserves the original empty-file revision
through initialization. It refuses malformed, incomplete, invalid UTF-8, legacy,
and invalid-ancestry input without repair. Legacy migration needs explicit
recovery; it is no longer a load-time side effect.

The audit also found `createBranchedSession` could branch from stale memory or
adopt a destination revision as overwrite permission. It now holds the source
writer lock, validates the loaded snapshot and expects a nonexistent destination.
`forkFrom` holds source and destination locks, uses exclusive creation and syncs
the file and parent ancestry before returning. Partial writes or sync failures
return UNKNOWN, without automatic replay or deletion of their evidence.

`test-native-writer-coverage.mjs` passes **19 isolated controls**, including real
external appends at load barriers, source/destination lock refusal, partial fork
write and directory-sync denial, and positive durable fork/branch cases. Ten
controls first failed on the old artifact with assertions (logs under
`/var/tmp/pr48-writer-old-*.log`). Canonical preparation integration, independent
review, and persistent runtime manager error/reopen semantics remain open.

## Open gates

- Integrate the now-executable `capture_source`/`CompactionSource` API into actual
  summary preparation. It binds canonical native witness, root identity,
  owner/turn/goal and actual bus/input fingerprints; the commit boundary rejects
  drift or current-admission UNKNOWN input before journal/dispatch. The legacy
  receipt correction counter is not evidence. Whole-store revision matching is
  intentionally conservative (unrelated traffic can decline a candidate).
- Couple result publication through a durable keyed outcome/outbox rather than
  replaying native mutation. Ingress exclusion is not publication coupling.
- Independently review native writer coverage and complete-package provenance.
  Canonical preparation now applies the production patch chain and checks the
  complete tree; see `native-package-provenance.md`. Safe rollout must still
  retire old unfenced/idle managers before any adaptive activation.
- Extend deterministic race tests to raw bus sends, ACP queued/steered inputs,
  every lifecycle writer, and idle persistent-session invalidation.

No live activation is safe before these gates and independent review.
