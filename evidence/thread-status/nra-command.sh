#!/bin/sh
# Run from this persistent worktree; internal budget overrides NRA's 20-second default.
exec timeout 165 /home/ts/code/projects/nominal-refactor-advisor/.venv/bin/python \
 -m nominal_refactor_advisor \
 src/agent_comms/thread_status.py src/agent_comms/declarations.py \
 src/agent_comms/registry_document.py src/agent_comms/registration.py \
 src/agent_comms/operations.py src/agent_comms/supervised_cutover.py \
 src/agent_comms/goal_failure_observation.py \
 --context-root src/agent_comms --parse-workers 1 --analysis-workers 1 \
 --no-cache --scan-budget-seconds 140 --json --json-payload loop
