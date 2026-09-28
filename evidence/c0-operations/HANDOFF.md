# C0 operations — completed source handoff

Source: `ab41680afb122efcf69223da94ca82cc0d21946d`. PR178 draft:
https://github.com/OpenHCSDev/agent-comms/pull/178

Base: current main177 `7eaa38d` (includes PR176 checkpoint migration). Tree:
`/home/ts/wt/comms-refactor-c0-operations-20260928`.

Lease closure is complete/merged. C0 operations implementation and focused
local acceptance are complete. Parent owns combined declaration/Toad integration.
No live changes or checkpoint migration changes.

## Ownership/import boundary for Darwin and parent

- Pascal deletes operations.py. `comms.py` becomes construction only.
- `messaging.py`: guarded publication, human sender identity, receipt decoding;
  direct bus read wrappers disappear (callers use bus).
- `agent_activity.py`: activity/runtime-info stores and local turn leases.
- `channel_management.py`: catalog mutations/tag membership and passive scope.
- `transcripts.py`: transcript route store, bounded native transcript reading.
- `history_views.py`: history/display/read marks, display basis and sent cache.
- `owner_lifecycle.py`: launch pins, maintenance, process start/stop/restart.
- `thread_management.py`: registration/import/rename/delete/fork transactions.
- `goal_management.py`: goal operation state, waits, dispositions and action context.
- `collaboration_ledger.py`: registered-author mutation over the existing ledger.
- Darwin owns declaration bodies, bus/presentation extraction, declaration import
  destinations. These components initially import current declaration owners;
  imports will migrate using his destination map/commit. No old aggregator kept.
- Parent owns current Toad method/import migration and actual checkpoint fix.

Existing lock ordering and wire transactions remain. Each component receives
explicit dependencies; no shared Comms/self backpointer or method forwarding
facade. Operational methods disappear from Comms and all core/test callers move.

Direct `codex queue` to Darwin 01a0e526-04a9-7981-bbb1-f152b3a9edf5 was rejected:
`direct app-server input is not allowed for unloaded spawned sub-agents`.
Parent has been informed; this file is the exact import/file handoff.

## Implemented checkpoint

`operations.py` is deleted. `Comms` now has only its constructor; no old methods,
shared-self mixins, `__getattr__`, or old module re-export. Ten explicit owner
modules consume Registration/MessageBus/ChannelCatalog/ReadLedger rather than
copying them. Goals own waits/pauses; goal actions take Goals rather than Comms.
Relationships now receive registry/bus/views explicitly, removing its root
backpointer. Ten old operation entrypoints are replaced with direct owner APIs; the ledger
guard now extends the real SharedLedger owner. The complete current method/field/import map is in
`caller-map.json`; the goals operation module is `goal_management.py`, reserving
`goals.py` for Darwin's Goal declarations.

Current core/test/script callers migrated, including subprocess Python fixtures,
CLI/runtime requests, ACP components, normal coding tools, claim/delivery,
compaction, migration/cutover, exports and history. Existing transactional guard
order and on-disk data encoding unchanged. No checkpoint/private-bus changes.

## Focused checkpoint evidence

Commands use the existing integration venv, absolute PYTHONPATH to this tree,
`timeout 60 ... -m pytest -q -o addopts='' --basetemp=.artifacts/c0/tests/<shard>`.
All failed attempts retained; fixture migration failures are not production
proofs and are not reported as initial-green batches.

- core-second: 121 passed, 3 stale test hook failures; exact core-fix: 3 passed.
- goals-first: 69 passed.
- views-first: 96 passed, 4 dynamic helper failures; corrected in fixture-fixes.
- routes: 52 passed, 4 fixture failures (3 overlap views); corrected in fixture-fixes.
- adapters: 74 passed, 3 old fake-component failures; corrected in fixture-fixes.
- fixture-fixes: 25 passed (history/roster/restart queue, some overlapping passes).
- acp: 126 passed (ACP, participant loop, private worker entrypoint).
- publication: 53 passed, 3 embedded child-code failures; corrected in children.
- children: 22 passed, including separate-process human identity, SIGKILL before/
  after publication, CLI end-to-end, channel disposition and read marks.
- NRA: exact_compact_global, 79 analyzed/0 omitted, complete, finding_count=0.
  Exact full-context invocation is `nra-command.sh`; counts-only payload is
  interpreted via finding_count. This is authored extraction plus executed
  behavior tests, not native codemod equivalence proof.
- I/F lint and diff whitespace checks pass.

Remaining (parent): combine Darwin declaration source and paired Toad changes,
then installed activation. Operations source has no remaining implementation blocker.
The new components currently use the actual main177 declarations. Apply
`declaration-imports.patch` when combining Darwin source: it moves every new
component import using his canonical symbol-owners.json. No compatibility
aggregator is introduced. Current source is based on main177, with176 behavior intact.
No CI gate or additional provider run is requested.

## Final closure and integration evidence

- Removed operational package-root re-exports too: Comms/wire, result records,
  transcript records and observation interval now import from actual owners.
  All current core/test/script callers and embedded subprocess imports migrated.
  The canonical mapping remains stable for parent Toad work.
- Rebase176 conflict acceptance: 5 passed (existing-root preserve/append,
  invalid-prefix refusal and managed new-root default checkpoint install).
- Public import/end-to-end subprocess acceptance after export deletion: 3 passed.
- Current I/F lint and diff check pass. No repeated optional suites or CI wait.
- Preserved failed logs and scan evidence; large logs compressed. Cleaned owned
  exited test fixture/cache data (53MB before cleanup); no live data/worktrees
  touched. `changed-files.txt` is the exact production/test/script/doc delta.
- Direct Darwin queue was unavailable; parent supplied declaration map and now
  owns serial integration. Goal declaration remains his `goals.py`; operational
  Goals component is solely `goal_management.py`.

### Scope strength

This removes the Comms shared-self mechanism and makes its explicit components
own stores, caches, launch pins and cross-store transactions. Most method bodies
retain behavior; file relocation alone is not claimed as lower case-extension
cost. S7's separate WireLog/Publisher and universal LockedStore adoption are not
claimed complete by this operations task. Parent planning owns those leftovers.
