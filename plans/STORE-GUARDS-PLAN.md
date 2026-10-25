# agent-comms: each store owns its guard

Audited head: `389d0b22a` (main, 2026-10-24), recounted at `8bc8f8fef`. Every count below is `git grep -cE PATTERN -- src ':!src/agent_comms/skills/'`; line numbers are at `8bc8f8fef`.

## The thesis

A lock protects a store's writes. Which lock a write needs, whether a read needs one, how long a waiter waits and whether maintenance refuses the write are facts about the store, so each store family declares them once. A call site asks its store to read or write and never names a lock, a mode, a wait budget or a maintenance check. A store type whose writes need no lock has none, and no call site can add one.

Tristan, 2026-10-24: "lock use shouldn't be manually managed ... it should be enforced via polymorphism or a contextmanager or something that's structural, not hand patched"; "there's too much performative safety happening".

## Why this is slop today

The audit classified the 88 store-lock sites by what each lock protects:

| Verdict | Sites | Meaning |
|---|---|---|
| NEEDED | 38 | the lock serializes a read-modify-write of the store it names |
| WIDER | 22 | the lock is a bigger store's lock than the write needs (the registry lock guarding a per-thread turn file) |
| PROTECTS NOTHING | 19 | the guarded operation is already atomic (an atomic rename, a lock-free read) or its store has its own lock |
| DUPLICATE | 9 | a maintenance check written beside a write whose store already refuses during maintenance |

The fact "which lock guards this write" is declared nowhere, so every caller restates it (IMPL-12; AGENT-7 for the 19 that protect nothing):

