T="tests/test_irc.py::TestChannelsDerived::test_channels_empty_wire
tests/test_goal_nominal.py
tests/test_maintenance_barrier.py::test_activity_write_is_refused_while_draining
tests/test_concurrency.py::TestConcurrentWire::test_concurrent_senders_preserve_every_message_and_sequence
tests/test_native_unknown_recovery.py::test_abandon_requires_real_release
tests/test_monitor_delivery.py::test_parts_require_complete_exact_body_then_reopen
tests/test_historical_notifications.py::test_delivered_assignment_uses_its_recorded_conversation_response
tests/test_coordination_cohort.py::test_reader_rejects_later_corrupt_row_even_for_earlier_valid_seq
tests/test_change_report.py::TestChangeReportKind::test_declared_report_publishes_and_round_trips
tests/guards/test_source_proof_ownership.py::test_source_proof_owners_stay_within_s7_bounds
tests/test_t2_boundary_guards.py::test_retired_attachment_protocol_is_deleted
tests/test_private_idle_drain.py::test_empty_delivery_recovery_does_not_wait_for_wire_writer"
python -m pytest -q -p no:cacheprovider -n 4 --tb=short --no-header $T 2>&1 | grep -v "^\s*$" | cut -c1-260 | head -900
