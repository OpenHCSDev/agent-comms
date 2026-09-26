# Production compaction successor

Base: merged PR48 `d0ced47fbec39ac4d110c9f7395442524363871d`.
This is **not deployment approval**. The adaptive trigger remains disabled.

## ACP native-commit send/outbox/reopen slices (combined review pending)

A committed exact-ID native outcome now atomically enqueues only metadata in
its durable journal; an existing local ACP owner session projects pending
metadata before the next native send, ACKing locally only after a transport
returns. No listener or uncertain send leaves it pending; exact ID dedups
reprojection. ACP final send boundary refuses any unresolved native intent or
UNKNOWN before binding input, including correction/follow-up paths. Idle native
Pi manager has an irreversible discard-before-external-write method; its next
attempt strictly validates v3 saved disk through the pinned read-only loader,
then compares fresh RPC state identity before any provider send. Canonical
legacy `/compact` fails closed rather than using separately installed Pi as
an alternate unjournaled writer. Provider-free focused 344 ACP/backend/journal/
reopen tests pass; full isolated source suite before local projection 1690/61skip;
updated full rerun and owner adaptive call site are still required. See
`compaction-publication-outbox.md` and `compaction-runtime-reopen-correction.md`.
This is not adaptive activation or combined clearance.

## Package-subprocess corrective successor (review pending)

Both reviewers retain NON-CLEAN for `8c6ce7` package admission: update checks
could invoke npm/git for already-installed unapproved sources. New candidate
admits the whole source set before dedup/probes and forbids all three actual
package-manager subprocess sinks, including direct metadata/global-root APIs.
Approved manifest-local install/discovery/check/update remains usable.
Tree `628b68df…`, build `e9f19aa7add2a71c`; 36 provider-free package-process cases,
actual canonical PR77 RPC/ancestor controls, source243/1skip, wheel54 and native/
adaptive contracts pass. See `native-package-subprocess-correction.md`.
Prior narrow import positives and both3585 manager-retirement CLEAN verdicts do
not clear this successor. Runtime/ACP/outbox/recovery/final review remain OPEN.

## Earlier 8c6ce7 import integration (package slice NON-CLEAN)

After draft `5dbd6fe`, canonical preparation now includes the immutable PR77
production package, native-only manifest extension loader, pre-install package
source admission, synchronous Node resolve/load hooks and in-root compaction
helper. CLI and bridge preload the fence; helper/resource bytes must agree.
Tree `b6d13d86…`, build `26e29f3669b35ce5`; manager remains `10ac30c1…`.
Real canonical `pi install`/RPC proves PR77 registration/status with no unapproved
MCP child under kernel network denial. A reached committed dependency's ancestor
require is refused; old unguarded execution succeeds. Source **243 passed,
1 skipped**, wheel **54 passed**, native/import/adaptive contracts pass.
See `native-import-boundary-integration.md` for exact evidence and limits.
Actual runtime/idle/ACP/outbox/recovery integration and fresh review remain OPEN.

## Earlier blocking independent writer findings

The `9c7445e` writer-coverage review is **NON-CLEAN**. Manager `41a94b37…`
remained affected through `8cf666c` and the main integrations at `98b8770`:

1. A denied `createBranchedSession` destination lock leaves changed manager
   state pointing to a nonexistent destination. Catching the error and then
   appending can report success while creating headerless JSONL.
2. A malformed `setSessionFile` target throws after binding its path/revision,
   retaining the old tree and flushed state. Catching the error and appending
   can write a stale-tree row onto the corrupt target.

The corrective candidate now irreversibly poisons a failed manager, covering
all subsequent mutators and history/witness reads rather than just the first
exception. New manager `10ac30c1…`, tree `7d16eb01…`, build `1684f7d9f014feb8`:
**494 continuation controls pass** on a fresh canonical build; both original
counterexamples reproduce on the preserved old artifact. This is owner evidence,
not by itself independent clearance. Preflight reviewer subsequently reported
narrow CLEAN for exact `3585b0f` (own differential and 114-control subset).
Original-findings sink subsequently independently gave narrow CLEAN for3585b0f
as well (18 own denied continuations and fresh-instance recovery). Runtime
manager disposal/error projection is NOT covered. Exact corrective review goes to both
`pr1-goal-p15-sink-independent-review` and
`pr1-native-goal-preflight-independent-review`. See
`compaction-writer-failure-state-corrections.md`; import closure and runtime
integration remain open.

Independent report:
`/var/tmp/ac-pr48-writer-coverage-independent-review-9c7445e-20260926.md`.
The prior journal/watchdog narrow CLEAN is unchanged. Main `fc417934` (PR77)
was normally merged at `98b8770`; its integration run was **240 passed, 1 skipped**,
which does not include corrected versions of these two still-open negatives.

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
SessionManager bytes at this initial checkpoint. The later complete-package
checkpoint below replaces that weaker bridge-only pin.

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
Logs: `/var/tmp/pr48-allwriter-*.log`. This checkpoint preceded the complete-tree
preparation below; runtime integration remained unfinished and no installed
package was changed.

## Complete-package preparation and packaged bridge resources

At the prior provenance checkpoint, canonical preparation applied three native
successor patches (the failure-state correction now adds the fourth) and
verified the entire dependency tree. The CLI wrapper verifies it again before
launch; the bridge checks before journal creation and before each native call.
A single full-tree commitment in `pi-native.sha256` replaces the manager-only
bridge pin. That checkpoint's build was `d45562f846a0afa3`, tree SHA `4a688172…`;
the current corrective pins are listed above.
Preparation materializes only internal regular-file npm aliases as independent
copies, preventing symlink/hardlink patch escape into stock. Node ambient loader
options are removed; the managed-project bootstrap is preserved inside the
verified native package. The helper uses non-forking `env` → Node exec, retaining
the exact watchdog PID and inherited exclusions.

Wheel builds include the canonical manifest and Node helper as package data;
installed code never guesses a neighboring source checkout for these resources.
An offline sdist → wheel build and **51 passing** extracted-wheel package/native
integration tests cover this path, without installing anything. The source-tree
provenance/authority/journal/ingress suite passes **227 tests, 1 skipped**. Canonical preparation was also
run twice in a disposable repository, with actual CLI `--version` and all native
scripts. Unlisted dependency drift blocks both launch and re-preparation, and
ambient malicious Node preload tests prove no marker execution.

Details and trust/rollout limits: `native-package-provenance.md`. Independent
CLEAN of prior durability/watchdog defects is limited to `5f50fe7`/`90c4d57`;
new writer/provenance bytes still require review. Current main `3e1813e` was
normally merged at `409349c`. Broader backend tests have 11 identical failures
also reproduced on archived main; those are not silently counted as passing.

## Still required before activation

1. Independent review of the combined authority/journal/native slice and
   exhaustive conflicting-writer inventory (see `compaction-writer-inventory.md`).
2. Integrate the source capture and ingress exclusion into actual summarization
   and runtime entrypoints; close/reopen idle persistent native managers and
   verify all ACP queue/steer paths, not just their canonical store methods.
3. Verified full deployment and trusted bridge origin at every actual runtime
   entrypoint, plus send/publication coupling (the authority lock alone does
   not make split transactions atomic).

Then safe rollout/retirement of old native writers, operator recovery, real
adaptive/ACP integration, and isolated end-to-end tests remain. The independent hard-context
backstop is unchanged. No installed-package edits, provider calls, or live
activation have occurred. Independent review is mandatory before merge.
