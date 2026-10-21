# P448 paired Toad edits — ready to apply after allocation is repaired

Owner: unit-goalblock. This is an implementation handoff, **not applied Toad code**.
Core PR #2529 checkpoint at preparation: `4bb907101156df2b027abc9123e552354176f6f9`.
Toad source inspected: origin/main `43b0467826a6b8926011aaa42cb1eb87ec34615d`.
The relevant files are unchanged from the existing read-only base checkout at
`de66b5120c2cc9ed29d86b84aa0e5a3bc22c32d9`.

The allocation owner is the commander's PR #2535, expected in install 42
(message 15553). Until that fix is actually installed, do not create a checkout
by another mechanism. The only prescribed checkout is
`/home/ts/wt/unit-goalblock--P448--toad`. Core must not install alone.

## Ownership and source evidence

Reuse `GoalSession`, which already owns a conversation's goal controls, display,
execution and source lifetime. Pass that existing owner to control predicates
and captions instead of passing a naked stored goal and recovering execution
from its state. Core's `GoalExecution` owns the action and caption. Toad renders
and submits that action; it does not classify failure or choose Retry by reading
`goal.state`, an enum, a kind string or an isinstance chain.

Refactor-audit Package inventory parsed all **283 Toad production modules with
zero parse omissions** at the main above. Core's existing inventory parsed 427
modules with zero omissions. Declaration/import/call evidence is retained at
`/home/ts/.cache/agent-scratch/unit-goalblock/toad-controls-ast.txt` and
`final-owner-ast.txt`. Dynamic Python rebinding is outside that syntactic proof.
Patterns: remove IMPL-2 enum-based fixture constructors and IMPL-12 copied Pause
behavior. Keep controls dependent on the execution owner. Do not introduce TIME-3
compatibility aliases for the deleted ThreadGoalExecution base.
The Core checkpoint also removes the stale state-toggle test and its literal
caption table; actual turn tests now check Retry versus Pause on the resulting
execution. StandbyExecution composes ActiveExecution and WaitingExecution so the
shared Pause action is inherited, not copied into the standby sibling.

Production consumers:
- `src/toad/goal_interaction.py`: deleted type import/annotation; the three stored
  state probes in ToggleInteraction; shared control predicate/caption protocol.
- `src/toad/widgets/goal_bar.py`: the single control-paint loop is the only caller
  of enabled/visible/caption. It already watches execution as well as goal data.
- `src/toad/widgets/conversation.py`: deleted type import and reactive annotation.
- `src/toad/screens/goal_details.py`: already watches both values and uses the
  session's execution. No replacement reader, state filter or cache is needed.
- `src/toad/session_observation.py`: already publishes both members of the same
  Core GoalSnapshot and retains an unavailable view on read failure. Keep this
  owner and source-binding checks; do not create another observation path.
- `src/toad/core/input_events.py`/GoalControl: declared interaction dispatch is
  already correct; control discovery remains derived from the family.

## Exact production edits

### goal_interaction.py

Import `GoalExecution` in place of `ThreadGoalExecution`. Remove the unused
GoalDisplay TYPE_CHECKING import after changing the signatures below.
`GoalSession.execution` returns GoalExecution directly, not a compatibility name.

Make the base control methods consume the owner already used by apply:

```python
@classmethod
def enabled(cls, session: GoalSession) -> bool:
    return session.display.can_control

@classmethod
def visible(cls, session: GoalSession) -> bool:
    return True

@classmethod
def caption(cls, session: GoalSession, collapsed: bool) -> str:
    return cls.label
```

CollapseInteraction keeps its differences only: enabled(session) returns True;
caption(session, collapsed) returns `"Expand" if collapsed else cls.label`.

Replace ToggleInteraction's predicate/caption/action with:

```python
@classmethod
def visible(cls, session: GoalSession) -> bool:
    return session.display.can_control and session.execution.toggle is not None

@classmethod
def caption(cls, session: GoalSession, collapsed: bool) -> str:
    return session.execution.toggle_label if session.display.can_control else cls.label

@classmethod
async def apply(cls, session: GoalSession) -> None:
    action = session.execution.toggle
    if action is None:
        session.view.flash(session.display.heading(session.execution))
    else:
        await session.change(action)
```

The optional action is the existing Core control contract, not a test for a
missing block. Completed/idle executions supply no action. Failed/unresolved
executions supply Retry; unresolved authorization remains refused by the ledger.
Do not copy a completed/failure decision or its explanation into this widget.

In GoalSession.activate, after the existing refresh and binding checks, call
`action.enabled(self)` rather than `action.enabled(view.goal_display)`. Keep the
source-binding guards, finally-refresh, rejected-write handling and prompt focus.

### widgets/goal_bar.py

In `_update_goal_text`, acquire `session = self._source().goal_controls` once and
change the existing loop (no new loop or registry):

```python
for control in self.actions:
    action = control.interaction
    control.disabled = not action.enabled(session)
    control.display = action.visible(session)
    control.update(action.caption(session, self.collapsed))
```

