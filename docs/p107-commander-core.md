# P107: fresh-main commander ownership

Draft Core PR1164; fresh branch from main `49f187fcb`.
The frozen PR1145, stopped-proof-1, operation5/6, original Run, SDK/wire/input
records and recovery evidence are untouched. No failed/UNKNOWN input replay.

## Shipped source ownership

- `Todo.creator_identity` owns commander authority; `require_commander` checks it.
- Existing `Assignment` owns assignee incarnation, parent and generation. The
  assigned Goal ID is that generation. `Goal.problem` references the Todo.
- Todo owns deadline and strike records. Each `StrikeTodo` records its exact
  struck Assignment, actor and reason. `strike_count` derives only records whose
  Assignment equals the current one, including its generation.
- A strike changes only history/revision. First AND second strikes retain owner
  and task state. Only an explicit commander assignment changes assignment;
  its new generation naturally has zero strikes without deleting old reasons.
- Existing Goal transitions and launch-grant admission check the Todo assignment.
  Assigned Goals cannot silently be replaced, cleared or edited away. A released
  or reassigned old Goal cannot report progress against its former assignment.
- Existing Goal-state polymorphism decides whether an order slot is available.
  `AssignmentChange.remember` owns receipt metadata shared by release/transfer;
  Todo owns assignment mutation. Consumers no longer repeat those decisions.
- TypedTable owns SQL enum constraints and row updates. Handwritten CHECK lists,
  field-difference update rosters and the duplicate busy-timeout setting are gone.
- Normal declared tools expose create, revision-checked changes and order
  assignment through the existing CLI/native tool catalog. No second publisher.

## Deliberately not imported from PR1145

- `GoalProgress`, `NoGoalProgress`, `TextGoalProgress`, `ReportedGoalProgress`,
  `GoalReport`, `Delivery`, `Unfinished`, `WorksInApp`: no second progress/report
  language. Existing Goal state, progress and native turn custody remain owners.
- `GoalAuthor`, `UncommandedGoalAuthor`, `CommandedGoalAuthor`,
  `TransferredGoalAuthor`: no mirrored commander/assignment inside Goal authors.
  The Goal references Todo, which owns those facts.
- `UnassignedTodoAssignment` / `AssignedTodoAssignment`: retain main's existing
  Assignment and revision/CAS lifecycle, rather than replace the family.
- `SourceTodoWhere`, `TodoObservation`: no extra filesystem-location or observer
  authority around the existing Todo store.
- `AcceptanceCheck`: no subprocess executor disguised as an acceptance fact.
  No fleet wrapper or separate notification publisher was imported either.

## Format boundary

No retained Goal progress/mention format conversion or legacy reader was added.
Historical PR1145 converted roots remain frozen and are not accepted implicitly.
Existing pre-P107 Todo databases need a separately reviewed candidate carry:
`tools/cutover/prepare_p107_todos.py` reads the exact prior layout and prepares a
new file using Todo's declarations, preserving old SQL values and supplying only
new declaration-owned defaults. It cannot install/publish or overwrite a
candidate. It has NOT been executed. Delete it after accepted cutover. Codex
alone owns runtime publication and stopped-owner carry review.

## Verification and limits

- 25 existing Todo/Goal/nominal checks passed with the configured asyncio plugin.
  A preceding plugin-disabled batch had one unsupported-async failure; retained
  separately, not a source pass. No tests were added or edited.
- Ruff and whitespace checks pass; final debt ratchet has no positive deltas or
  introduced findings. Source net +73, below the +80 cap.
- Actual `agent-comms` console entrypoint, source import, fresh isolated root:
  create problem -> assign worker A with deadline -> strike1 -> strike2 ->
  explicitly reassign worker B. Both strikes retained A/open; counts were
  1 -> 2 -> 0; both reasons survived reassignment. Old A's progress command was
  refused after reassignment. No provider input, new native session or original
  saved-history mutation was involved.
- Source/evidence: `.artifacts/p107-commander/`; private CLI state remains there
  as development evidence. An initial wrong `python -m agent_comms` invocation
  failed before tool dispatch; the real console entrypoint was then exercised.
- Not merged, installed or native/UI completion accepted. The candidate carry
  and actual installed commander/native journey remain review/acceptance work.
  Native TypeScript dispatch is dynamic and outside the Python AST census.
