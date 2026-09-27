# S8 goal lifecycle/action handoff — 2026-09-27

Owner: authorized Codex S8 worker. Worktree `/home/ts/wt/comms-refactor-s8-goals-20260927`; branch `codex/refactor-s8-goals-20260927`. Publication receipt follows in `publication.json`. No live goal was resumed, input replayed, owner restarted, or deployment/PR merge performed. No extra workers, models or paid providers were used.

Exact owner directive: "and makenthem be aggressive in yhebrefactor, large highvleverage butes, no incrmental busywork, we have itblaidbout clewrly".

## Integration and ownership

Started at A8 `b97a108` with its existing A1/A2 foundation. Reused all three authorities; no new family registry, codec or persistence layer. Initial domain/action commit `738476f`. Main `0309f7b` (PR128) was merged normally, S1 `7c0b7f4` was merged as `568b05e`, and current main `3683792` (PR130/131/132) was merged as `2d6a757`. S1 completion and the owner's explicit handoff transferred the narrow goal event/ACP consumer methods before those edits began. No S1 work was duplicated or stopped.

The S1 merge also brings its published PR95 integration. The only manual merge conflict combined A8 `_store_lock(shared=...)` with S1/PR95's yielded descriptor and POSIX last-close semantics; both remain. Parent recovery, native, stack-pin and PR95 files enter only through normal prerequisite merges. S8 made no authored edits there, to parent cursor/admission methods, or to tests_headless_diagnostics. S4 retains read/routing and general presentation. The narrow `ThreadView.presentation` edit changes only its goal-standby predicate; `list_threads` changes only goal-pause projection. `wire_watch.py` changes only the goal-waits filename reference to its owner.

Draft base: published S1 branch, with A8/PR129 and foundation/PR125 dependencies explicitly retained. The fork head is intended for OpenHCSDev/agent-comms, not the fork's main. The draft includes inherited A8/foundation and newer main commits; the authored S8 commits are identified in the receipt so reviewers can distinguish them from prerequisites.

## Completed behavior and APIs

- `GoalState` owns status semantics, transitions, toggle action types, labels and presentation. `PausedGoal` carries a `PauseSource`, `BlockedGoal` carries its reason. `OpenGoal` owns the common active/paused transition algorithm; eligible destinations declare `FromOpenGoal`, and the successor set derives through A1. No repeated successor roster remains.
- `Goal` stores one typed state. `GoalStateProjection` supplies read-only legacy status/reason/source dataclass views, preserving existing `asdict` and `replace` consumers without a second writable authority. Persisted status strings remain `active/paused/blocked/completed`; `pause_source` is an additive optional field. Registry ingress uses A2 and joins an exact legacy pause event only if the new field is absent. Unmatched legacy pauses become OwnerPause. New rows carry the source across every revision; current pause queries do not read the audit file.
- Existing A8 `GoalPauseEvents` is retained for audit and legacy ingress. Existing SQLite GoalHistory remains the journal, with A2 serialization; no replacement history/event log was introduced. Old history snapshots lacking source take the conservative owner default. Legacy tools/constructors keep their boundary names; this is forward reading compatibility, not a claim that pre-S8 binaries accept new `pause_source` keys.
- `GoalAction` owns shared CAS, actor checking, publication and wait cleanup. Set, clear, edit, active, standby, paused, blocked, completed and explicit retry are concrete public subclasses with their own fields/behavior. `Comms.update_goal` accepts a typed action; its old flat keyword ingress remains a boundary decoder for unowned/external callers, not a behavior dispatcher. ACP goal methods and comms_goal now use typed actions. Model choices derive from `ModelInvocable`; the golden model request schema is unchanged. A model cannot take Pause, matching the S8 plan.
- Owner retry and resume retain the existing GoalAttemptStore, PID, goal identity/revision, turn and grant preconditions. No new incarnation/generation authority exists. Generation now exposes a typed lifecycle plus a legacy string projection. State classes own ready/resume/retry behavior; SQL CAS, grants and reservation identities remain in their established store.
- S1 `GoalChanged` is produced by the goal authority from the current durable snapshot versus an observer's last published snapshot. Existing registry/watch/poll and history publication carry changes across processes; A4 `AcpEventConsumer` handles the event. ACP completion evidence comes from the goal authority's existing reported_turn and goal ID, not a comms_goal tool-name check. The separate pre-existing comms_set_goal origin/launch-grant attribution is preserved; it is not the goal-change notification mechanism.
- GoalExecution presentation delegates to behavior owners, and goal waits owns its filename. The current source has no goal ACP PlanEntry construction to migrate; its UI updates are SessionInfoUpdate. The planned ACP status spelling is declared on GoalState without inventing a new UI flow.

Shared APIs for future owners: `LifecycleState` is the one registry-free ABC providing `successors`, `may_become`, and derived `transition_table`. Compose it with the existing `DeclaredFamily` as the domain root (as GoalState and GenerationState do); do not create a second lifecycle registry. `Command` is the analogous inbound ABC providing A2 `from_payload` and abstract `apply`; future S7 roots compose it with A1. A4/A10 are S1's exact mechanisms, unchanged except for the added goal event/handler.