The execution watcher must still repaint this loop. This makes a ledger-only
change update both the heading and Retry/Pause/Resume without a stored goal
change. Leave standby animation, collapse/resize, document height and source
lifetime with their existing owners.

### widgets/conversation.py

Use `GoalExecution` in the import and in
`goal_execution: var[GoalExecution] = var(IdleGoalExecution())`.
No adapter, re-export, string state or second reactive status is added.

## Test-family migration

- `tests/support/goals.py`: use GoalExecution annotations. Require an explicit
  execution in ScriptedGoalOwner.owning: a present goal must not silently receive
  IdleGoalExecution. Its scripted successful update must publish the matching
  active goal **and RunnableExecution**, not leave a stale failure execution.
  This fixture supplies a canned external reply; do not implement another Core
  state machine or dispatch by the requested action in it.
- `tests/textual/test_goal_retry.py`: pass
  `DecidedBlockExecution(goal.id, goal.state.block_reason)` to the scripted owner.
  Await the owner snapshot/paint before asserting the Retry caption; do not rely
  on a manual goal assignment while execution is still Idle. After clicking,
  assert RetryGoalAction reached the owner and the owner reply repaints Pause.
  Add the actual defect case through a typed ActiveGoal + failed-attempt snapshot
  (using the existing Core owner/fixture), not a fake stored backend BlockedGoal.
- `tests/textual/test_goal_owner_views.py`: import StandbyExecution,
  RunnableExecution and PausedExecution. Replace the two enum constructors with
  `StandbyExecution(goal.id, targets, ())` and `RunnableExecution(goal.id)`.
  When the scripted source becomes paused, publish its matching PausedExecution;
  do not retain an inconsistent standby execution and make the widget re-decide
  precedence. Restore the matching standby snapshot on return.
- `tests/textual/test_goal_bar_collapse.py`: use StandbyExecution; shown_bar must
  supply RunnableExecution alongside its present GOAL before asserting its
  header and controls. Preserve narrow/collapsed geometry, draft and pulse checks.
- `tests/textual/test_goal_controls.py`: show_goal supplies RunnableExecution for
  the present goal. ProbeInteraction still needs no entry in a hand-made roster;
  it inherits the owner-based caption/predicate protocol.
- Replace stale None assertions for absent goals in
  `tests/native/test_native_goal_objective_edit.py` and
  `tests/textual/test_compaction_turn_events.py` with the existing NoGoal and
  IdleGoalExecution members. The inspected main changes the compaction process
  owner from ParentedProcess to SpawnedProcess, but leaves this stale goal
  assertion unchanged; preserve its newer process-owner change.
  Do not keep None-as-absent state as an alternate contract.
- `tests/native/test_native_goal_bar.py`: its two `execution is not None` waits
  do not establish that a goal was read, because IdleGoalExecution is non-null.
  Wait for the expected owner goal/checkpoint and painted behavior instead.

Search the **whole Toad production and test roots** after applying. There must be
no ThreadGoalExecution, GoalExecutionState, old GoalExecution(enum, ...) calls,
stored-state toggle or external execution-state switch left. Keep legitimate
terminal/scrollback `.execution.state` uses outside this goal family unchanged.

## Start immediately when the paired checkout is available

1. Confirm install 42 includes the actual #2535 fix, use the prescribed checkout
   creation, and inspect current Toad main plus open file owners/PRs. At this
   snapshot the open PRs are #1115/#1121/#1125/#1129/#1131; none owns these goal
   controls. Fetch again then, not a new clone or environment.
2. Apply the owner-and-consumers batch above, including every affected test
   fixture/caller. Import the paired Core worktree through existing dependencies;
   no uv sync, virtualenv, compatibility alias or installed-package mutation.
3. First Toad commit: push, verify ls-remote equals HEAD and open a draft PR.
   Keep both working versions visible; keep Core PR #2529 current with main.
4. Run only the touched goal-family tests, in the background under timeout 300
   and faulthandler_timeout=60. Reuse existing dependencies and persistent owned
   scratch. No local ratchets or full suite. Record collected/executed counts.
5. Qualify the paired app on a copied saved bus/session, with a plain st terminal
   on an isolated display. Exercise the affected native transport and visible
   Toad controls: failed ledger with Active goal -> productive successor -> Active,
   stop and actual worker relaunch without Retry, UNKNOWN never replayed, explicit
   model/owner blocks preserved, and ledger-only changes repainted while the view
   stays open. Include fresh DM/reply with the tab open, tab return, details,
   history, drafts, selection/reader position and controls after a rejected Retry.
6. The merger owns the fresh practice install on the pushed paired heads. Core
   does not install alone. Run the one-shot carry during rollout, retain its
   migrated/preserved counts, then remove it and its one-time test after it ran.
   Record the full delivery claim in Core only when the order's done list holds;
   a Core-only checkpoint is not P448 acceptance.
