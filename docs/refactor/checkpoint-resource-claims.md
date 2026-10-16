# Checkpoint-owned resource claims

Owner: core-goal-retry. PR #966. Base: `46ee4a206a28dfb97ff93ed4d839436cda4b5261`.
Measured and privately installed production: `03b2a98c`.

**Source and private installed acceptance pass. Current-main integration, reviewed
live cutover and default installation remain with Codex. No live schema was changed.**

## Before: the complete reader family

At the base, `StoreLock.claim_transitions` (`store_files.py:141`) materializes every
certified message and extracts every transition. Four external callers consume it:

| Consumer | Before | Now |
|---|---|---|
| Selected claim (`claim_admission.py`) | Replay all; scan consumed operations again | Indexed resource and unique operation-ID queries |
| Publication (`publisher.py`) | Replay all before append | Shared transition behavior over touched current resources |
| Selected write (`claim_admission.py`) | Replay all to verify one claim | Indexed exact current owner |
| Terminal release (`coordination_snapshot.py`) | Replay all | Read current membership, never journal history |

`WireLog.claim_projection` independently scanned raw history; it now reads current
checkpoint membership. The recursive StoreLock refresh, missing-checkpoint scan,
replay-all helpers, unused Message transition accessor and forwarding-only claim
reader context are deleted. There are no production references to the deleted
`claim_transitions`, `project_verified_transitions` or `apply_transition` APIs.

## Facts and determining owners

- `ClaimTransition` owns exact incarnation/generation release, authenticated rename,
  full-set atomic application and optional-operation legality. Publication preflight
  and committed checkpoint insertion use this same behavior, not copied algorithms.
  Its single `max_resources = 32` declaration supplies publishing and release batches;
  this is not a history/recovery retention cap. The existing UUID width is unrelated.
- `Message.checkpoint_rows` owns publication and optional claim declarations.
  `MessageWireRecord` delegates that behavior; committed-delivery indexing still
  extends it through its existing inheritance chain.
- Immutable `ClaimCommits` stores the original typed transition once. Unique
  generated sequence and optional operation-ID columns derive from that value
  through the existing FieldCodec/Column mechanisms. A consumed ID survives release.
  One envelope can acquire multiple resources: putting its unique operation ID on
  each resource row would reject valid original evidence.
- `ResourceClaims` contains only a resource primary key and original acquisition
  sequence, with a deferred foreign key to `ClaimCommits`. Ownership is derived from
  the original transition, not mirrored. Both relations live in the existing sealed
  SQLite checkpoint and its envelope-commit transaction; no new store/cache exists.
- `CertifiedSourceRead` and its declared row types answer resource, operation and
  current-inspection queries. Actual SQL plans use their unique indexes, not SCAN.
  Admission reads only touched current acquisitions plus the latest claim sequence;
  normal B-tree costs remain. Current inspection is proportional to current claims,
  not settled message/transition history.
- `StoreLock.current_source` owns refreshed certified reads under its retained physical
  lock. Missing/old checkpoint shapes refuse instead of invoking another reader.
- `TypedTable` owns shared keyed predicate encoding and removal, including checkpoint
  recovery. The canonical checkpoint owns fsync/certification and exception handling.
  Failed verification now explicitly reports durability UNKNOWN rather than hiding
  that uncertainty behind only “unavailable”. No replay permission was added.

## Durable cutover — not performed on the live root

`tools/cutover/carry_resource_claims_once.py --bus /explicit/bus.jsonl` plans by
default. `--apply` is an offline operation after old writers stop. It reuses the
physical lock and canonical checkpoint installer, verifies the original final seal,
exact preceding schema and source revision, then atomically reconstructs the
checkpoint with the two declared claim relations from original verified envelopes.
Bus bytes and every non-seal marker fact remain unchanged. Foreign, changed, partial
and unfinished sources refuse. An interrupted apply remains UNKNOWN; do not retry
or replay application input to conceal it.

The new runtime has no old-shape reader or scan fallback. Integrate current main
before default publication; do not install the old-base standalone wheel over
newer unrelated work. Delete the temporary carry tool after reviewed cutover and
acceptance. No source data, user session/auth or UNKNOWN input was modified here.

## Measured actual wire-lock scope

