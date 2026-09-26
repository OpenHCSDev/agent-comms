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
- Internal `run_authority_child` is a single-attempt, maximum-30-second Linux
  transport with an independent pidfd watchdog armed before native exec. It
  inherits authority FDs, kills/reaps on timeout/cancellation, and never retries.
  The watchdog survives parent SIGKILL and receives no authority descriptors.
  See `compaction-durability-deadline-corrections.md` for the independent-review
  findings, exec-gate proof, effective SQLite EXTRA and per-COMMIT directory sync.
- Non-Linux/kernel-missing-pidfd configurations fail closed in this bridge;
  ordinary registry locks and audit attestations retain their prior portability.

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

## Executable native commit/journal checkpoint

`OwnerCompactionCommit` now couples the retained registry guard to a single-shot
non-forking Node helper in a **disposable** pinned package. The Python API checks
canonical owner/epoch/turn/goal and canonical saved-session path. It journals a
unique intent BEFORE dispatch, retains authority until child exit and outcome
persistence, and never dispatches a used ID. Verified SQLite EXTRA commits plus
explicit post-COMMIT directory fsync,
parent/ancestor fsync and a unique unresolved-session index preserve uncertainty
across crashes. A terminal outcome is immutable; unresolved intents block new
bridge commits to that session.

The native successor patch adds exact operation-ID/digest stamps, strict JSONL
reads and writer-locked reconciliation. A matching unique entry is fsynced before
reporting committed; absence only proves no-write if the exact captured disk
revision remains unchanged. Changed/duplicate/malformed/mismatched evidence stays
unknown. Removing a stale native lock is never itself reconciliation.

Patch a NEW disposable package only:

```sh
python stack/patch-native-session-writer-prototype.py --production \
  "$PI_NATIVE_PACKAGE_DIR/dist/core/session-manager.js"
python stack/patch-native-compaction-journal.py \
  "$PI_NATIVE_PACKAGE_DIR/dist/core/session-manager.js"
python stack/patch-native-writer-coverage.py \
  "$PI_NATIVE_PACKAGE_DIR/dist/core/session-manager.js"
```

Current resulting manager SHA256:
`41a94b3777ac0ec322f649e3e234836893b8de86085f55a927ce29216205c28f`.
The prior `8ec0b8f1…` journal-only artifact remains unchanged for the independent
`5f50fe7` durability/watchdog correction review; it is the exact input to the
new writer-coverage patch.
No production fault hook was added. The helper rejects receipt fields and
requires an inherited FD with matching stat identity and parent PID. **These
lineage checks do not independently prove a flock is held**; correctness relies
on the trusted Python launch path. They are not a new public RPC authorization
protocol or proof against arbitrary same-UID code execution. The bridge pins
SessionManager bytes, not yet the entire transitive package/deployment manifest.

Additional provider-free reproduction:

```sh
PI_COMPACTION_TEST_PACKAGE="$PI_NATIVE_PACKAGE_DIR" \
  TMPDIR=/var/tmp PYTHONPATH=src python -m pytest -n0 --no-cov -q \
  tests/test_compaction_journal.py tests/test_owner_compaction_commit.py
TMPDIR=/var/tmp node stack/test-native-compaction-journal.mjs
TMPDIR=/var/tmp node stack/test-native-writer-exclusivity.mjs
TMPDIR=/var/tmp node stack/test-native-auto-compaction.mjs
node stack/test-adaptive-compaction-contracts.mjs
```

Integration tests exercise actual native commit while stop/heartbeat/goal
registry writers are excluded, stale owner/goal refusal, lost result without
resend, no-write reconciliation, outcome-persistence failure, stale-lock
recovery, and real owner SIGKILL after native durability but before journal
outcome. A further deterministic test-only JS wrapper gates the actual native
`appendCompactionIfCurrent` after stdin parsing and lineage validation: kill
Python with the native child still waiting, prove real registry stop and native
executor acquisition both remain excluded, release the native mutation, then
recover the original intent under a fresh owner without resending. Neither the
production helper nor the patched package contains that barrier/fault hook.

