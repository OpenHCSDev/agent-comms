#!/bin/sh
# Run from this worktree. Explicit internal and shell budgets avoid NRA's default 20s timeout.
exec timeout 165 /home/ts/code/projects/nominal-refactor-advisor/.venv/bin/python \
  -m nominal_refactor_advisor \
  src/agent_comms/relationships.py src/agent_comms/passive_channel_awareness.py \
  src/agent_comms/relationship_migration.py \
  --context-root src/agent_comms --parse-workers 1 --analysis-workers 1 \
  --no-cache --scan-budget-seconds 140 --json --json-payload loop