Both sides are file-wheel installs with PYTHONPATH unset and Python `-I`. The base
seeds each authored bus through public Comms publication, then acquires/conflicts/
releases real claims. The candidate carries the same bus without changing its bytes
and uses the same public publisher. There is no synthetic lock or replacement app.
Three successful fresh acquisitions per size; ordinary fsync and registry work stay
in the measurement. Conflict is synchronous and appends no bus bytes.

Timing starts after native physical acquisition and ends after the matching lock
file closes. It includes post-close callbacks, so it is an upper bound on actual
physical lock hold, not a kernel-exact unlock timestamp.

| Initial settled messages | Final bus bytes | Base median ms | Candidate median ms |
|---:|---:|---:|---:|
| 100 | 3,401,143 | 41.23 | 39.04 |
| 1,000 | 33,937,930 | 118.05 | 27.99 |
| 5,000 | 169,668,929 | 623.18 | 35.48 |

The process-wide decoder hook observes roughly H+3..5 decodes on the base; candidate
samples are 1..6, including unrelated post-release/background preparation. It is
not an isolated admission-only counter. Separate refusal hooks prove claims do not
call either certified whole-message iteration or raw bus replay. Indexed work is
history-independent; wall time still varies with fsync and host scheduling.

A prior candidate checkpoint's samples remain retained, not silently replaced.
The final table is `installed-receipt.json` / `bench-*/final-after.json` for 03b2a98c.
A 5.5 GB duplicate/run was deliberately not attempted with critical disk/swap pressure.
This bounded roughly 170 MB run used existing dependencies and one process at a time.
Do not extrapolate its absolute latency into a measured 5.5 GB result.

## Final confirmation

- **79 passed, zero skipped**, against the private installed wheel, including real Pi
  child/ACP claimed write, worker stop/start, exact release and saved-history startup
  with controlled provider replies. These are not new configured paid-provider claims.
- Multi-resource admission consumes exactly one operation; releasing all resources
  does not make that operation reusable. Reopen retains that fact. Fresh IDs work.
- Exact release/generation, parent-directory swap fences, whole-set conflict/rollback,
  partial/failed fsync, all blocked reader routes, later resync and crash certification
  pass without replaying original input.
- One-time carry plan is byte-preserving; apply recovers current claims/consumed IDs;
  ordinary old-schema reads and foreign-schema carry refuse.
- Installed receiving exits **0**, exact audit pin
  `120c04fb9a0eb4afafab6d621d1c5fd421bb7552`, no positive deltas or waiver. The initial
  two foreign-state probes were repaired at Message/ClaimTransition, not renamed or
  exempted. Source, wheel and private install match for all ten production modules.
- An unchanged base test expected a redundant second marker sample; it now checks
  the actual single certified marker owner. Two unchanged-base failure cases exposed
  the hidden UNKNOWN diagnosis; the checkpoint boundary was fixed, not those checks
  relaxed. Earlier failed/configuration/build logs remain available.

## Structural evidence and retained paths

NRA's existing parser/index, cache disabled and one parse worker, inventories
**1,064 before / 1,066 after modules, zero omissions** across Core, tests, tools,
paired Toad and metaclass-registry. The original canonical family index joined
2,511 declarations across 336 Core/dependency modules with zero unmatched declarations.
Actual installed MRO/schema and consumer searches supplement this syntax evidence.
Arbitrary getattr, callbacks, lazy registries and dynamic MRO are not fully resolved
by the syntax inventory; SQL plans and real installed transport/state checks provide
separate behavioral evidence, not an AST claim of correctness.

Owned artifacts: `/home/ts/.cache/agent-scratch/core-goal-retry/checkpoint-resource-claims`.
Concise results: `installed-final.txt`, `installed-receipt.json`,
`ratchet-final-installed.json`, `before-ast.txt`, `after-ast-final.txt`,
`before-families.txt`, `consumers-before.txt`, `benchmark.py`, and carry results.
`runtime/` and `wheels-03b2a98c/` retain the accepted private build; its unchanged
paired dependencies are pinned to the published e7a08b454 runtime. `runtime-baseline/`
and `wheels-baseline/` retain the exact base comparator. Authored `bench-*` buses remain
only for pending integration comparison; they contain no provider/native input.
The immutable source snapshot `/home/ts/wt/core-claims-baseline-46ee4a2` belongs to
this comparison and is disposable after reviewer/borrower checks. Failed test roots
remain retained; do not confuse them with accepted authored scratch.
