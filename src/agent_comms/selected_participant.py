"""A selected participant holds one exact live lease and its frozen source."""

from __future__ import annotations

import secrets
from contextlib import contextmanager
from dataclasses import dataclass

from .activity import ActivityState
from .bus_publication import CommittedDelivery, stable_thread_lookup
from .cohort_schema import assert_cohort_schema
from .comms import Comms
from .coordinated_runtime_schema import assert_native_runtime_schema
from .coordination_cohort import accept_delivery_cohort, next_sealed_assignment
from .coordination_errors import IdentityConflict, StaleFence
from .coordination_response import LiveResponseOwner, _assert_response_schema
from .coordination_tables.assignments import WakeAssignment
from .coordinator import Coordination
from .errors import RelationViolationError
from .message_bus import MessageBus
from .native_input_owner import ParticipantOwner, RegistryOwner
from .private_registry_guard import _require_no_private_owner_rename
from .wake import WakeDecision


@dataclass(frozen=True)
class SelectedParticipant:
    comms: Comms
    bus: MessageBus
    store: Coordination
    root_id: str
    owner: RegistryOwner
    identity: ParticipantOwner
    lookup: str
    assignment: WakeAssignment
    initial: CommittedDelivery
    provider: str
    model: str

    @property
    def response_owner(self) -> LiveResponseOwner:
        return LiveResponseOwner(
            thread=self.owner.thread, admission_generation=self.owner.admission_generation
        )

    def activity(self, state: ActivityState, description: str) -> None:
        self.comms.agents.set_activity(self.owner.thread.name, state, description[:200])

    @classmethod
    @contextmanager
    def select(cls, comms: Comms, store: Coordination, root_id: str, name: str, after_seq: int):
        bus = MessageBus(comms.root / "bus.jsonl", comms.registry, private_response_writes=True)
        with bus.log.locked():
            _require_no_private_owner_rename(comms.root)
            marker = bus.log._private_marker_unlocked()
            if marker.root_id != root_id:
                raise IdentityConflict("selected admission wire root changed")
            after_seq = max(after_seq, marker.admission_after_seq)
        with store.session.read():
            assert_cohort_schema(store.session._connection)
            _assert_response_schema(store.session._connection)
            assert_native_runtime_schema(store.session._connection)
        try:
            thread, generation = comms.registry.live_owner_with_admission(name)
        except (RelationViolationError, ValueError) as error:
            raise StaleFence("recipient registry identity stopped or changed") from error
        owner = RegistryOwner(thread=thread, admission_generation=generation)
        owner.require_registry(comms.registry)
        lookup = stable_thread_lookup(thread.created_at)
        participant = store.participants.get(lookup)
        identity = ParticipantOwner(thread, participant.participant_generation)
        with store.session.read():
            identity.require(store, lookup)
        assignment = next_sealed_assignment(store, lookup, thread.name, after_seq=after_seq)
        if assignment is None:
            yield None
            return
        participant.pointer.require_idle()
        model_selection = comms.threads.resolve_thread_model(thread.name)
        if not model_selection or "/" not in model_selection:
            raise IdentityConflict("Selected owner has no configured provider/model")
        provider, model = model_selection.split("/", 1)
        if not provider or not model:
            raise IdentityConflict("Selected owner's configured provider/model is incomplete")
        with cls.lease(comms, owner) as leased:
            initial = cls.source(bus, store, root_id, assignment, identity)
            selected = cls(
                comms,
                bus,
                store,
                root_id,
                leased,
                ParticipantOwner(leased.thread, identity.generation),
                lookup,
                assignment,
                initial,
                provider,
                model,
            )
            selected.activity(ActivityState.WORKING, f"Preparing {initial.message.target} message")
            yield selected

    @staticmethod
    @contextmanager
    def lease(comms: Comms, owner: RegistryOwner):
        # Registry CAS owns admission/turn identity; never borrow another ACP
        # instance's turn or revive an owner through a registration side effect.
        try:
            owner.thread.require_idle()
            thread, generation = comms.registry.lease_live_turn_with_admission(
                owner.thread,
                secrets.token_hex(16),
                expected_generation=owner.admission_generation,
            )
        except RelationViolationError as error:
            raise StaleFence("selected owner stopped or busy before native turn") from error
        lease = thread.turn_lease
        assert lease is not None
        try:
            current = RegistryOwner(thread=thread, admission_generation=generation)
            current.require_registry(comms.registry)
            current.require_active_turn()
            yield current
        finally:
            comms.agents.finish_turn(lease)

    @staticmethod
    def source(bus, store, root_id, assignment, identity) -> CommittedDelivery:
        initial = bus.log.read_delivery_cohort(root_id, assignment.wire_seq)
        receipt = accept_delivery_cohort(bus, root_id, assignment.wire_seq, store).value
        if receipt.message_id != assignment.message_id:
            raise IdentityConflict("selected source differs from its sealed receipt")
        if not any(row.assignment_id == assignment.assignment_id for row in receipt.assignments):
            raise IdentityConflict("selected assignment is absent from its sealed receipt")
        # Compare existing recipient and wake declarations as complete values.
        from .audience_manifest import FrozenRecipient

        expected_recipient = FrozenRecipient(
            recipient_lookup=assignment.recipient_lookup, canonical_thread=assignment.recipient
        )
        expected_decision = WakeDecision(
            assignment.recipient_lookup, assignment.audience, assignment.lifecycle.mode
        )
        matches = sum(
            recipient == expected_recipient and decision == expected_decision
            for recipient, decision in zip(
                initial.audience.recipients, initial.decisions, strict=True
            )
        )
        if matches != 1:
            raise IdentityConflict("pending claim is not an original selected bus recipient")
        with store.session.read():
            identity.require(store, assignment.recipient_lookup)
        return initial