- **99 lock calls in 42 modules outside `store_files.py` and `locked_store.py`** (calls to `.locked`, `.locked_async`, `_store_lock`, `_async_store_lock`, `StoreLock.ordered`, `fcntl.flock`, the same at `389d0b22a` and `8bc8f8fef`; a grep finds 101 lines, two of them `def try_store_lock(` and `is_store_lock(`). Each picks its own lock, mode and wait.
- **24 lock calls pass `mode=` or `contention=` by hand** (`(\.locked|_store_lock|_async_store_lock|ordered)\([^)]*(mode|contention)=`), and `StoreLockOwner.locked` (`store_files.py:301-305`) makes both caller parameters.
- **Every wait is unbounded by default.** `UNBOUNDED_CONTENTION` (`store_files.py:129`) is the default at `store_files.py:167`, `:303`, `:492`, `:505`, `typed_table.py:2321`, `:2351`, `bus.py:123`, `:139`, `:495`, `:581`, `message_bus.py:100`, `coordination_session.py:87`, `:128` (19 occurrences). On 2026-10-24 the registry lock was held in 200 of 200 samples, and exclusive waiters starved at `store_files.py:112` behind it, among them `turn_lease.py:652` on every tool call (fixed in #3187) and `queued_input.py:438`.
- **SQLite connection boundaries are serialized at four call sites by hand.** Opening or closing a connection initializes or cleans the WAL index and can answer BUSY to another attachment before any transaction exists (`typed_table.py:2325-2331`), which `BEGIN IMMEDIATE` (`locked_store.py:692`) does not cover. `typed_table.py:2246` (`reconcile_at`), `:2335` (attach), `:2340` (retire) and `locked_store.py:645` (`settle`) each take the file's lock around that boundary themselves.
- **12 hand flocks bypass the store family entirely:** `restart_queue.py:410`, `:420`; `pi_durable_backend.py:230`; `private_sidecar.py:144`; `installer/launch.py:248`; `child_process.py:443` (`acquire_session_writer`, used by `native_session_files.py:121`); `agent_comms_hooks/watch.py:109`, `:117`, `:129`, `:135`, `:143`, `:153`. Each is a process-singleton or writer claim with its own lock path, open flags and release.
- **Maintenance is checked by hand at 9 DUPLICATE sites** beside writers whose store kind already refuses (`StoreWriter.require_maintenance_open`, `typed_table.py:2213`; `CarriedDocument.require_maintenance_open`, `maintenance_barrier.py:646`): `registration.py:269`, `comms.py:91`, `message_bus.py:157`, `owner_lifecycle.py:314`, `:360`, `:378`, `:516`, `external_transcript.py:575`, `goal_attempts.py:690`. `typed_table.py:2209` and `goal_attempts.py:764` are the target shape already: the check inside the store's own write transaction.

## How to execute without churn

- **No stored data changes.** Lock files keep their paths (`store_lock_path`, `store_files.py:343`); only who takes them changes. No carry step.
- **Delete, never wrap.** A site the audit marked PROTECTS NOTHING loses its lock with no replacement. A WIDER site moves to its own store's guard, as #3187 did for turn files.
- **One store family per PR.** Phases 1 to 3 change `store_files.py`, `locked_store.py` and `typed_table.py` together, so they run in order with one editor. Phase 4 waits for P628 (unit-installer), which owns `maintenance_barrier.py` and `goal_attempts.py` now.
- **Out of scope while owned elsewhere:** `coordination_session.py` and `proven_source_coverage.py` (fork-coverage), `typed_table.py:2183-2310`, `goal_attempts.py` and `maintenance_barrier.py` (P628), and `queued_input.py`'s `deliver_pending` (#3183). Each joins its phase once its owner merges.
- **Guards are the existing ratchet.** Phase 5's measure goes into `tools/debt_ratchet.py`; no new CI.

## Phases

Each phase, its predecessors and its evidence on `origin/main` are declared in `src/agent_comms/plan_phases.py` (`StoreGuardsPlan`).

| Phase | Done when, on main | Now | Done |
|---|---|---|---|
| 1 `StoreGuardFamiliesPhase` | `SingleFileStore`, `SqliteStore`, `MultiFileStore`, `ProcessSingleton` declared in `store_files.py`; `fcntl\.flock\(` outside `child_process.py` | 11 | 0 |
| 2 `ReadWriteMembersPhase` | `(\.locked\|_store_lock\|_async_store_lock\|ordered)\([^)]*(mode\|contention)=` | 24 | 0 |
| | `\.locked\(` outside `store_files.py` and `locked_store.py` | 68 in 31 | 0 |
| 3 `BoundedWaitPhase` | `UNBOUNDED_CONTENTION\|UnboundedContention` | 19 | 0 |
| 4 `MaintenanceInWritePhase` | `assert_open_unlocked\(\)\|require_maintenance_open\((path)?\)` outside `store_files.py`, `locked_store.py`, `typed_table.py` and `maintenance_barrier.py` | 9 | 0 |
| 5 `LockCensusPhase` | `^class StoreLockCalls\(` in `tools/debt_ratchet.py` | 0 | 1 |

### 1. Each store's guard is declared on its family

**Target.** Four guard families under `StoreLockOwner` (`store_files.py:275`), each owning how its store is locked:

- `SingleFileStore`: one JSON or text file published by atomic rename (`JsonDocument._write_unlocked`). Its writers take its own lock; its reads take none, because a rename is atomic and `JsonDocument.read()` is already lock-free.
- `SqliteStore`: a SQLite file. Its guard serializes connection attachment and retirement, and SQLite's own write transaction serializes writers between them. `reconcile_at` (`typed_table.py:2246`), `settle` (`locked_store.py:645`) and `WalStore._attached` (`:2335`, `:2340`) open their connections through it and name no lock.
- `MultiFileStore`: a store whose state spans files, such as the registry with its turn files (`RegistryStore.cut_paths`, `registry_store.py:271`). Its one lock covers the set; its members declare that they follow it (`DependentStateTier`, `store_files.py:335`).
- `ProcessSingleton`: one running process per path. It owns the lock path, open flags, try-or-wait and release. The 12 hand flocks become members: restart queue watcher, Pi durable session, private sidecar, installer launch, session writer and the hook watcher.

**Witnesses.** `store_files.py:275-305` (`StoreLockOwner.locked`), `typed_table.py:2246`, `:2335`, `:2340`, `locked_store.py:645`, and the 12 flock sites above.

**Checks.** `git grep -cE 'fcntl\.flock\(' -- src ':!src/agent_comms/child_process.py' ':!src/agent_comms/skills/'` goes from 11 to 0. The four classes are declared. Behaviour: two processes writing one store still serialize, and a second process starting a singleton still refuses, each pinned by one test per family, not one per store.

**Deleted.** The four call-site SQLite flocks (moved into `SqliteStore`'s attachment), 12 hand flocks, and their per-site lock path spellings.

### 2. Read and Write members; `locked()` stops being public

**Target.** Every guard family exposes `reading()` and `writing()` context managers. Each family's override decides whether a read locks (`SingleFileStore`: no) and which mode a write takes. `StoreLockOwner.locked` becomes private to `store_files.py`, and its `mode=` and `contention=` parameters are deleted. `StoreLock.ordered` (`store_files.py:163`) takes guards, not `{owner: mode}`.

**Witnesses.** The 24 `mode=`/`contention=` sites, for example `registry_store.py:254`, `:284`, `input_origin.py:150`, `selected_participant.py:226`, `:301`, `goal_management.py:419`, `goal_waits.py:264`, `read_basis.py:737`, `cursor_publication.py:113`, `todos.py:4438`, `optional_awareness_projection.py:191`, `:270`, `active_route.py:334`; the 68 `.locked(` calls in 31 modules.

**Checks.** Both patterns in the table go to 0. Behaviour: a registry read while another process holds the registry exclusively still returns (the #3187 pinning test generalized to every `SingleFileStore`).

**Deleted.** The `mode=` and `contention=` parameters, every caller-chosen mode, and the 19 PROTECTS NOTHING locks (deleted, not moved).

### 3. The wait policy is a guard property

**Target.** Each guard family declares its wait: bounded, with the bound on the family (`StoreLockContention(seconds)`), and released when the holding process stops. A waiter past the bound raises `StoreLockBusyError` with the holders (`store_files.py:73`) instead of starving. Delete `UnboundedContention` and `UNBOUNDED_CONTENTION` (`store_files.py:119-129`).

**Witnesses.** The 19 `UNBOUNDED_CONTENTION` sites listed above; `store_files.py:93-112` (`StoreLockContention.waits`, 25 ms retries); the 200-of-200 registry hold.

**Checks.** `UNBOUNDED_CONTENTION|UnboundedContention` goes from 19 to 0. Behaviour: a writer blocked past its family's bound raises with the holder named, and a killed holder frees its waiters.

**Deleted.** `UnboundedContention`, `UNBOUNDED_CONTENTION`, and every `contention:` parameter defaulting to it.

### 4. The maintenance hold is checked in each guard's Write

**After P628** (unit-installer's ledger fold, which owns `maintenance_barrier.py` and `goal_attempts.py`).

**Target.** The guard's `writing()` asks the store's kind whether maintenance refuses it. That kind is `StoreWriter.require_maintenance_open` (`typed_table.py:2213`), with `CarriedWriter` at `:2301` refusing and `RuntimeWriter` at `:2309` allowing, plus `CarriedDocument` (`maintenance_barrier.py:646`). The 9 DUPLICATE call-site checks are deleted.

**Witnesses.** `registration.py:269`, `comms.py:91`, `message_bus.py:157`, `owner_lifecycle.py:314`, `:360`, `:378`, `:516`, `external_transcript.py:575`, `goal_attempts.py:690`. `typed_table.py:2209` and `goal_attempts.py:764` are the target shape already: the check inside the store's own write transaction.

**Checks.** The pattern in the table goes from 9 to 0. Behaviour: a write during maintenance still refuses, and the maintenance holder still writes.

**Deleted.** The 9 hand-placed checks.

### 5. A census of lock calls, ratcheted to zero

**Target.** `tools/debt_ratchet.py` gains `StoreLockCalls`: it counts lock calls outside `store_files.py` and `locked_store.py` (the census pattern above) and refuses growth on any file.

**Witnesses.** 99 calls in 42 modules at `389d0b22a` and at `8bc8f8fef`.

**Checks.** The class exists; the census reads 0 once phases 1 to 4 land.

**Deleted.** Nothing; this phase stops the next hand lock from being written.
