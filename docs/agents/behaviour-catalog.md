# Behaviour catalog

Generated from `agent_comms.behaviour_patterns` by
`python -m agent_comms.behaviour_patterns > docs/agents/behaviour-catalog.md`; do not edit by hand.

Each behaviour failure pattern is declared once in Core with its signature (what it looks like in state, readable without trusting a report) and the existing checks that enforce it. Patterns in code, rather than in behaviour, are the refactor-audit skill's catalog (`refactor-audit/references/patterns/README.md`).

The [Codex postmortem](https://github.com/OpenHCSDev/toad/blob/main/docs/agents/CODEX-POSTMORTEM.md) names ten failures; each pattern cites the ones it checks:

- [Failure 1](https://github.com/OpenHCSDev/toad/blob/main/docs/agents/CODEX-POSTMORTEM.md): no pattern checks it yet
- [Failure 2](https://github.com/OpenHCSDev/toad/blob/main/docs/agents/CODEX-POSTMORTEM.md): no pattern checks it yet
- [Failure 3](https://github.com/OpenHCSDev/toad/blob/main/docs/agents/CODEX-POSTMORTEM.md): no pattern checks it yet
- [Failure 4](https://github.com/OpenHCSDev/toad/blob/main/docs/agents/CODEX-POSTMORTEM.md): `uncounted_check`
- [Failure 5](https://github.com/OpenHCSDev/toad/blob/main/docs/agents/CODEX-POSTMORTEM.md): no pattern checks it yet
- [Failure 6](https://github.com/OpenHCSDev/toad/blob/main/docs/agents/CODEX-POSTMORTEM.md): no pattern checks it yet
- [Failure 7](https://github.com/OpenHCSDev/toad/blob/main/docs/agents/CODEX-POSTMORTEM.md): `board_disagrees`, `uncounted_check`
- [Failure 8](https://github.com/OpenHCSDev/toad/blob/main/docs/agents/CODEX-POSTMORTEM.md): `plain_text`, `undeclared_audience`
- [Failure 9](https://github.com/OpenHCSDev/toad/blob/main/docs/agents/CODEX-POSTMORTEM.md): no pattern checks it yet
- [Failure 10](https://github.com/OpenHCSDev/toad/blob/main/docs/agents/CODEX-POSTMORTEM.md): no pattern checks it yet

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

- tool refusal in `agent_comms.commander.send_guard.TacticalCommand`

Learned from: P171 (cba7b09c3)

## plain_text

A commander order, strike, roast or directive whose words outside cited spans match the declared jargon list in `PlainText`.

Checked by:

- verb refusal in `agent_comms.commander.order.refuse`
- tool refusal in `agent_comms.connector.DirectorCaller.require_directive`

Learned from: [comms-commander rule](../../src/agent_comms/skills/comms-commander/SKILL.md) "Hate jargon."; [Codex postmortem, failure 8](https://github.com/OpenHCSDev/toad/blob/main/docs/agents/CODEX-POSTMORTEM.md)

## undeclared_audience

A send to a channel, which reaches many threads, names no thread as its audience.

Checked by:

- tool refusal in `agent_comms.channel_targets.ChannelTarget.addressed`

Learned from: commit dc4fb3bfa; [Codex postmortem, failure 8](https://github.com/OpenHCSDev/toad/blob/main/docs/agents/CODEX-POSTMORTEM.md)

## repeated_strike

A second strike for one miss: the same actor, owner and reason as a live strike, or a send-back strike on an order already struck since it was sent.

Checked by:

- tool refusal in `agent_comms.todos.StrikeTodo.repeats`
- verb refusal in `agent_comms.commander.order.send`

Learned from: P184 (7af14a520)

## strike_in_error

A strike recorded in error is deleted, or voided while not live, instead of a void recorded beside it with a plain reason.

Checked by:

- verb refusal in `agent_comms.commander.order.unstrike`
- tool refusal in `agent_comms.todos.VoidStrikeTodo.apply`

Learned from: P47 (15d435152)

## uncounted_check

An order whose check line does not read `repo :: command -> integer`, prints no integer, or already prints its target before any work.

Checked by:

- verb refusal in `agent_comms.goals.OrderCheck.parse`
- verb refusal in `agent_comms.goals.NoIntegerPrinted`
- verb refusal in `agent_comms.commander.order.correct_check`

Learned from: P114 (d889df612); [Codex postmortem, failure 4](https://github.com/OpenHCSDev/toad/blob/main/docs/agents/CODEX-POSTMORTEM.md); [Codex postmortem, failure 7](https://github.com/OpenHCSDev/toad/blob/main/docs/agents/CODEX-POSTMORTEM.md)

## strike_on_named_problem

A strike or hunt recorded on a problem other than the one given as its argument, such as one moved by a problem number inside its reason.

Checked by:

- verb refusal in `agent_comms.commander.order.strike`
- verb refusal in `agent_comms.commander.order.hunt`

Learned from: P194 (b26843727); [comms-commander rule](../../src/agent_comms/skills/comms-commander/SKILL.md) "Strikes, then fire."
