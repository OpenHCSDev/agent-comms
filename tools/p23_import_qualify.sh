#!/usr/bin/env bash
# Original callbacks, source-stashed regression, then their real source consumers.
set -u
scratch=/home/ts/.cache/agent-scratch/comms-friction
python=/home/ts/code/projects/agent-comms/.venv/bin/python

for first in agent_comms.agent_runtime agent_comms.import_history_source agent_comms.imported_conversation agent_comms.importing agent_comms.execution.native_entries agent_comms.cli_bridges.history agent_comms.registry.thread_management; do
    printf 'Cold entry %s\n' "$first"
    PYTHONPATH=src timeout -s INT 30 "$python" tests/goal_publication_probe.py "$first" || exit $?
done > "$scratch/p23-import-contract-cold.log" 2>&1
printf '0\n' > "$scratch/p23-import-contract-cold.exit"

# Keep the new regression; the source-control owner hides and restores only source.
PYTHONPATH=src bash tools/p23_source_control.sh "$scratch/p23-import-contract-source-before" \
    P23-import-contract-source-before-2c816b6c0 \
    timeout -s INT 90 "$python" -m pytest -q -p no:xdist -o faulthandler_timeout=60 \
    tests/test_goal_runtime_annotations.py -k test_original_import_factory_callbacks_use_actual_source_capabilities
before=$?
read -r restored < "$scratch/p23-import-contract-source-before-restore.exit" || exit $?
if test "$restored" -ne 0; then exit "$restored"; fi
if test "$before" -ne 1; then
    printf 'Expected a collected callback failure, got exit %s\n' "$before"
    exit 1
fi
grep -q "NameError: name 'History' is not defined" "$scratch/p23-import-contract-source-before.log" || exit 1
grep -q '1 failed' "$scratch/p23-import-contract-source-before.log" || exit 1

# All files naming the moved values/capabilities and changed registry self-name callbacks.
PYTHONPATH=src timeout -s INT 300 "$python" -m pytest -q -p no:xdist -o faulthandler_timeout=60 \
    tests/test_bulk_lifetimes.py tests/test_busy_history_loading.py tests/test_cli_bridges.py \
    tests/test_importing.py tests/test_native_failure_recovery.py tests/test_thread_listing.py \
    tests/test_todo_stories.py tests/test_unreadable_history_observation.py \
    tests/test_acp_private_nk_delivery.py tests/test_auto_title.py tests/test_goal_history.py \
    tests/test_goals.py tests/test_holder_standing_rules.py tests/test_operations.py \
    tests/test_optional_awareness_projection.py tests/test_relationships.py \
    tests/test_team_notifications.py tests/test_thread_names.py tests/test_thread_order.py \
    tests/test_cli.py tests/test_goal_runtime_annotations.py -k 'not resolve_cold' \
    > "$scratch/p23-import-contract-consumers.log" 2>&1
result=$?
printf '%s\n' "$result" > "$scratch/p23-import-contract-consumers.exit"
exit "$result"
