# S5 identity/counter ownership — completed source handoff

PR: https://github.com/OpenHCSDev/agent-comms/pull/153 (draft).
Branch: `codex/refactor-s5-identity-20260928`, persistent `~/wt` worktree.
Base includes main `9988f6b`: S3/ACP149, PR150 native-summary fix and integration152
including the native_prompt_binding observer fixture. Parent owns integration and
live acceptance. Darwin owns InputDrain/compaction queue behavior.

## Acceptance / actual ownership

- `ThreadIncarnation(name, created_at)` owns historical provenance. `OwnerIdentity`
  adds process generation. `TurnIdentity(incarnation, generation)` is the exact
  turn independently of metadata/owner-counter changes; its lease separately binds
  the existing admission generation. This is deliberately more precise than the
  provisional A7 structure which included mutable ownership in DM provenance.
- `GenerationCounter` owns monotonic allocation and tombstones. Registry owner and
  admission domains reuse it. S5 owner generations advance for process changes,
  explicit owner replacement or entry/exit from active ownership. Metadata, turn
  start/finish, rename, idle heartbeat and already-stopped status changes do not.
  Only turn start advances the turn counter; registration cannot forge/reset it.
- `_turn_epochs` state is removed. `ActiveTurn.current()` validates the existing
  admission/turn witnesses. Begin/finish leases own typed TurnIdentity. Registering
  a saved revoked turn strips its admission; it cannot restore execution authority.
- `TurnClaimFence` is only a compatibility alias for `TurnLeaseFence`; scalar
  constructor/accessors derive from its identity. ACP needs no overlapping edits.
- Launch pipe proofs bind typed OwnerIdentity. Launch/restart admission consumers,
  goal-failure observations, DM pages and read-ledger conversations are migrated.
- SQLite coordinator participant generation is intentionally independent: it
  advances on coordinator assignment/retry, not process admission. Runtime now
  names participant generation and owner admission separately. Tested both ways.
- No current src/stack/Toad importer used resource_claims; only its own test did.
  Removed module and test. Envelope claims/tool policy are unchanged.

## Open-window deployment compatibility — resolved, tested with old processes

The first implementation's new-only registry keys were NOT compatible: actual
pre-S5 source reset its owner map and dropped the new ActiveTurn field on write.
Raw failure evidence is retained in `.artifacts/s5/coexist-before.txt`.

Final encoding retains `owner_epochs`, `owner_epoch_counter`, and `turn_epochs`.
The first two encode the single GenerationCounter; the third is a **derived
compatibility projection** of currently admitted turns, not a second in-memory
map or counter. S5 reads old names at the boundary and uses nominal owners within.
No persisted ActiveTurn.owner_generation field is needed: old readers already
preserve the admission/turn fields on which exact-turn authority now depends.
Read-ledger participant pairs likewise retain their old disk encoding, derived
from ThreadIncarnation fields. Both pair and nominal-object reads are accepted.

`tests/test_registry_coexistence.py` runs actual archived main `9988f6b` Python
in separate subprocesses against new output. Four cases pass:
1. Public registry: old UI title update preserves high-water owner counters,
   active turn/admission and new-core settlement of the original fence.
2. Guarded private registry: the same update works with the actual guard protocol.
3. Old-core stop revokes the new turn; restart/reused ID cannot settle the old fence.
4. Old UI reads new read-ledger marks and adds its own; new core retains both.

**Mixed-version limit:** old writers still perform their historical extra owner
counter bump on metadata. S5 accepts that conservative invalidation without losing
admission, exact-turn settlement, historical identity or counters. Precise
owner-only bump semantics apply to S5 writers. No stopping an open UI is required.
No old behavior is claimed to have been changed inside a still-running old process.

Reproduce old-process acceptance without installing anything:

```sh
mkdir -p .artifacts/s5/old-core
git archive 9988f6b src/agent_comms | tar -x -C .artifacts/s5/old-core
AGENT_COMMS_OLD_SOURCE=.artifacts/s5/old-core/src \
PYTHONPATH="$PWD/src" /home/ts/wt/comms-refactor-integration-20260927/.venv/bin/python \
-m pytest -q -o addopts='' tests/test_registry_coexistence.py
```

## Local evidence (overlapping shards, do not sum)

- Baseline: 142 passed, 1 skipped.
- Initial identity/registry/DM/restoration/goal: 196 passed, 1 skipped.
- Merged launch/read/history/session consumers: 143 passed.
- Merged coordinator/native-binding/maintenance/restart: 121 passed, 1 skipped;
  actual local child restart preserves the session. Absolute PYTHONPATH is needed
  for this child because its working directory is deliberately a test worktree.
- Ownership/restart/authority/restoration: 50 passed.
- ACP streaming, relay cancellation and goal/final settlement: 4 passed.
- Final coexistence/identity/registry/goal/compaction-authority: 203 passed, 1 skipped.
- Final old-process + DM/read/identity acceptance: 36 passed.
- Final runtime/restart/goal shard after compatibility correction: 116 passed.
- Ruff and git diff checks pass. No CI gate, provider call or live restart/install.

NRA scanned all 10 changed/new production modules with full `src/agent_comms`
context (baseline reported declarations/operations/read_basis in the same complete
context). Final result recorded in `nra-summary.json`: the same four pre-existing
raw findings grouped into DisplayOrder, Message and ThreadStatus boundaries;
no new identity/counter mirror reported. Payload is `semantic_boundary_evidence`;
this build emits no scan_status or detector analyzed/omitted counts. Existing
findings remain for their assigned surfaces. Behavioral changes were authored:
these executed tests are not an NRA native equivalence proof.

## Changed production paths

- `src/agent_comms/thread_identity.py` — new A7 values/shared counter.
- `src/agent_comms/declarations.py` — registry, turn fences, DM identities and codecs.
- `src/agent_comms/operations.py` — launch proof, begin/finish, DM, admission consumers.
- `src/agent_comms/read_basis.py`, `read_ledger.py` — incarnation consumers and old encoding.
- `src/agent_comms/coordinated_runtime.py`, `coordination_store.py` — explicit generation domains.
- `src/agent_comms/goal_failure_observation.py` — typed lease consumer.
- `src/agent_comms/compaction_publication.py` — owner-generation reader API.
- `src/agent_comms/owner_compaction_adaptive.py` — **only three reader-name replacements**;
  no queue, admission, model or provider behavior changes, preserving Darwin's hunks.
- `src/agent_comms/resource_claims.py` — deleted dead duplicate authority.

Tests: new `test_thread_identity.py` and `test_registry_coexistence.py`; migrated
`test_declarations.py`, `test_operations.py`, `test_restart.py`,
`test_coordinated_runtime.py`; deleted `test_resource_claims.py`.

## Remaining scope

No implementation blocker. Parent integrates/deploys and performs live provider
acceptance. Production settings, live roots, pins, coding/claim internals and ACP
InputDrain were not changed. Legacy API/disk spellings remain compatibility
projections; broad cosmetic attention/claim renaming is not part of this identity
change. Failed attempts, old-reader failure evidence and scan payloads are retained;
owned successful fixture directories/caches can be removed once processes exit.
