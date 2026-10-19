# Behaviour catalog

Generated from `agent_comms.behaviour_patterns` by
`python -m agent_comms.behaviour_patterns > docs/agents/behaviour-catalog.md`; do not edit by hand.

Each behaviour failure pattern is declared once in Core with its signature (what it looks like in state, readable without trusting a report) and the existing checks that enforce it. Patterns in code, rather than in behaviour, are the refactor-audit skill's catalog (`refactor-audit/references/patterns/README.md`).

A pending check records a known gap, not enforcement. Adding a pattern means adding one `BehaviourPattern` subclass; membership and this document derive from those classes. `tests/test_behaviour_catalog.py` rejects hand edits.

The [Codex postmortem](https://github.com/OpenHCSDev/toad/blob/main/docs/agents/CODEX-POSTMORTEM.md) names these failures; each pattern cites the ones it checks:

- [Failure 1](https://github.com/OpenHCSDev/toad/blob/main/docs/agents/CODEX-POSTMORTEM.md), It rewrote the framework instead of questioning its own design: no pattern cites it yet
- [Failure 2](https://github.com/OpenHCSDev/toad/blob/main/docs/agents/CODEX-POSTMORTEM.md), It added states instead of removing them: no pattern cites it yet
- [Failure 3](https://github.com/OpenHCSDev/toad/blob/main/docs/agents/CODEX-POSTMORTEM.md), It wrote down the right principle and then violated it: no pattern cites it yet
- [Failure 4](https://github.com/OpenHCSDev/toad/blob/main/docs/agents/CODEX-POSTMORTEM.md), It measured the wrong thing and reported it as progress: `uncounted_check`, `wrong_measure`
- [Failure 5](https://github.com/OpenHCSDev/toad/blob/main/docs/agents/CODEX-POSTMORTEM.md), It redefined "the real app" until something passed: no pattern cites it yet
- [Failure 6](https://github.com/OpenHCSDev/toad/blob/main/docs/agents/CODEX-POSTMORTEM.md), It ignored direct orders: no pattern cites it yet
- [Failure 7](https://github.com/OpenHCSDev/toad/blob/main/docs/agents/CODEX-POSTMORTEM.md), It described its intentions as its accomplishments: `board_disagrees`, `uncounted_check`
- [Failure 8](https://github.com/OpenHCSDev/toad/blob/main/docs/agents/CODEX-POSTMORTEM.md), It drowned the work in ceremony: `plain_text`, `undeclared_audience`
- [Failure 9](https://github.com/OpenHCSDev/toad/blob/main/docs/agents/CODEX-POSTMORTEM.md), It merged over its own checks and broke things in circles: no pattern cites it yet
- [Failure 10](https://github.com/OpenHCSDev/toad/blob/main/docs/agents/CODEX-POSTMORTEM.md), It invented rules and blamed them on Tristan: no pattern cites it yet

## plan_idle

A plan phase whose earlier phases are all done is not done itself, and no open order held by a unit serves it.

Checked by:

- standing rule `plan_idle` (`agent_comms.standing_rules.PlanIdle`)

Learned from: P83 (34ba68c81)

## board_disagrees

An open order's progress reads no commit while a pull request linked to it has merged.

Checked by:

- standing rule `board_disagrees` (`agent_comms.standing_rules.BoardDisagrees`)

Learned from: P170 (c9c0c04db); [Codex postmortem, failure 7](https://github.com/OpenHCSDev/toad/blob/main/docs/agents/CODEX-POSTMORTEM.md)

## tactical_command

The commander's own session runs an install, maintenance, connector restart or carry command instead of ordering a unit to.

Checked by:

- check in `agent_comms.commander.send_guard.TacticalCommand.install_refusals`

Learned from: P171 (cba7b09c3)

## plain_text

A commander order, strike, roast or directive whose words outside cited spans match the declared jargon list in `PlainText`.

Checked by:

- check in `agent_comms.todos.OrderWords.refused`

Learned from: [comms-commander rule](../../src/agent_comms/skills/comms-commander/SKILL.md) "Hate jargon."; [Codex postmortem, failure 8](https://github.com/OpenHCSDev/toad/blob/main/docs/agents/CODEX-POSTMORTEM.md)

## undeclared_audience

A send to a channel, which reaches many threads, names no thread as its audience.

Checked by:

- check in `agent_comms.channel_targets.ChannelTarget.addressed`

Learned from: commit dc4fb3bfa; [Codex postmortem, failure 8](https://github.com/OpenHCSDev/toad/blob/main/docs/agents/CODEX-POSTMORTEM.md)

## repeated_strike

A second storage strike with the same actor, owner and reason as a live strike, or a send-back strike on an order already struck since it was sent.

Checked by:

- check in `agent_comms.todos.StorageStrikeTodo.repeats`
- check in `agent_comms.todos.Todo.struck_on_order`

Learned from: P184 (7af14a520)

## strike_in_error

A strike recorded in error is deleted, or voided while not live, instead of a void recorded beside it with a plain reason.

Checked by:

- check in `agent_comms.todos.VoidStrikeTodo.apply`

Learned from: P47 (15d435152)

## uncounted_check

An order whose check line does not read `repo :: command -> integer`, prints no integer, or already prints its target before any work.

Checked by:

- check in `agent_comms.goals.OrderCheck.parse`
- check in `agent_comms.child_process.NoIntegerPrinted.unproven`
- check in `agent_comms.child_process.CountPrinted.unproven`

Learned from: P114 (d889df612); [Codex postmortem, failure 4](https://github.com/OpenHCSDev/toad/blob/main/docs/agents/CODEX-POSTMORTEM.md); [Codex postmortem, failure 7](https://github.com/OpenHCSDev/toad/blob/main/docs/agents/CODEX-POSTMORTEM.md)

## strike_on_named_problem

A strike recorded on a problem other than the one given as its argument, such as one moved by a problem number inside its reason. Hunts are commander records, not strikes on a problem.

Checked by:

- check in `agent_comms.todos.TodoStore.apply`

Learned from: P194 (b26843727); [comms-commander rule](../../src/agent_comms/skills/comms-commander/SKILL.md) "Strikes, then fire."

## report_instead_of_state

A strike or send-back whose reason cites no state the record can check.

Checked by:

- not enforced here: P198 (#1744)

Learned from: seed catalog pattern 1 (claude-ai message 1c8729ba0ed7)

## wrong_measure

A done report that names its own measure instead of what its order's Check printed.

Checked by:

- not enforced here: P197 (#1733)

Learned from: seed catalog pattern 4 (claude-ai message 1c8729ba0ed7); [Codex postmortem, failure 4](https://github.com/OpenHCSDev/toad/blob/main/docs/agents/CODEX-POSTMORTEM.md)

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
