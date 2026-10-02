"""A selected participant holds one exact live lease and its captured source batch."""

from __future__ import annotations

import secrets
from collections.abc import Generator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial

from .agent_events import NativePhaseChanged
from .pi_events import TurnContextObserved
from .bus_publication import stable_thread_lookup
from .cohort_schema import assert_cohort_schema
from .comms import Comms
from .coordinated_runtime_schema import assert_native_runtime_schema
from .coordination_cohort import _receipt_matches, pending_sealed_assignments
from .coordination_errors import IdentityConflict, StaleFence
from .coordination_response import LiveResponseOwner, _assert_response_schema
from .coordination_tables.assignments import WakeAssignment
from .coordinator import Coordination
from .errors import RelationViolationError
from .message_bus import MessageBus
from .message_reference import MessageReference
from .mro_dispatch import MroDispatch, handles
from .native_input_owner import ParticipantOwner, RegistryOwner
from .native_attestation import ObservedAttestation
from .private_registry_guard import _require_no_private_owner_rename
from .participant_store import ParticipantSnapshot
from .turn_phase import PreparingPhase, TurnPhase
from .selected_source_batch import SelectedSource, SelectedSourceBatch


@dataclass
class SelectedParticipant(MroDispatch):
    comms: Comms
    bus: MessageBus
    store: Coordination
    root_id: str
    owner: RegistryOwner
    identity: ParticipantOwner
    lookup: str
    batch: SelectedSourceBatch
    provider: str
    model: str

    @handles(ObservedAttestation)
    async def native_source(self, event: ObservedAttestation) -> None:
        """Publish the original child identity before its one input is admitted.

        The registry remains the current-source owner. This acquired operation
        receives its returned snapshot; admission references this SAME context,
        rather than maintaining another independently refreshed owner snapshot.
        """
        identity = event.identity
        if identity is None:
            raise IdentityConflict("Native source publication lacks its attested identity")
        self.owner = await Coordination.run_worker(partial(
            self.comms.registry.attach_native_session, self.owner, identity.session_file
        ))

    def require_current(self, resource: Coordination) -> None:
        self.owner.require_registry(self.comms.registry)
        with resource.session.read():
            self.identity.require(resource, self.lookup)

    @property
    def response_owner(self) -> LiveResponseOwner:
        return LiveResponseOwner(
            thread=self.owner.thread, admission_generation=self.owner.admission_generation
        )

    def transition(self, phase: TurnPhase) -> None:
        lease = self.owner.thread.require_turn_lease()
        self.comms.agents.transition_turn(lease, phase)

    def consume_reply_wait(self) -> None:
        """Consume each original dependency reply included in this completed input."""
        for source in self.batch.sources:
            self.comms.goals.consume_reply_wait(self.owner, source.delivery.message.reference)

    @handles(TurnContextObserved)
    async def observe_context(self, event: TurnContextObserved) -> None:
        self.owner.require_active_turn()
        lease = self.owner.thread.require_turn_lease()
        await event.context.record(self.bus.log, self.owner.thread, lease)

    @handles(NativePhaseChanged)
    async def native_phase(self, event: NativePhaseChanged) -> None:
        await Coordination.run_worker(partial(self.record_native_phase, event))

    def record_native_phase(self, event: NativePhaseChanged) -> None:
        current = self.comms.registry.require(self.owner.thread.name).turn_state.phase
        self.transition(current.observed(event.phase))

    @classmethod
    @asynccontextmanager
    async def select(cls, comms: Comms, store: Coordination, root_id: str, name: str, after_seq: int):
        bus = MessageBus(comms.root / "bus.jsonl", comms.registry, private_response_writes=True)
        owner, participant, pending = await Coordination.run_async(
            store.session.path,
            lambda resource: cls.prepare(comms, bus, resource, root_id, name, after_seq),
        )
        if not pending:
            yield None
            return
        identity = ParticipantOwner(owner.thread, participant.participant_generation)
        provider, model = await Coordination.run_worker(lambda: cls.configured_model(comms, name))
        lease = cls.lease(comms, owner)
        try:
            leased = await Coordination.run_worker(lambda: next(lease))
            sources = await cls.sources(bus, store, root_id, pending, identity)
            batch = SelectedSourceBatch(sources)
            selected = cls(
                comms,
                bus,
                store,
                root_id,
                leased,
                ParticipantOwner(leased.thread, identity.generation),
                participant.lookup,
                batch,
                provider,
                model,
            )
            await Coordination.run_async(store.session.path, selected.require_current)
            await Coordination.run_worker(lambda: selected.transition(
                PreparingPhase(f"Preparing {len(batch.sources)} messages in {', '.join(batch.targets)}")
            ))
            yield selected
        finally:
            # Joined entry can acquire custody without delivering its result.
            # The original generator owns retirement even in that case.
            await Coordination.run_worker(lease.close)

    @staticmethod
    def prepare(
        comms: Comms, bus: MessageBus, store: Coordination,
        root_id: str, name: str, after_seq: int,
    ) -> tuple[RegistryOwner, ParticipantSnapshot, tuple[WakeAssignment, ...]]:
        """Capture detached original registry/participant rows in one worker."""
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
        pending = pending_sealed_assignments(store, lookup, thread.name, after_seq=after_seq)
        if pending:
            participant.pointer.require_idle()
        return owner, participant, pending

    @staticmethod
    def configured_model(comms: Comms, name: str) -> tuple[str, str]:
        model_selection = comms.threads.resolve_thread_model(name)
        if not model_selection or "/" not in model_selection:
            raise IdentityConflict("Selected owner has no configured provider/model")
        provider, model = model_selection.split("/", 1)
        if not provider or not model:
            raise IdentityConflict("Selected owner's configured provider/model is incomplete")
        return provider, model

    @staticmethod
    def lease(comms: Comms, owner: RegistryOwner) -> Generator[RegistryOwner, None, None]:
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
    async def sources(bus, store, root_id, assignments, identity) -> tuple[SelectedSource, ...]:
        """Borrow one original certificate for the already sealed pending batch.

        Initial acceptance owns writes. Selection verifies every original sealed
        receipt through that same validator, without re-entering acceptance. Both
        read resources close before any native preparation or provider request.
        """
        def capture(resource: Coordination):
            # The certificate lends original pointer/byte snapshots. Decode
            # outside its custody, using only the worker's owned SQL connection.
            with bus.log.certified_read() as source:
                marker = source.marker
                if marker.root_id != root_id:
                    raise IdentityConflict("selected source wire root changed")
                for assignment in assignments:
                    if assignment.wire_seq <= marker.admission_after_seq:
                        raise IdentityConflict("historical source precedes the current admission floor")
                captured = source.references(tuple(
                    MessageReference(assignment.wire_seq, assignment.message_id)
                    for assignment in assignments
                ))
            originals = tuple(captured)
            with resource.session.read():
                assert_cohort_schema(resource.session._connection)
                for assignment, initial in zip(assignments, originals, strict=True):
                    receipt = _receipt_matches(resource.session._connection, initial)
                    if not any(row.assignment_id == assignment.assignment_id
                               for row in receipt.assignments):
                        raise IdentityConflict("selected assignment is absent from its sealed receipt")
                    assignment.require_selected_source(initial, identity.thread)
                    identity.require(resource, assignment.recipient_lookup)
            return originals

        originals = await Coordination.run_async(store.session.path, capture)
        # Mutable claims stay with the original caller's open store; the worker
        # exports no connection, lease or second source/lifecycle authority.
        return tuple(
            SelectedSource(assignment.assignment_id, store.assignments, initial)
            for assignment, initial in zip(assignments, originals, strict=True)
        )
