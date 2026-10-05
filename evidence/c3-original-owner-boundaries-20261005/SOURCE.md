# C3 current goal selection and state refusal

Source checkpoint, not behavioral readiness. Original plan:
`docs/refactor/cleanup-20260929/C3-owner-state.md`.

## Existing facts and owners

`Thread.goal` is the persisted goal record. `Goal` owns identity, revision,
failure projection and state. `GoalState` owns activity and pause provenance.
`GoalPrecondition` owns command compare-and-set; it remains responsible for
exact record/state/process expectations. `TurnGoalAccount` owns permits and
settlement witnesses, not a second current-goal record.

The remaining copied decision was nullable current-goal selection by ID in
input review, command ingress, failed-turn projection and permit settlement.
Those operations deliberately permit different revisions of the same goal;
this must not replace exact-record CAS or active-goal admission.

## Implemented owner and consumers

* `Thread.goal_for(id)` selects the actual current record of an ID, preserving
  later revision/state. `require_goal` refuses an absent or replaced requested
  goal. Existing `require_active_goal` composes selection with state behavior.
* `GoalState.activity_refusal` owns the pause instruction and inactive notice;
  existing `require_active` and command CAS use it.
* `GoalActionContext.require_goal`, `GoalPrecondition.check`,
  `Goals._goal_input_review`, `block_goal_after_failed_turn`,
  `TurnGoalAccount.settle_original/finish`, and the model edit/resume tools
  now derive selection from `Thread`; their copied ID/absence predicates are
  deleted.
* `Goals.consume_reply_wait` uses existing `Thread.continuation_goal` after
  `RegistryOwner.require_registry`. `RegistryWorktreeRule` already requires
  the captured worktree to match at that boundary, so this retains the
  original active-goal/ID relation without adding a worktree refusal.

No fields, stored state, goal grants, queue, wait applicability, timeout,
native input or publication fences changed. Absent/replaced inactive native
admission still raises `RelationViolationError` (a `ValueError`); its notice
now derives the same cleared/replaced and owner-pause text used by commands.
Exact goal record/state CAS, completion CAS, and verified terminal witnesses
remain distinct and unchanged.

## Six original source surfaces

| Surface | Remaining boundary and owner |
| --- | --- |
| `coordination_response` | Frozen route/audience, existing receipt, append-once dispatch and source revision are publication boundaries; registry/SQLite/wire custody remains original. |
| `owner_lifecycle` | Original process birth, admission, restart selection and native loss receipts govern acquisition/release. Presence probes operate on actual owned process resources. |
| `goal_actions` | Selection/refusal copies removed; command CAS, actor capabilities and durable generation/grant checks remain rightful command and ledger boundaries. |
| `backend` | Actual child/session reuse, transport decoding, pending ACK/result and joined retirement belong to original session/transport owners. No alternate turn map is introduced. |
| `turn_watchdog` | Existing watchdog schedules original admission, stats and phase deadlines and joins actual read/cancel tasks. No copied deadline or state store added. |
| `goal_management` | Input-review, reply and failed-goal selection copies removed. Bus references/input review and full-record completion CAS remain actual publication boundaries. Parent #673 wait applicability is a separate accepted owner change. |

The five historical C0 families were already implemented before this branch;
none is reconstructed or requalified here. Captured input permissions remain
on `TurnGoalPermission`: exact owner permission, active continuation, and an
accepted input's active goal ID are different authorization relations, not
current registry selection. Serialized goal/execution identity validation is
an external value boundary.

## Source evidence and remaining acceptance

`BEFORE.json` uses the original NRA `Package.load` and its parsed trees for
production, test and tool call sites. Lexical attribute calls do not establish
dynamic receivers; the relevant receivers/storage/lifetimes were read.
No new scanner or dependency installation is used.

Parent #673 owns `GoalWait.current_for` and related wait-policy consumers;
this branch changes only the coordinated input-review and settlement
sections. Native lifecycle owners were notified before these edits. Its
actual merge `a7cc1da6cb5442e666c0a61012e6ef7de5eb865d` is normally joined;
`goal_waits.py` and `tests/test_goal_standby.py` have no branch delta against
that main. `AFTER.json` records the implemented owner call sites (18 methods),
with the same 324 production modules parsed and zero omissions.

No tests, installed package, native process, provider or public operation has
run for this change. Final acceptance must cover current/replaced/cleared
goal selection, owner pause, same-ID later revision settlement, exact CAS and
certified reply consumption through their real owners under a separately
issued purpose. Earlier C3/SDK receipts do not qualify this changed family.

Existing input-review/nominal fixtures which merely call `wire(...)` do not
by themselves establish a private-bus initial marker. The accepted
`goal_owner_fixture.canonical_goal_wire` is the original producer for a
future canonical goal control. Do not invent a marker or alter readers to
qualify these selectors. Legacy settlement cases in `test_acp.py` use the
real CommsAgent/OwnedTurn/SQLite path but replace the backend stream and
yield authored `Done` events. They are controlled producer-event cases,
not native-terminal or provider acceptance. The changed native controls in
`test_goal_selection_native.py` use the unchanged original saved SDK fixture,
`open_owner`, real TurnRunner and localhost HTTP. No terminal event is
manufactured and no backend stream is replaced.

The new controls are prepared, not run. `test_goal_selection_owner.py` uses
`canonical_goal_wire` for replaced/cleared selection, later-revision failure
projection, exact CAS, owner-pause refusal, model edit/resume and certified
reply consumption. Its leases are real registry begin/finish operations,
not native input proof. The separate two-case SDK control allows the actual
provider request to advance the same goal or replace it through canonical
commands; settlement must preserve the changed record and never apply the
old permit to a replacement. SDK-authored seed rows are distinct from the
one actual localhost native input per case.

## Effective packaged tuple

The initial proposed native 086 operand was stale and is preserved unchanged
in `PROPOSED-INSTALLED-OPERANDS.json`. Current source and its actual forced
manifest declare native 7a, tree `0d7ff54a...`. The append-only
`EFFECTIVE-INSTALLED-OPERANDS.json` corrects only that tuple and binds the one
authorized normal cached Hatchling 1.28 wheel (`WHEEL.json`): all 355
application assets equal Git, local source and ZIP, including the three
forced resources; metadata is retained in the full source proof.

The corrected tuple is not an artifact execution grant. The prefix remains
unborrowed; a fresh Bohr package/import/execution purpose and a NEW literal
Sch matching 7a READ/execution purpose are needed. No install/import/test/
native/provider job has occurred. No source, control or old proposal bytes
were changed to conceal the stale native operand.
