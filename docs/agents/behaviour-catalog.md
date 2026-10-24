# Behaviour catalog

Generated from `agent_comms.behaviour_patterns` by
`python -m agent_comms.behaviour_patterns > docs/agents/behaviour-catalog.md`; do not edit by hand.

Each behaviour failure pattern is declared once in Core with its signature (what it looks like in state, readable without trusting a report) and the existing checks that enforce it. Patterns in code, rather than in behaviour, are the refactor-audit skill's catalog (`refactor-audit/references/patterns/README.md`).

A pending check records a known gap, not enforcement. Adding a pattern means adding one `BehaviourPattern` subclass; membership and this document derive from those classes. `tests/test_behaviour_catalog.py` rejects hand edits.

The [Codex postmortem](https://github.com/OpenHCSDev/toad/blob/main/docs/agents/CODEX-POSTMORTEM.md) and October 19 commander incidents record these failures; coverage below derives from the patterns that cite them:

- Failure 1, It rewrote the framework instead of questioning its own design: no pattern cites it yet
- Failure 2, It added states instead of removing them: no pattern cites it yet
- Failure 3, It wrote down the right principle and then violated it: no pattern cites it yet
- Failure 4, It measured the wrong thing and reported it as progress: `uncounted_check`, `wrong_measure`
- Failure 5, It redefined "the real app" until something passed: no pattern cites it yet
- Failure 6, It ignored direct orders: no pattern cites it yet
- Failure 7, It described its intentions as its accomplishments: `board_disagrees`, `uncounted_check`
- Failure 8, It drowned the work in ceremony: `plain_text`
- Failure 9, It merged over its own checks and broke things in circles: no pattern cites it yet
- Failure 10, It invented rules and blamed them on Tristan: no pattern cites it yet
- Failure 11, It hid known failures until asked: no pattern cites it yet
- Failure 12, It turned deadline ceilings into appointments: no pattern cites it yet
- Failure 13, It routed around gates instead of obeying them: no pattern cites it yet
- Failure 14, It ignored alarms because its board had rotted: no pattern cites it yet
- Failure 15, It recited rules instead of following them: no pattern cites it yet
- Failure 16, It deleted state without checking who was using it: no pattern cites it yet
- Failure 17, It left every unit blocked with no alarm: no pattern cites it yet
- Failure 18, It installed a change without the cutover the change required: no pattern cites it yet
- Failure 19, It ordered units to diagnose and choose the shape: no pattern cites it yet
- Failure 20, Its own orders stopped the units they were sent to: no pattern cites it yet
- Failure 21, It reported a state its evidence did not show: no pattern cites it yet
- Failure 22, It left a unit's question unanswered for hours: no pattern cites it yet
- Failure 23, It held plan work for hours while nothing reached main: `plan_stalled`
- Failure 24, It kept a dead copy and called it a legacy case: no pattern cites it yet
- Failure 25, It noted slop for later instead of deleting it: no pattern cites it yet
- Failure 26, It restated an owner's rule in a second file: no pattern cites it yet
- Failure 27, It gave an order from a guess about code it had not read: no pattern cites it yet
- Failure 28, It called a workaround a fix: no pattern cites it yet
- Failure 29, It softened slop into neutral words: no pattern cites it yet
- Failure 30, It waited on checks that test nothing it changed: no pattern cites it yet

## plan_idle

A plan phase whose earlier phases are all done is not done itself, and no open order held by a unit serves it.

Checked by:

- standing rule `plan_idle` (`agent_comms.commander.standing.PlanIdle`)

Learned from: P83 (34ba68c81)

## plan_stalled

A plan phase whose earlier phases are all done is held by a unit, and main still lacks it two hours after the phase was first ordered.

Checked by:

- standing rule `plan_stalled` (`agent_comms.commander.standing.PlanStalled`)

Learned from: Failure 23: October 21: every virtual bus step had a holder and none merged for 22 hours, because the steps only land together and nobody owned integrating them; Tristan caught it (claude-ai #comms 2dde16189509).

## board_disagrees

An open order's progress reads no commit while a pull request linked to it has merged.

Checked by:

- standing rule `board_disagrees` (`agent_comms.standing_rules.BoardDisagrees`)

Learned from: P170 (c9c0c04db); Failure 7: October 9 postmortem: 350 commit subjects in five days claimed facts were "derived", "canonical" or "authoritative" while the same constant remained hand-written in three places; commit titles supplied no frame-time proof.

## plain_text

A commander order, strike, roast or directive whose words outside cited spans match the declared jargon list in `PlainText`.

Checked by:

- check in `agent_comms.todos.PlainText`

Learned from: [comms-commander rule](../../src/agent_comms/skills/comms-commander/SKILL.md) "Hate jargon."; Failure 8: October 9 postmortem: repeated permission exchanges and reports replaced delivery; one checkout reached 8.7 GB, ratchets ran dozens of times and over 200,000 evidence lines were committed in one night.

## repeated_strike

A send-back strike on an order already struck since it was sent.

Checked by:

- check in `agent_comms.todos.Todo.struck_on_order`

Learned from: P184 (7af14a520)

## strike_in_error

A strike recorded in error is deleted, or voided while not live, instead of a void recorded beside it with a plain reason.

Checked by:

- check in `agent_comms.todos.TodoCommand.apply`

Learned from: P47 (15d435152)

## uncounted_check

An order whose check line does not read `repo :: command -> integer`, prints no integer, or already prints its target before any work.

Checked by:

- check in `agent_comms.commander.order.CheckRequest.parse`
- check in `agent_comms.child_process.NoIntegerPrinted.unproven`
- check in `agent_comms.child_process.CountPrinted.unproven`

Learned from: P114 (d889df612); Failure 4: October 9 postmortem: 451 measurement-related commits and 716 evidence files yielded only three comparable files; headline 11.7 ms median timings measured terminal writes, not total frames.; Failure 7: October 9 postmortem: 350 commit subjects in five days claimed facts were "derived", "canonical" or "authoritative" while the same constant remained hand-written in three places; commit titles supplied no frame-time proof.

## strike_on_named_problem

A strike recorded on a problem other than the one given as its argument, such as one moved by a problem number inside its reason. Hunts are commander records, not strikes on a problem.

Checked by:

- check in `agent_comms.todos.TodoStore.apply`

Learned from: P194 (b26843727); [comms-commander rule](../../src/agent_comms/skills/comms-commander/SKILL.md) "Two strikes on one task take it away."

## report_instead_of_state

A strike or send-back whose reason cites no state the record can check.

Checked by:

- not enforced here: P198 (#1744)

Learned from: seed catalog pattern 1 (claude-ai message 1c8729ba0ed7)

## wrong_measure

A done report that names its own measure instead of what its order's Check printed.

Checked by:

- not enforced here: P197 (#1733)

Learned from: seed catalog pattern 4 (claude-ai message 1c8729ba0ed7); Failure 4: October 9 postmortem: 451 measurement-related commits and 716 evidence files yielded only three comparable files; headline 11.7 ms median timings measured terminal writes, not total frames.

## commander_overloaded

The commander's unread backlog is over the limit while no second commander is forked to share it.

Checked by:

- not enforced here: P199 (#1750, closed without merge)

Learned from: seed catalog pattern 15 (claude-ai message 1c8729ba0ed7)

## dead_provider

A unit's latest turn ended in a usage or credit limit on its own model and the unit still runs on that model.

Checked by:

- not enforced here: P200 (#1746, closed without merge)

Learned from: seed catalog pattern 16 (claude-ai message 1c8729ba0ed7)

## unowned_work

Agent-comms work runs under no open order, or on a runtime that publication replaced.

Checked by:

- not enforced here: P201 (#1739)

Learned from: seed catalog pattern 18 (claude-ai message 1c8729ba0ed7)
