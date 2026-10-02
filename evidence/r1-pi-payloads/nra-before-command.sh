#!/bin/sh
set -eu
cd /home/ts/wt/comms-refactor-r1-pi-payloads-20260928
exec timeout 165 /home/ts/code/projects/nominal-refactor-advisor/.venv/bin/python -m nominal_refactor_advisor src/agent_comms/pi_events.py src/agent_comms/pi_commands.py src/agent_comms/pi_rpc.py src/agent_comms/turn_usage.py --context-root src/agent_comms --parse-workers 1 --analysis-workers 1 --no-cache --scan-budget-seconds 140 --json --json-payload loop
