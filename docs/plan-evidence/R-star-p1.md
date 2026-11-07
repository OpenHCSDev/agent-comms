# R* — first P1 batch and the remaining ownership questions

Source revision: `51d17a5cb1db130bbeb707e5448b5daa92670c54`.

This is a corrected, retrospective trace of the first batch, not evidence that the
trace preceded #4321. That move already landed before P0 was established correctly.
The retained static map is `module-map-imports-51d17a5cb.json` beside this file.

## Scope and counts

The map parses tracked Python under `src`, `tests` and `tools` from that Git tree.
It records direct absolute and resolved relative imports, including imports inside
functions, plus explicit module imports. Dynamic imports, reflective access and
consumers in other repositories are unscored, not declared absent.

| Original module | Lines (LF) | Top-level classes | Production importer files | Distinct names directly imported by production code |
|---|---:|---:|---:|---:|
| `src/agent_comms/todos.py` | 7134 | 254 | 38 | 97 |
| `src/agent_comms/tools.py` | 1795 | 116 | 6 | 7 |
| `src/agent_comms/standing_rules.py` | 1555 | 51 | 12 | 22 |

These are dependency observations, not ownership proofs. The earlier claim that
seven named imports exhaust `tools.py`'s consumers was wrong: tests import other
classes, and `tool_declaration.py:139` acquires the module itself for registration.
The previous 39/12/13 importer counts came from a text heuristic, not this resolved
import map. The previous "family roots" column was a base-name heuristic and is
withdrawn; it did not establish registry roots or walk the MRO.

## First batch: goal-tool owner

Required destination: `agent_comms.transport.tools.goals`. The batch preserves
these fifteen identities and their bodies; it does not merge declarations merely
because their current implementations agree.

All declaration locations below are in `src/agent_comms/tools.py` at the pinned
revision. The machine map records their bases and each directly declared method.

| Line | Declaration | Shared provider / distinct contribution |
|---:|---|---|
| 232 | `OwnGoalPublication` | `ToolResult`; owns the publication-kind projection |
| 243 | `GoalSet` | `OwnGoalPublication`; goal and instruction fields |
| 249 | `GoalProgress` | `OwnGoalPublication` and `GoalSnapshot`; execution field |
| 254 | `GoalUpdated` | `OwnGoalPublication`; goal field |
| 259 | `TeammateGoalChanged` | `ToolResult`; peer publication, not caller publication |
| 268 | `OwnGoal` | `NullMember`; caller identity and runtime actor |
| 281 | `TeammateGoal` | `SingleFieldValue`; named peer and peer actor |
| 295 | `GoalChangeTool` | `ThreadToolRequest`; shared target/update/current operations |
| 945 | `CommsSetGoalTool` | `GoalChangeTool`; sets a fresh goal |
| 961 | `CommsGoalTool` | `ThreadToolRequest`; progress and transition of the caller's goal |
| 1016 | `CommsResumeGoalTool` | `GoalChangeTool`; paused-goal resume semantics |
| 1047 | `CommsEditGoalTool` | `GoalChangeTool`; text edit retaining goal identity |
| 1070 | `CommsClearGoalTool` | `GoalChangeTool`; clear semantics |
| 1090 | `CommsPauseGoalTool` | `GoalChangeTool`; pause semantics |
| 1112 | `CommsGoalHistoryTool` | `ThreadReadTool`; history read |

### Declaration → implementation → projection

- `GoalChangeTool.update/current` at `tools.py:304–308` uses the target's
  `thread_name` and `actor`; `OwnGoal` and `TeammateGoal` own those differences.
  The verbs at `:945`, `:1016`, `:1047`, `:1070`, `:1090` call that shared owner.
  `GoalSet`/`GoalUpdated` are projected through the selected target's `published`
  method, so a peer edit does not masquerade as the caller's own publication.
- `CommsGoalTool.apply` at `tools.py:998–1012` validates claims through
  `progress_claims.py:30`, builds its transition using `goal_actions` declarations,
  and calls `Goals.update_goal_snapshot` at `goal_management.py:343`. Its result is
  built by `GoalSnapshot.from_snapshot` at `goal_presentation.py:295`; the
  `GoalProgress` subclass retains the `goal_execution` wire-name annotation.
- `OwnGoalPublication.publication_kind` at `tools.py:238–239` encodes the
  `PublishedGoalToolDetails` declaration from `pi_payloads`, rather than inventing
  another publication tag in the moved module.
- `CommsGoalHistoryTool.apply` at `tools.py:1119–1120` calls
  `Goals.recorded_history` at `goal_management.py:269`.
- `ThreadReadTool` and `ThreadToolRequest` at `tool_request.py:306/312` remain the
  caller/read capability providers. The move does not replace their implementations.

### Direct consumers of the selected declarations

These are actual import locations, not counts printed as if they were line numbers:

| Consumer | Imported selected declarations |
|---|---|
| `src/agent_comms/execution/owned_turn.py:67` | `CommsSetGoalTool` |
| `tests/test_goal_block_reason.py:25` | `CommsGoalTool` |
| `tests/test_goal_execution.py:48` | `CommsGoalHistoryTool` |
| `tests/test_goal_history.py:35` | `CommsGoalHistoryTool` |
| `tests/test_goal_input_review.py:26` | `CommsGoalTool` |
| `tests/test_goal_owner_pause.py:16` | `CommsGoalTool`, `CommsResumeGoalTool` |
| `tests/test_goal_reply_lock_scope.py:34` | `CommsGoalTool` |
| `tests/test_report_goal_evidence.py:18` | `CommsGoalTool` |
| `tests/test_turn_goal_permission.py:12` | `GoalSet` |

`OwnedTurn.tool_ended` at `execution/owned_turn.py:125–132` uses the set-goal
member's declared name to recognize a successful goal publication and reserve its
origin usage. That dependency follows the moved class, not a copied name string.
The test imports above must also follow the owner. #4321 initially missed some;
that was a consumer-closure failure, not proof the consumers did not exist.

### Registration is a consumer too

`ToolDeclaration._member_declarations` at `tool_declaration.py:133–141` acquires
`order_tools` and `tools`. The selected native tool classes register through the
existing family/metaclass machinery when the module is acquired. Moving them
requires acquisition of their new module as well as updating named imports.
A successful direct import alone does not establish that retained wire records
can still acquire their tool declaration in a fresh process.

## OPEN before subsequent batches

- `todos.py`'s 254 declarations have not been traced individually. The earlier
  proposed planning/execution/transport partition is a hypothesis, not an accepted
  ownership relation; import counts do not choose it. In particular, the claim
  that delivery evidence has "no order vocabulary" was not established.
- `ProgressRead` at `todos.py:1647` assigns `_progress` in `__init__`; the earlier
  assertion that it declares no fields was false. Its ownership remains OPEN.
- Leaving `Todo`/`TodoStore` in `todos.py` until a later phase contradicts P1's
  requirement that the old path disappear. The final owner partition must close
  that requirement; deferral cannot silently stand in for completion.
- `standing_rules.py` has twelve production importers in the pinned static map,
  but consumer location alone does not prove every declaration belongs in `team/`.
  Its split still needs the declaration/implementation/consumer trace P1 requires.
- Dynamic and cross-repository consumers remain OPEN and unscored. Any deletion or
  identity merge needs additional closure over its admitted callers; this map
  does not certify arbitrary future imports or behaviour.
