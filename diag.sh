T="tests/test_acp.py::TestHandlers::test_new_session_registers_thread_from_cwd
tests/test_command_families.py
tests/test_mentions.py
tests/test_change_report.py::TestChangeReportKind::test_declared_report_publishes_and_round_trips
tests/test_tool_output.py
tests/test_cli.py::TestCliSuccess::test_acknowledge_marks_only_its_references_beside_a_reader
tests/test_owner_release_restart.py::test_changed_owner_after_release_refuses_escalation_and_replacement
tests/test_historical_page_scope.py
tests/test_private_idle_drain.py
tests/test_native_unknown_recovery.py::test_launch_recovers_before_reserving_replacement_pid
tests/test_native_failure_recovery.py
tests/test_concurrency.py
tests/test_peer_relevance.py
tests/test_route_current_sources.py
tests/test_goal_input_review.py
tests/test_historical_notifications.py
tests/test_envelope_bus_integration.py
tests/test_catalog_document.py
tests/test_todos.py::test_watcher_git_and_github_reads_leave_wire_free
tests/test_cli_execution.py
tests/test_carry_bus_history.py
tests/test_coordinated_runtime.py
tests/test_outbound_transcript_source.py
tests/test_transcript_input_display.py
tests/test_transcript_partial_tail.py
tests/test_monitor_delivery.py
tests/test_connector.py::test_a_call_returns_what_the_tool_returns_in_process
tests/test_session_close.py
tests/test_runtime_subscription_handshake.py
tests/test_recipient_activity_index.py
tests/test_coordination_cohort.py
tests/test_coordination_response.py
tests/test_selected_source_batch.py
tests/test_interrupted_delivery_recovery.py
tests/test_channel_bus_ownership.py"
timeout 2000 python -m pytest -q -p no:cacheprovider -rfE --tb=short --no-header --timeout=60 --timeout-method=thread $T > /tmp/pt.txt 2>&1
echo "pytest exit $?"
grep -E '^(FAILED|ERROR) tests|passed|failed' /tmp/pt.txt | cut -c1-230
echo "=== TAILS"
python diag_tails.py /tmp/pt.txt 9 | head -c 80000
