# Executed local commands

Working directory: `/home/ts/wt/comms-c0-declarations-20260928`.
Common test invocation below (`PY` is shorthand in this record, not a separate environment):

```sh
PYTHONPATH="$PWD/src" timeout 60 \
 /home/ts/wt/comms-historical-views-20260927/.test-venv/bin/python \
 -m pytest -o addopts='' --basetemp .c0-artifacts/<owned-case> <files> -q
```

- core: tests/test_declarations.py tests/test_s4_ownership.py tests/test_channels.py tests/test_saved_view_boundary.py tests/test_registration_components.py tests/test_thread_identity.py
- bus-history: tests/test_envelope_bus_integration.py tests/test_private_bus_checkpoint.py tests/test_dm_scoped_ack.py tests/test_read_ledger.py tests/test_historical_views.py tests/test_bus_page_index.py tests/test_activity_index.py tests/test_viewer_snapshot_index.py tests/test_goal_history.py tests/test_s4_ownership.py
- correction: tests/test_private_bus_checkpoint.py::test_failed_marker_binding_does_not_promote_checkpoint tests/test_s4_ownership.py
- native-consumers (165s bound): tests/test_turn_runner.py tests/test_input_drain_native.py tests/test_session_components.py tests/test_command_families.py tests/test_acp_private_nk_delivery.py
- checkpoint-integration (incomplete at60s): tests/test_private_bus_checkpoint.py tests/test_private_checkpoint_cursor_integration.py tests/test_supervised_cutover.py tests/test_native_source_cursor_certificate.py
- checkpoint-tail (165s bound): 'tests/test_private_checkpoint_cursor_integration.py::test_fresh_open_1002_initials_over_eight_mib_remain_exact[True]' tests/test_private_checkpoint_cursor_integration.py::test_certified_unproven_first_source_cannot_be_skipped tests/test_private_checkpoint_cursor_integration.py::test_pending_unknown_append_cold_rebuild_does_not_replay tests/test_private_checkpoint_cursor_integration.py::test_checkpoint_index_rollback_denies_cursor_without_sql_mutation tests/test_supervised_cutover.py tests/test_native_source_cursor_certificate.py
- certificate-corrected: tests/test_native_source_cursor_certificate.py
- ownership: tests/test_c0_ownership.py

Final native local acceptance:

```sh
PI_COMPACTION_TEST_PACKAGE=/home/ts/wt/comms-refactor-integration-20260928/stack/.pi-native-aef88838db0496c2/node_modules/@earendil-works/pi-coding-agent \
PYTHONPATH="$PWD/src" timeout 60 \
 /home/ts/wt/comms-historical-views-20260927/.test-venv/bin/python \
 -m pytest -o addopts='' --basetemp /home/ts/wt/.c0-darwin-queue \
 tests/test_input_drain_native.py tests/test_selected_tool_native_fake.py -q
```

NRA before/after common invocation:

```sh
timeout 60 /home/ts/code/projects/nominal-refactor-advisor/.venv/bin/python \
 -m nominal_refactor_advisor --json --json-payload summary \
 --parse-workers 2 --analysis-workers 2 --scan-budget-seconds 45 \
 --cache-dir .c0-artifacts/nra-<before-or-after> --context-root src <targets>
```

Before target: src/agent_comms/declarations.py. After targets under src/agent_comms/: activity.py bus_durability.py channel_targets.py display_order.py errors.py goals.py message_bus.py message_page.py messages.py routing.py runtime_info.py shared_ledger.py store_files.py thread_presentation.py threads.py turn_lease.py channels.py read_basis.py presentation.py registry_document.py response_policy.py goal_presentation.py bus_activity_index.py thread_identity.py envelope_claim_transitions.py private_registry_guard.py bus_publication.py.

Ruff I/F and git diff --check completed. Collection used the common interpreter/PYTHONPATH and `--collect-only -q`, before176 integration. Body comparison used Python AST against b5ec95a declarations, removing only Import/ImportFrom nodes; no synthesized equivalence claim.