## Evidence

All tests ran sequentially with repository xdist/coverage defaults disabled, PYTHONPATH=src, PYTHONDONTWRITEBYTECODE=1, worktree TMPDIR and basetemp, and a 60-second external bound per shard. Existing dependency installations were read-only; a disposable worktree venv referenced them and the existing metaclass-registry source. No live wire was used.

Commands follow this pattern:

```
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src TMPDIR=$PWD/.artifacts/tmp timeout 60 \
  .artifacts/testenv/bin/python -m pytest -o addopts='' -p no:cacheprovider \
  --basetemp=$PWD/.artifacts/<shard> <named tests> -q
```

Successful receipts (counts are per shard, with overlap; do not sum as unique cases):

- `final-core.txt`: 67 goal, A/B/C, history, mentions, pause, block-reason and LockedStore cases.
- `final-owner.txt`: 32 real local runtime/socket, owner retry/edit/snapshot, direct-interrupt and watcher cases.
- `final-state.txt`: 104 failed-turn, attempt-ledger, A/B/C and history cases.
- `final-acp.txt`: 49 A/B/C/fresh CLI and owner/runtime cases after typed ACP migration.
- `owner-fix.txt`: 56 ACP original-input/authority, standby and liveness cases.
- `events-regression.txt`: 45 S1 event, input-review, history, mention and resume cases.
- `goal-consumers.txt`: 29 goal-selected cases from ACP, declarations and tools (183 other cases deliberately deselected).
- `transitions.txt`: final focused state-transition regression after the NRA-discovered duplicated successor sets were replaced with the declaration capability.
- Earlier failed/intermediate receipts remain for provenance. They include the corrected transition-precheck ordering regression and old expectations intentionally changed by S8 (model pause, unmatched legacy pause, additive source field).

Experiment A adds just SpendCapPause with its instruction: edit/reopen/failure observation protect it without consumer cases. B adds just a ModelInvocable action, loads the ordinary tool catalog, invokes the real tool and dispatches its GoalChanged through S1's real ACP consumer. C exercises each action that keeps a goal paused, immutable rewrites, history, removal of audit data, reopen and failed-turn paths. Automated state transitions cannot remove the owner pause; explicit owner resume remains allowed. A fresh CLI subprocess against a fixture cannot resume an owner pause. Runtime retry tests use an alternate tool name with the genuine reported_turn, proving notification/completion no longer depends on the old tool spelling. UNKNOWN-input preservation and no overlapping retry launches remain covered by existing runtime tests.

Ruff passes across authored source and tests. Scoped mypy passes nine goal/shared modules. A separate declarations check reports only the existing Windows msvcrt stub omissions at the inherited A8 lock modes; no whole-project mypy claim. Formatting is confined to owned regions (unrelated MessageBus formatting was preserved).

NRA checkout `52fe8b4666a20583f0ddf8ed3b7a9e89857e4809`; actual CLI, operation catalog, public contracts, PatchTargetOperation and the OOPSLA manuscript `paper1_jsait.md` were read. Required-answer fidelity motivates carrying pause provenance; ancestry/capabilities supply common action and transition behavior. Exact declaration-targeted presentation recipe, simulation and revision-checked application are retained. Authored action/transaction moves are not claimed as native-equivalence proofs.

The initial full scan timed out; a subsequent bounded compact global baseline and final scans completed all 79 detectors with zero omissions and no cache. They use the complete package as context, single parser/analysis workers, and a 150-second internal/165-second external bound. `nra-acceptance.json` is the final authority; prior scans show the two successor-roster findings that were fixed. GoalExecutionState case recovery and FailedTurnProjection's serializer mirror disappear. GoalPauseSource has no remaining case recovery (the baseline detector did not report it separately). Remaining raw/grouped findings belong to other surfaces; see `nra-assessment.json`. No package-wide zero-findings claim.

Available RAM stayed above 8 GiB (about 14–24 GiB observed); disk headroom stayed about 40–42 GiB. `process-evidence.json` records the resumed owning Codex PID 1823525 in this worktree. Disposable test/codetool caches can be removed after all processes exit; durable receipts and fixtures are retained.

## Adoption and limits

No CI wait, full parallel suite, Windows run, installed-wheel run, mounted Toad/native-Pi pilot, or live deployment is claimed. The source and real local CLI/Unix-socket paths are validated. The mounted stack tests require separate prepared native/Toad fixtures and contain external temp-root assumptions, so they were not silently activated. Parent owns deployment, S4 owns its disjoint read/routing work, and PR95 successor retains its ongoing branch. No unfinished S8 G5 integration remains. Next step is review/adoption of this draft with A1/A2, A8 and S1 included once, followed by the owner's desired integration gates. Do not resume a live goal as part of adoption.
