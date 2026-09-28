# C0 declaration ownership — Darwin

Base b5ec95a (merged Pascal lease174). Implementation starting; not yet validated.
Parent accepted functional audit e3ee932 and owns checkpoint migration/activation.

Sequence / actual defining modules:

1. `errors.py`; `store_files.py` canonical lock/atomic/JSONL helpers; `bus_durability.py` existing private read barrier. The lock invokes its barrier at acquisition time; no duplicate lock or durability implementation.
2. `activity.py`, `runtime_info.py`; `goals.py` goal and provenance; extend `goal_presentation.py` execution projections; `thread_presentation.py` immutable label value.
3. `channel_targets.py` builtins/tags/target predicates; `display_order.py` order declarations; extend `channels.py` channel and saved-view declarations alongside existing catalog.
4. Extend `thread_identity.py` ThreadRole; `turn_lease.py` ActiveTurn and fences; `threads.py` Thread/current_thread. Extend `registry_document.py` RegistrySnapshot. Identity/lease policies unchanged.
5. `messages.py`, `routing.py`, `message_page.py`; extend `response_policy.py` eligibility, `read_basis.py` captured display scopes/bases/unread, `presentation.py` view/snapshot projections and `bus_activity_index.py` its ChannelActivity value, `bus_publication.py` HumanOrigin, `envelope_claim_transitions.py` existing claim wire/resource helpers, `private_registry_guard.py` pending rename guard.
6. `message_bus.py` is the sole relocated MessageBus (further S7 decomposition not claimed); `shared_ledger.py` sole existing ledger. Delete declarations.py and declaration re-exports from package root. Migrate all current core/test imports and actual monkeypatch owners; parent migrates Toad.

Pascal owns operational behavior/composition. Darwin edits operations.py only to migrate imports. No shared-self mixins, compatibility aggregate or aliases. Parent handles serial integration and any new Pascal component import conflicts using generated symbol map.

Direct CLI coordination attempted: `codex queue` to Pascal's current thread was rejected with `direct app-server input is not allowed for unloaded spawned sub-agents`. Parent must route this file map to Pascal; independent declaration work can proceed after his completed lease handoff.

This is declaration relocation and deletion of aggregate ownership, not a claim that moving MessageBus changes extension cost. Existing declaration-owned behavior/FieldCodec/ReadLedger/Registration remain authoritative.
