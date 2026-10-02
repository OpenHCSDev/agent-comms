#!/bin/sh
set -eu
cd /home/ts/wt/comms-refactor-s7-wire-log-20260928
exec timeout 165 /home/ts/code/projects/nominal-refactor-advisor/.venv/bin/python -m nominal_refactor_advisor src/agent_comms/message_bus.py src/agent_comms/wire_log.py src/agent_comms/publisher.py src/agent_comms/private_bus_checkpoint.py --context-root src/agent_comms --parse-workers 1 --analysis-workers 1 --no-cache --scan-budget-seconds 140 --json --json-payload loop
