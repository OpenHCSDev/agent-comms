#!/bin/sh
set -eu
cd /home/ts/wt/comms-refactor-c0-operations-20260928
exec timeout 165 /home/ts/code/projects/nominal-refactor-advisor/.venv/bin/python -m nominal_refactor_advisor src/agent_comms/comms.py src/agent_comms/messaging.py src/agent_comms/agent_activity.py src/agent_comms/channel_management.py src/agent_comms/goal_management.py src/agent_comms/owner_lifecycle.py src/agent_comms/thread_management.py src/agent_comms/transcripts.py src/agent_comms/history_views.py src/agent_comms/collaboration_ledger.py --context-root src/agent_comms --parse-workers 1 --analysis-workers 1 --no-cache --scan-budget-seconds 140 --json --json-payload loop
