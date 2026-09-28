# Canonical declaration imports

No `agent_comms.declarations` or moved declaration package-root exports remain. Import each current symbol from its actual module below; names and saved formats are unchanged. Parent owns the paired Toad migration. Pascal uses `goal_management.py` for operations; `goals.py` owns Goal data.

- `agent_comms.activity`: `Activity`, `ActivityLog`, `ActivityState`
- `agent_comms.bus_activity_index`: `BusActivityIndex`, `ChannelActivity`
- `agent_comms.bus_durability`: `_claim_gate_enabled`, `_claim_gate_path`, `_verify_claim_bus_before_read_unlocked`
- `agent_comms.bus_page_index`: `BusPageIndex`, `StaleBusPageIndexError`
- `agent_comms.bus_publication`: `CommittedInitial`, `HumanOrigin`, `PRIVATE_WIRE_FIELD`, `has_private_wire_fields`, `initial_sideband`, `public_envelope_digest`, `stable_thread_lookup`, `unique_wire_object`, `validate_initial_record`
- `agent_comms.bus_route_counts`: `BusRouteCounts`
- `agent_comms.channel_targets`: `BROADCAST_ALIASES`, `BuiltinChannel`, `GLOBAL_CHANNEL`, `Tag`, `_TAG_CHARS`, `channel_tag`, `is_channel_target`
- `agent_comms.channels`: `AllOfMatch`, `AnyOfMatch`, `Channel`, `SavedView`, `ViewKind`, `ViewMatch`, `ViewPredicate`
- `agent_comms.declared_family`: `DeclaredFamily`
- `agent_comms.display_order`: `ChannelSort`, `DisplayOrder`, `ThreadSort`
- `agent_comms.envelope_claim_transitions`: `ClaimProjection`, `ClaimRelease`, `ClaimTransition`, `FileClaimPath`, `WakeAdmission`, `_claim_transition_from_wire`, `_claim_transition_wire`, `_release_resource`, `apply_transition`, `normalize_claim_file`, `parse_complete_transition_line`
- `agent_comms.errors`: `ClaimEnvelopeUnknownError`, `HumanInitialUnknownError`, `RelationViolationError`, `UnregisteredThreadError`
- `agent_comms.field_codec`: `FieldCodec`, `projected`
- `agent_comms.goal_presentation`: `ExecutionPresentation`, `GoalExecution`, `GoalExecutionState`, `GoalWaitTarget`, `StandbyExecutionPresentation`, `StateExecutionPresentation`
- `agent_comms.goal_states`: `ActiveGoal`, `BlockedGoal`, `CompletedGoal`, `GoalState`, `PausedGoal`
- `agent_comms.goals`: `Goal`, `GoalMentionBinding`, `GoalMentionSource`
- `agent_comms.mentions`: `MentionCandidate`, `ThreadMention`
- `agent_comms.message_bus`: `MessageBus`
- `agent_comms.message_page`: `MessagePage`
- `agent_comms.messages`: `MembershipChange`, `Message`, `MessageType`, `MessageWireCodec`
- `agent_comms.presentation`: `ChannelView`, `CoordinationSnapshot`, `ThreadView`, `WireRevision`
- `agent_comms.private_registry_guard`: `PRIVATE_OWNER_RENAME_PENDING`, `_require_no_private_owner_rename`
- `agent_comms.read_basis`: `ChannelDisplayScope`, `DMDisplayBasis`, `DisplayBasis`, `ViewUnread`
- `agent_comms.registry_document`: `RegistrySnapshot`
- `agent_comms.response_policy`: `CollectivePolicy`, `DirectPolicy`, `InformationalPolicy`, `MentionedOnlyPolicy`, `ResponseEligibility`, `ResponsePolicy`
- `agent_comms.routing`: `DeliveryScope`, `MessageRoute`, `PendingCounts`, `ScheduledTurn`, `TurnRouting`
- `agent_comms.runtime_info`: `AgentRuntimeInfo`, `RuntimeInfoStore`
- `agent_comms.shared_ledger`: `SharedLedger`
- `agent_comms.store_files`: `_append_jsonl`, `_atomic_write_text`, `_iter_jsonl_records`, `_iter_jsonl_stream`, `_jsonl_records`, `_repair_trailing_jsonl`, `_replace_snapshot`, `_store_lock`, `file_revision`
- `agent_comms.thread_identity`: `OwnerIdentity`, `ThreadIncarnation`, `ThreadRole`, `TurnIdentity`
- `agent_comms.thread_presentation`: `ThreadPresentation`
- `agent_comms.thread_status`: `ThreadStatus`
- `agent_comms.threads`: `Thread`, `_GeneratedCreationTime`, `_thread_creation_time`, `current_thread`
- `agent_comms.turn_lease`: `ActiveTurn`, `FinishedTurnFence`, `TurnFence`, `TurnLeaseFence`

The JSON companion is the same one-time migration map, not a runtime registry. Current operational imports (Comms/wire and result types) are Pascal's independent closure.
