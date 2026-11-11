# P23 declaration packages

Original declarations keep their fields, identities and procedures. This table only places P23's newly introduced flat modules under their existing domain packages; native relocation and dissolve must migrate every Core and owned Toad consumer without re-exports. Refused ownership, module-context or construction obligations remain explicit.

| Package | Original P23 declarations to place there |
|---|---|
| `execution` | `agent_event.py`, `context_assembly.py`, `context_owner.py`, `context_provenance_source.py`, `delivers_input.py`, `following_input_contracts.py`, `import_history_source.py`, `imported_conversation.py`, `imported_messages.py`, `imported_session_metadata.py`, `input_event_source.py`, `input_ingress.py`, `input_lookup.py`, `input_publication_read.py`, `input_publication_source.py`, `instruction_bytes.py`, `native_execution.py`, `native_failure_reads.py`, `original_inputs.py`, `queue_publication.py`, `runtime_info_projection.py`, `runtime_info_source.py`, `session_owner_reads.py`, `session_reads.py`, `turn_failure_reads.py`, `turn_progress_reads.py`, `turn_registry.py` |
| `messaging` | `assignment_notice.py`, `bus_record_builder.py`, `channel_sources.py`, `delivery_recipients.py`, `display_sources.py`, `initial_origin.py`, `message_clock_source.py`, `message_directions.py`, `message_reads.py`, `message_send_reads.py`, `message_sender.py`, `message_writer.py`, `published_messages.py`, `response_message_source.py`, `task_callbacks.py` |
| `planning` | `goal_control.py`, `goal_events.py`, `goal_execution.py`, `goal_execution_snapshot.py`, `goal_ledger.py`, `goal_mutation.py`, `goal_permit_source.py`, `goal_snapshots.py`, `goal_wait_target.py`, `goal_work.py`, `order_phase.py`, `task_scope.py` |
| `registry` | `activity_registry_reads.py`, `peer_relation.py`, `recipient_selection.py`, `registry_names.py`, `relationship_catalog_source.py`, `tagged_thread_source.py`, `task_owner.py`, `task_registry.py`, `task_scope_owner.py`, `thread_identity_source.py`, `thread_role_source.py` |
| `cli_verbs` | `target_command_bindings.py` |