The bridge now acquires the existing **executor lifetime** session fence
nonblocking BEFORE registry authority, and inherits both descriptors into the
native child. An already-running backend stream is refused before intent or
dispatch. This executor fence is distinct from the native per-entry writer
fence, which still comes AFTER registry authority. Idle persistent Pi managers
must additionally be closed/reopened by the future runtime integration; they
cannot silently keep an in-memory tree after this external helper writes.

The bridge additionally captures a frozen `CompactionSource` BEFORE summary
preparation: native witness, root identity, owner/turn/goal, and bounded actual
bus/input contents plus file identities. Commit compares those observations
under executor → wire → bus → registry → input → native exclusion, retains all
five Python descriptors in the child, and rejects current-admission UNKNOWN
inputs without resolving or replaying them. Whole-store fingerprints are
conservative: unrelated bus/input movement also declines the candidate. Invalid
or incomplete bus data declines preparation without repair. The legacy integer
correction counter is not used as evidence.

New tests cover raw bus sends, Comms.send and direct input-record races, accepted
queued correction refusal, STARTED-input drift, and pre-summary bus correction
invalidation. Native orphan tests now prove all five outer exclusions survive
SIGKILL. Combined focused suite: **177 passed, 1 skipped**; the seven
native-process race/crash cases passed three repeated runs. No provider calls
were used; the installed package remains unchanged. Subsequent independent-review
journal/deadline corrections raise the focused total to **186 passed, 1 skipped**
(see the dedicated corrections record).

## Native writer coverage successor (disposable only)

The next patch removes load-time newline repair and implicit legacy migration,
rejects corrupt/partial/invalid-ancestry persisted data, prevents post-load stat
refresh from blessing stale memory, and rereads unbound preloaded arrays. Empty
file initialization retains its pre-read revision through the guarded rewrite.
Fork and branch paths now hold their source lock; fork destination creation is
writer-fenced, exclusive, and file/ancestor-synced. The bridge pin now requires
this new artifact, not the earlier journal-only manager.

`stack/test-native-writer-coverage.mjs`: **19 passing controls**, with ten
reproduced assertion failures on the old artifact before passing on the new one.
Tests include actual external-process appends during constructor/switch loading,
held source/destination locks, stale branch refusal, fork partial-write and
parent-sync UNKNOWN denial, and coherent positive fork/branch paths. Fault hooks
are test-only Node builtin/prototype interception, not production package code.

On the same new artifact, native journal, writer exclusivity, hard-context,
manual/parallel compaction, input recovery and adaptive-contract scripts pass.
The two older summary fixtures now accept `PI_NATIVE_PACKAGE_DIR`, avoiding any
need to populate an installed/canonical package path for tests. Main-integrated
Python authority/journal/ingress regression suite: **200 passed, 1 skipped**.
Logs: `/var/tmp/pr48-allwriter-*.log`. Full-package provenance/preparation and
runtime integration are still unfinished; no installed package was changed.

## Still required before activation

1. Independent review of the combined authority/journal/native slice and
   exhaustive conflicting-writer inventory (see `compaction-writer-inventory.md`).
2. Integrate the source capture and ingress exclusion into actual summarization
   and runtime entrypoints; close/reopen idle persistent native managers and
   verify all ACP queue/steer paths, not just their canonical store methods.
3. Verified full deployment and trusted bridge origin at every actual runtime
   entrypoint, plus send/publication coupling (the authority lock alone does
   not make split transactions atomic).

Then canonical all-writer deployment, operator recovery, real adaptive/ACP
integration, and isolated end-to-end tests remain. The independent hard-context
backstop is unchanged. No installed-package edits, provider calls, or live
activation have occurred. Independent review is mandatory before merge.
