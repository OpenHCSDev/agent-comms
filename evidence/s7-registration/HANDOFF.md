# S7 Registration + merged S5 deletion closure

## Current owner instruction

Owner-DECISIONS top (2026-09-28): one implementation/internal API. Migrate current consumers and delete replaced interfaces. Old-client coexistence is **not** a target or gate. Preserve user data; parent owns coordinated rollout and current Toad migration.

Worktree: `/home/ts/wt/comms-refactor-s7-registration-20260928`.
Branch: `codex/refactor-s7-registration-20260928`; base `c45ee130b232e1afe6ed7253fd3bca39bdcf8a0b`.
Owner: Pascal. No live activation, provider calls, helpers, model changes or new environment.

## Implemented owners

- `RegistryDocument` owns registration preparation, lifecycle transitions, thread facts, alias provenance, S5 owner/admission counters, turn claim/settlement and snapshot creation. It has no I/O or borrowed Comms/registry self.
- `Registration` owns transactions over `RegistryStore`, owner/admission checks, goal-history journal ordering and publication/maintenance fences. Comms holds the actual component.
- `RegistryStore` adopts existing A8 `LockedStore` locks/typed document boundary, shared reads, copy-on-edit transactions and one revision cache. The existing private guard prepare/write/commit protocol remains specialized: interrupted publication must remain fail-closed, never use the ordinary JSON rollback writer.
- `Thread.from_registry` derives ordinary field decoding from the declaration and FieldCodec. Creation-time recovery retains historical provenance; real historical documents without stamps can still migrate. No second full Thread field roster.
- All current source/test/stack callers use Registration/store or typed identities directly. Goal completion/failed-attempt consumers use exact TurnIdentity and incarnation.

## Old mechanisms deleted

- Entire ThreadRegistry declaration and package/declarations export; no alias to Registration.
- Registry-owned mutable maps, hand-rolled load/save/revision bookkeeping, path/cache/guard/snapshot forwarding methods.
- S5 `live_owner_with_epoch`, `claim_live_turn_with_epoch`, scalar-result `claim_live_turn`, `RegistrySnapshot.owner_epochs`.
- `TurnClaimFence` alias; custom TurnFence scalar constructor and name/created_at/turn_generation getters. Producers/consumers use TurnLeaseFence/FinishedTurnFence with TurnIdentity.
- `DMDisplayBasis.viewer_epoch/peer_epoch` aliases.
- Duplicate `turn_epochs` write roster and validation path; exact ActiveTurn witnesses remain authority. Existing saved files with that redundant key load; next write removes it.
- Dual registry owner-generation-key decoder. Only established owner_epochs/owner_epoch_counter disk keys encode the current counter. These names exist at the saved boundary, not as internal aliases.
- Dual read-ledger participant decoder: one persisted pair encoding, decoded once into ThreadIncarnation.
- S4 ChannelDisplayScope `after`, `basis_revision`, `expanded_after`, their producer arguments and display-cache references.
- Pre-S5 old-process coexistence fixtures; no compatibility constructor or old-client acceptance gate.

## Integration seams and dependencies

- **Darwin ACP/TurnRunner:** only three ACP symbol substitutions here: import and two annotations `TurnClaimFence` -> `TurnLeaseFence`. In Darwin's current TurnRunner tree these live in `turn_runner.py` import plus `_finish_turn`/`_cancel_turn` annotations (observed lines30/383/396). This session has no peer-message/subagent tool and the Comms roster contains no Darwin thread; direct delivery was unavailable. The patch is narrow for serial integration; no TurnRunner behavior was edited.
- `owner_compaction_adaptive.py`: import/type and `registry.store.path` only. Queue/planning/settings behavior unchanged.
- Compaction native receipt `owner_epoch` is the actual current external native protocol field, not an internal alias. This patch changes no native receipt schema.
- `exporting.py`: Registration import/type migration only; preserve parent's PR125 enum/factory deletion on integration.
- Toad source audits: `toad-live-pins-20260928`, `toad-refactor-integration-20260927`, `toad-export-caller-migration-20260928`: no matches for removed registry/fence/epoch APIs or ChannelDisplayScope legacy members. No known cross-repo caller blocks removal.

## Acceptance and remaining scope

Focused evidence is listed below. No known implementation blocker remains in this assigned surface. Actual local process restart and inherited-lock authority are covered; no live deployment claim. CI deferred by owner. Parent owns serial merge/deployment.

Known existing failure: `test_viewer_snapshot_index.py::test_reopened_viewer_snapshot_decodes_only_appended_rows` expects zero message decodes after reopening but sees100. Reproduced on untouched base c45ee13 using its archived source; current values/unread counts remain correct. Baseline/current logs retained. This is an existing display-index optimization issue, not an old-client coexistence check.

## Test evidence (xdist defaults disabled, existing integration venv, PYTHONPATH=this src)

| Shard | Result | Notes |
| --- | --- | --- |
| Baseline/core extraction | 158 passed,1 skipped | Registry durability/guard faults, declarations, identities, restoration, goal history, A8. |
| Deletion core | 208 passed,1 skipped,2 fixture migration failures | Two Thread-mutation fixture entries were accidentally changed to fence fields; corrected, then all failure-observation tests passed in display-final. |
| Lifecycle/restart/consumer closure | 254 passed,11 skipped,9 fixture migration failures | Actual local worker restart, inherited flock, operations, historical goals, IRC, ownership and scaling. Nine test dictionaries retained expected_epoch; corrected and gate cases passed in admission-final. Eleven opt-in native compaction cases skipped; no native acceptance claim. |
| Admission final | **113 passed** | Maintenance, selected-tool runner, coordinated runtime and corrected compaction gate. |
| Response final | **53 passed** | Coordination response, compaction publication, optional awareness, historical views and current painted-message acknowledgement. |
| Display/current identity final | **99 passed,1 base-reproduced failure** | Registration component, fresh-process lifecycle, registry/read-ledger roundtrip, typed fences, DM/channel views, failed-goal observations. Existing sidebar cache issue above. |
| Ledger final | **1 passed** | Current saved encoding survives fresh Comms open and another painted-message acknowledgement. |

An overlarge admission batch hit its60s bound; `admission-closure.txt` is partial evidence, not a pass. It was split into the successful admission/response shards above. One attempted display invocation used a nonexistent filename and ran zero tests; retained as display-closure.txt. Other first-run failures and later fixes remain in logs and compressed fixtures. Existing optional-awareness tests required their fixture thread to declare the fixture's configured model; production model settings are untouched.

NRA before/after scans: eight final selected modules with full src context;4 reported findings both before and after, grouped around existing DisplayOrder/Message/ThreadStatus ownership. Payload mode semantic_boundary_evidence; tool emitted no scan_status/detector coverage counts. This is analysis evidence, **not** native NRA semantic-equivalence proof. New component/source migration was authored directly where no automatic verified recipe was available. Raw scans compressed; summaries included.

Cleanup: only this task's exited disposable caches, archived source copies and temporary test roots are removed after preserving all test logs, failure fixtures and before/final scans. No native package installed or copied. Current source/worktree stays persistent under ~/wt.

Changed Python files: Ruff check and format check passed (52 files). Source diff whitespace check passed; retained pytest output is whitespace-normalized only. Full changed-file inventory: [changed-files.txt](changed-files.txt).
