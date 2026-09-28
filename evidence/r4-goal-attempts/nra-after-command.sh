#!/bin/sh
set -eu
cd /home/ts/wt/comms-refactor-r4-goal-attempts-20260928
exec timeout 165 /home/ts/code/projects/nominal-refactor-advisor/.venv/bin/python -m nominal_refactor_advisor src/agent_comms/goal_generation.py src/agent_comms/goal_attempts.py src/agent_comms/goal_attempt_phase.py src/agent_comms/goal_failure_observation.py --context-root src/agent_comms --parse-workers 1 --analysis-workers 1 --no-cache --scan-budget-seconds 140 --json --json-payload loop
