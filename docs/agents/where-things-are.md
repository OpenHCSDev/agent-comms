# Where things are

One page for finding every piece of agent-comms on a machine: the code, the bus, the commander, the Claude Code wiring and the connector. Paths are for user `ts`; the layout is the same on ts-gaming, the desktop and the laptop.

## Code

| What | Where |
|---|---|
| The repository | `~/code/projects/agent-comms` (main) |
| Its Python environment | `~/code/projects/agent-comms/.venv` |
| Installed commands (`agent-comms`, `agent-comms-commander`, `agent-comms-claude-bridge`, `toad`) | `~/.local/bin/`, each running the repo's `.venv` |
| Toad and the other packages it runs with | `~/code/projects/agent-comms/stack`, synced with `uv sync --locked` after each pull |
| Unit worktrees, one per order | `/home/ts/wt/<unit>--P<n>` |
| Unit home checkouts | `~/code/projects/agent-comms-wt/<unit>` |

## The bus (Core's durable state)

Everything lives in `~/.agent-comms/`:

- `registry.json`: every thread and its owner.
- `bus.jsonl`: the message log.
- `todos.sqlite3`: problems and orders.
- `goal_history.sqlite3`: Goals and their history.
- `carries.json`: which upgrade steps have run on this bus. The steps are in `tools/cutover/`; `tools/upgrade_bus.py` runs them in order.

Back up with `cp -a ~/.agent-comms <backup path>` before any upgrade step, and stop the writers first.

## The commander

The commander isn't built into Core yet; `plans/commander-native-plan.md` moves it there.

| What | Where |
|---|---|
| Its code | `src/agent_comms/commander/`: `order.py` (orders, strikes, done, hunts), `rules.py` and `rules/` (the rule review and the rule texts), `rotate.py` (the rotation tick), `send_guard.py`, `judge.py` |
| Its state | `~/.local/state/agent-comms/commander/`: `orders/`, `problems.json`, `deadlines.json`, `done.jsonl`, `closed.jsonl`, `hunts.jsonl`, `artifacts/`, `forks/` (the beats that count as live forks), `activity.log` |
| How to run it | `src/agent_comms/skills/comms-commander/SKILL.md` |
| The game it plays | `docs/agents/commander-game.md` |
| Where it is going | `plans/commander-native-plan.md` |
| Who owns which work, by machine | `plans/handoff-ts-gaming-commander.md` |

## Claude Code wiring

| What | Where |
|---|---|
| Skills | `~/.claude/skills/comms` and `~/.claude/skills/comms-commander`, which link into `src/agent_comms/skills/` |
| Hooks, in `~/.claude/settings.json` | `agent-comms-commander send-guard` refuses a raw `agent-comms send`; `~/.claude/hooks/tests-in-background.py` refuses a test run that blocks |
| Bus to Claude Code bridge state | `~/.local/state/agent-comms/claude-bridge-<unit>/` |

## The claude.ai connector (ts-gaming)

| What | Where |
|---|---|
| Code | `src/agent_comms/connector.py` (PR #1546) |
| Server | `127.0.0.1:18766/mcp` on ts-gaming; nothing else listens |
| Public URL, added in claude.ai under Connectors | `https://ts-gaming.tail553874.ts.net/mcp`, through Tailscale Funnel |
| Grants, stored as token digests | `~/.local/state/agent-comms/connector/` |
| Approve a login | `agent-comms-connector approve <id>` (installed once #1546 merges); `pending` lists waiting requests, `revoke` drops all grants |
| Switch the public URL off | `tailscale funnel --https=443 off` |
| Its plan | `plans/comms-claude-connector-pr.md` |
