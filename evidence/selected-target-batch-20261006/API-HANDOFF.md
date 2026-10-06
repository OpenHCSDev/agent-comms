# Selected-target backend API

Source checkpoint only. Parent owns Toad selection, dialog and completion consumers. Mendel owns Core declarations and tag disposition.

```python
actions = CliCommand.target_catalog(
    comms, ("alpha", "beta", "#team"),
    channel={"alpha": "#team", "beta": "#other"}, project=str(project),
)
request = TargetEdit(
    declaration=action.declaration, target=action.targets, arguments=editor_values,
    confirmed=confirmed, channel={"alpha": "#team", "beta": "#other"},
)
result = request.apply(comms)
```

A single string and a shared channel string still work. A tuple selects a batch. The optional channel mapping carries row context for thread pins; it is not backend membership or permission state. The same thread name selected twice is one target.

`TargetAction.bound` is now a tuple of original command instances. `editable_fields` is derived from those declarations. `edited(values)` returns another TargetAction. `confirmation` remains a property; the two Toad `edited(...).confirmation()` calls must drop the call parentheses. `with_confirmation(bool)` validates all original command warnings. `declaration`, `label`, `targets` and the encoded `bound_count` are the backend menu projection.

Only start, stop, archive/restore, read and pin declare multiple-target support. Mixed archive and pin menus derive their common declaration from their concrete command hooks. Channel start/stop expand the original visible roster and reuse original member eligibility. Archive/read/pin still use the channel, history and pin owners. No UI command list or permission reconstruction is needed.

At execution, every target is bound again. All editor payloads and warnings are checked before the first write. Overlapping channel/member operations execute once. Each command then checks its fresh binding and calls its original owner. Successful earlier actions remain successful if another target fails; there is no rollback or replay.

A batch returns TargetBatchResult with ordered TargetCompleted/TargetFailed outcomes, derived `successful` and `reconnect_targets()`. Completed outcomes retain the concrete command and original result. Failed outcomes retain the target/error type/text and do not assert that no effect occurred. Toad completion must show failures as partial failures; it must not notify the entire batch as successful. The existing declaration `reconnect_targets(result)` consumes successful start outcomes. The CLI emits the same outcome record and exits 1 for a partial failure. Single-target raw result shapes remain unchanged. CLI `--target`/`--targets` accept one or several names.

`DeleteExclusiveInactiveThreadsTagDisposition` extends the existing deletion cohort hook. It deletes only threads with exactly that tag, inactive status and no live original process. Active and multitag survivors keep their declaration, process, other tags and history; the deleted tag is removed through ChannelManagement's original tag-change operation. Existing delete-all and archive dispositions still refuse active owners. FieldCodec/DeclaredFamily automatically expose the new choice; no selector switch or second transaction algorithm was added.

Ownership trace: channel predicate/roster owns membership; command declarations and original Tool eligibility own availability; CliCommand owns batch binding/admission and ordered results; OwnerLifecycle owns native start/stop and birth-bound handles; ThreadManagement/Registration owns guarded archive/delete; ChannelManagement/catalog owns canonical tag metadata; HistoryViews owns read marks. Patterns addressed: IMPL-4 (complete family), IMPL-5 (one binding projection), IMPL-7 (declaration-owned operations), BOUND-2 (original codec).

The before trace uses existing refactor-audit Package across Core and Toad src/tests/tools: 1,483 parsed modules, zero omissions. Dynamic callback resolution is not claimed. Parent must migrate Toad's dialog/ThreadAction consumers; Mendel changes no peer checkout.

Source checks: 13 distinct focused controls passed across the initial batch and changed checks. They exercise the original private registry, catalog, messaging/read ledger, CLI and codec: mixed actions and partial failures, confirmation before writes, overlap deduplication, native roster availability without launching, human-only read marks, pin contexts, single-target behavior and tag deletion/preservation. Three wrong test expectations were corrected after reading their existing owners; all original failures/raw remain in SOURCE-RESULTS.json. Production stayed unchanged after the first checkpoint. Six touched Python files compile without imports; diffcheck passed. Native process start/stop, public mutations and installed multi-selection UI remain unrun. Parent owns the affected Toad integration/installed acceptance. Existing grants, raw results, helper roots and runtime installations remain unchanged.
