"""A selected participant holds one exact live lease and its captured source batch."""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable, Generator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import partial

from agent_comms.reason_codes import GivenReasonCode
from .agent_events import CompactionEvent, NativePhaseChanged
from .bus_publication import CommittedDelivery
from .cohort_schema import assert_cohort_schema
from .comms import Comms
from .coordinated_runtime_schema import assert_native_runtime_schema
from .coordination_cohort import _claim_record, pending_sealed_assignments
from .coordination_errors import IdentityConflict, StaleFence
from .coordination_response import LiveResponseOwner, _assert_response_schema
from .coordination_snapshot import RecoverySnapshot
from .coordination_tables.assignments import WakeAssignment
from .coordination_tables.executions import ExecutionOrigin
from .coordinator import Coordination
from .cursor_publication import CursorDelivery
from .errors import RelationViolationError
from .message_bus import MessageBus
from .message_reference import MessageReference
from .mro_dispatch import MroDispatch, handles
from .native_arguments import SelectedModel
from .native_attestation import ObservedAttestation
from .native_input_owner import ParticipantOwner, RegistryOwner
from .notification_assignment import NotificationAssignment, NotificationAssignmentRead
from .participant_store import ParticipantSnapshot
from .pi_events import TurnContextObserved
from .presentation import MessageNotification, OwnerOutcomes, SentMessageOutcome
from .private_registry_guard import _require_no_private_owner_rename
from .publisher import ParticipantPublisher
from .selected_source_batch import SelectedSource, SelectedSourceBatch
from .threads import Thread
from .turn_phase import PreparingPhase, TurnPhase
from .turn_goal_permission import OwnerGoalPermission, TurnGoalPermission
from .wire_log import WireLog


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
    sent_outcomes: tuple[SentMessageOutcome, ...]
    model: SelectedModel
    cursor_delivery: CursorDelivery
    goal_permission: TurnGoalPermission = field(init=False, repr=False)
    on_compaction: Callable[[CompactionEvent], Awaitable[None]] | None = field(default=None, kw_only=True, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.goal_permission = TurnGoalPermission(OwnerGoalPermission(self.owner.thread.goal))

    @handles(CompactionEvent)
    async def publish_compaction(self, event: CompactionEvent) -> None:
        """Lend the same native observation to this acquired output resource."""
        if self.on_compaction is not None:
            await self.on_compaction(event)

    @handles(ObservedAttestation)
    async def native_source(self, event: ObservedAttestation) -> None:
        """Publish the original child identity before its one input is authorized.

        The registry remains the current-source owner. This acquired operation
        receives its returned snapshot; input checks use this SAME context,
        rather than maintaining another independently refreshed owner snapshot.
        """
        identity = event.identity
        if identity is None:
            raise IdentityConflict("Native source publication lacks its attested identity")
        self.owner = await Coordination.run_worker(partial(
            self.comms.registry.attach_native_session, self.owner, identity.session_file
        ))

    async def fail_before_send(self, max_attempts: int) -> None:
        """Settle captured originals when acquisition fails before any native send."""
        await self.require_current()
        snapshot = self.store.executions.create(
            self.batch.execution_id, ExecutionOrigin.WIRE,
            self.lookup, self.owner.thread.name, max_attempts,
            sources=self.batch.sources,
        ).value
        self.store.executions.fail_unstarted(
            self.batch.execution_id, expected_revision=snapshot.execution.revision,
            reason_code=GivenReasonCode("session-acquisition-failed"),
        )

    async def prepare_attempt(self, snapshot: RecoverySnapshot) -> None:
        """Acquire the strictly newer coordinator owner required by a retry."""
        if snapshot.execution.lifecycle.retry:
            participant = self.store.participants.advance_generation(
                self.lookup,
                self.owner.thread.name,
                expected_generation=self.identity.generation,
            ).value
            self.identity = ParticipantOwner(
                self.owner.thread, participant.participant_generation
            )
        await self.require_current()

    async def require_current(self) -> None:
        def check(resource: Coordination) -> None:
            self.owner.require_registry(self.comms.registry)
            with resource.session.read():
                self.identity.require(resource, self.lookup)

        await Coordination.run_read_async(self.store.session.path, check)

    @property
    def response_owner(self) -> LiveResponseOwner:
        return LiveResponseOwner(
            thread=self.owner.thread, claimed_generation=self.owner.claimed_generation
        )

    async def transition(self, phase: TurnPhase) -> None:
        lease = self.owner.thread.require_turn_lease()
        await Coordination.run_worker(partial(self.comms.agents.transition_turn, lease, phase))

    def consume_reply_wait(self) -> None:
        """Consume each original dependency reply included in this completed input."""
        for source in self.batch.sources:
            self.comms.goals.consume_reply_wait(self.owner, source.delivery.message.reference)

    @handles(TurnContextObserved)
    async def observe_context(self, event: TurnContextObserved) -> None:
        self.owner.require_active_turn()
        lease = self.owner.thread.require_turn_lease()
        await event.context.record(self.comms.contexts, self.owner.thread, lease)

    @handles(NativePhaseChanged)
    async def native_phase(self, event: NativePhaseChanged) -> None:
        await Coordination.run_worker(partial(self.record_native_phase, event))

    def record_native_phase(self, event: NativePhaseChanged) -> None:
        self.comms.agents.observe_native_phase(self.owner.thread.require_turn_lease(), event.phase)

    @classmethod
    @asynccontextmanager
    async def select(cls, comms: Comms, store: Coordination, root_id: str, name: str, after_seq: int, *, cursor_delivery: CursorDelivery, on_compaction: Callable[[CompactionEvent], Awaitable[None]] | None = None):
        bus = MessageBus(WireLog.path_in(comms.root), comms.registry, ParticipantPublisher)
        with cursor_delivery.measurements.operation("claim_prepare"):
            owner, participant, pending, sent_outcomes = await Coordination.run_async(
                store.session.path,
                lambda resource: cls.prepare(comms, bus, resource, root_id, name, after_seq),
            )
        if not pending:
            yield None
            return
        identity = ParticipantOwner(owner.thread, participant.participant_generation)
        with cursor_delivery.measurements.operation("model_selection"):
            model = await Coordination.run_worker(lambda: cls.configured_model(comms, name))
        lease = cls.lease(comms, owner)
        try:
            with cursor_delivery.measurements.operation("claim_lease"):
                leased = await Coordination.run_worker(lambda: next(lease))
            with cursor_delivery.measurements.operation("claim_sources"):
                sources = await cls.sources(bus, store, root_id, pending, identity)
            batch = SelectedSourceBatch.first_route(sources)
            selected = cls(
                comms,
                bus,
                store,
                root_id,
                leased,
                ParticipantOwner(leased.thread, identity.generation),
                participant.lookup,
                batch,
                sent_outcomes,
                model,
                cursor_delivery,
                on_compaction=on_compaction,
            )
            await selected.require_current()
            await selected.transition(
                PreparingPhase(f"Preparing {len(batch.sources)} messages in {batch.target}")
            )
            yield selected
        finally:
            # The blocking call may acquire the lease even if its result is not delivered.
            # The original generator owns retirement even in that case.
            await Coordination.run_worker(lease.close)

    @staticmethod
    def capture_sent_outcomes(
        comms: Comms, owner: Thread, sources: Sequence[CommittedDelivery],
        assignment_read: NotificationAssignmentRead,
    ) -> tuple[SentMessageOutcome, ...]:
        """Project optional prompt context from the drain's captured source and SQL cuts."""
        notifications = MessageNotification.for_sources(
            comms.root, comms.registry, OwnerOutcomes.of(owner), sources,
            assignment_read=assignment_read,
        )
        return tuple(
            outcome
            for notification in notifications
            if (outcome := notification.sent_outcome()) is not None
        )

    @staticmethod
    def prepare(
        comms: Comms, bus: MessageBus, store: Coordination,
        root_id: str, name: str, after_seq: int,
    ) -> tuple[
        RegistryOwner, ParticipantSnapshot, tuple[WakeAssignment, ...],
        tuple[MessageNotification, ...],
    ]:
        """Capture detached registry, participant and optional context in one worker."""
        with bus.log.checked_read() as source:
            _require_no_private_owner_rename(comms.root)
            marker = source.marker
            if marker.root_id != root_id:
                raise IdentityConflict("selected claim_scope wire root changed")
            after_seq = max(after_seq, marker.claimed_after_seq)
        try:
            thread, generation = comms.registry.live_owner_with_claim(name)
        except (RelationViolationError, ValueError) as error:
            raise StaleFence("recipient registry identity stopped or changed") from error
        owner = RegistryOwner(thread=thread, claimed_generation=generation)
        owner.require_registry(comms.registry)
        lookup = thread.incarnation.lookup
        sent_owner, sent_sources = MessageNotification.sent_sources(
            comms.root, comms.registry, comms.bus.log, name,
            limit=MessageNotification.window_limit,
        )
        LiveResponseOwner(
            thread=thread, claimed_generation=generation
        ).resolve_retained(store, bus)
        with store.session.read():
            assert_cohort_schema(store.session._connection)
            _assert_response_schema(store.session._connection)
            assert_native_runtime_schema(store.session._connection)
            participant = store.participants.get(lookup)
            identity = ParticipantOwner(thread, participant.participant_generation)
            identity.require(store, lookup)
            pending = pending_sealed_assignments(
                store, lookup, thread.name, after_seq=after_seq
            )
            sent_read = NotificationAssignment.capture(
                store.session._connection, sent_sources
            )
        if pending:
            participant.pointer.require_idle()
        sent_outcomes = SelectedParticipant.capture_sent_outcomes(
            comms, sent_owner, sent_sources, sent_read
        )
        return owner, participant, pending, sent_outcomes

    @staticmethod
    def configured_model(comms: Comms, name: str) -> SelectedModel:
        return SelectedModel.decode(comms.threads.resolve_thread_model(name))

    @staticmethod
    def lease(comms: Comms, owner: RegistryOwner) -> Generator[RegistryOwner, None, None]:
        # Registry compare-and-swap checks input and turn identity; never borrow another ACP
        # instance's turn or revive an owner through a registration side effect.
        try:
            owner.thread.turn_record.read().require_idle()
            thread, generation = comms.registry.lease_live_turn_with_claim(
                owner.thread,
                secrets.token_hex(16),
                expected_generation=owner.claimed_generation,
            )
        except RelationViolationError as error:
            raise StaleFence("selected owner stopped or busy before native turn") from error
        lease = thread.require_turn_lease()
        try:
            current = RegistryOwner(thread=thread, claimed_generation=generation)
            current.require_registry(comms.registry)
            current.require_active_turn()
            yield current
        finally:
            comms.agents.finish_turn(lease)

    @staticmethod
    async def sources(bus, store, root_id, assignments, identity) -> tuple[SelectedSource, ...]:
        """Read the pending batch from the bus and check its saved acknowledgements.

        Accepting input writes the records. Selecting it checks those records
        without accepting it again. Close both the bus and database reads before
        preparing the native session or making a provider request.
        """
        def capture():
            # Capture source pointers and bytes before opening the SQL snapshot,
            # so file decoding cannot keep a database reader open.
            with bus.log.checked_read() as source:
                marker = source.marker
                if marker.root_id != root_id:
                    raise IdentityConflict("selected source wire root changed")
                for assignment in assignments:
                    if assignment.wire_seq <= marker.claimed_after_seq:
                        raise IdentityConflict("historical source precedes the current claim_scope floor")
                return tuple(source.references(tuple(
                    MessageReference(assignment.wire_seq, assignment.message_id)
                    for assignment in assignments
                )))

        originals = await Coordination.run_worker(capture)

        def verify(resource: Coordination):
            with resource.session.read():
                assert_cohort_schema(resource.session._connection)
                for assignment, initial in zip(assignments, originals, strict=True):
                    acknowledgement = _claim_record(resource.session._connection, initial)
                    if not any(row.assignment_id == assignment.assignment_id
                               for row in acknowledgement.assignments):
                        raise IdentityConflict("selected assignment is absent from its sealed acknowledgement")
                    assignment.require_selected_source(initial, identity.thread)
                    identity.require(resource, assignment.recipient_lookup)

        await Coordination.run_read_async(store.session.path, verify)
        # Mutable claims stay with the original caller's open store; the worker
        # exports no connection, lease or second source/lifecycle authority.
        return tuple(
            SelectedSource(assignment.assignment_id, store.assignments, initial)
            for assignment, initial in zip(assignments, originals, strict=True)
        )
