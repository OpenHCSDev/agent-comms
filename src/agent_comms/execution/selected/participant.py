from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Generator, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import (
    dataclass,
    field,
)
from functools import partial

from metaclass_registry.mro_dispatch import (
    MroDispatch,
    handles,
)

from agent_comms.agent_events import CompactionEvent
from agent_comms.comms import Comms
from agent_comms.context_segments.wake import SelectedWakeSegment
from agent_comms.coordination.cohort import (
    _claim_record,
    pending_sealed_assignments,
)
from agent_comms.coordination.cohort_schema import CohortTable
from agent_comms.coordination.coordinator import Coordination
from agent_comms.coordination.errors import (
    IdentityConflict,
    StaleFence,
)
from agent_comms.coordination.participant_store import ParticipantSnapshot
from agent_comms.coordination.reason_codes import GivenReasonCode
from agent_comms.coordination.response import LiveResponseOwner
from agent_comms.coordination.snapshot import RecoverySnapshot
from agent_comms.coordination.tables.assignments import WakeAssignment
from agent_comms.coordination.tables.executions import WireOrigin
from agent_comms.coordination.tables.response_schema import ResponseTable
from agent_comms.errors import RelationViolationError
from agent_comms.execution.cursor_publication import CursorDelivery
from agent_comms.execution.native_arguments import SelectedModel
from agent_comms.execution.native_attestation import ObservedAttestation
from agent_comms.execution.native_input_owner import (
    ParticipantOwner,
    RegistryOwner,
)
from agent_comms.execution.native_runtime_input import NativeRuntimeTable
from agent_comms.execution.native_turn_context import ContextObserved
from agent_comms.execution.publication_measurements import PublicationMeasurements
from agent_comms.execution.selected.owner_source import RegistryOwnerSource
from agent_comms.execution.selected.result import CoordinatedTurn
from agent_comms.execution.selected.source_batch import (
    SelectedSource,
    SelectedSourceBatch,
)
from agent_comms.execution.turn_goal_permission import (
    OwnerGoalPermission,
    TurnGoalPermission,
)
from agent_comms.execution.turn_phase import (
    NativePhaseChanged,
    TurnPhase,
)
from agent_comms.messaging.bus_publication import Delivered
from agent_comms.messaging.message_bus import MessageBus
from agent_comms.messaging.message_notification import (
    MessageNotification,
    SentMessageOutcome,
)
from agent_comms.messaging.message_reference import MessageReference
from agent_comms.messaging.notification_evidence import OwnerOutcomes
from agent_comms.messaging.publisher import ParticipantPublisher
from agent_comms.nominal.field_codec import (
    RootId,
    ThreadLookup,
    TurnId,
)
from agent_comms.notification_assignment import (
    AssignmentSource,
    NotificationAssignment,
    NotificationAssignmentRead,
    NotificationSelection,
)


class NoLivePublication(MroDispatch):
    """A selected turn run without an ACP runtime (foreground, tests) has no live viewers."""


@dataclass(frozen=True)
class TriagePublication(MroDispatch):
    """A triage call shows viewers only what changes the session, never its decision text."""

    publication: MroDispatch

    @handles(CompactionEvent)
    async def compacted(self, event: CompactionEvent) -> None:
        await self.publication.dispatch(event)


class ConsidersSelectedWork(ABC):
    """The execution a selection answers. It owns what a selected participant's turn does."""

    @abstractmethod
    async def consider(self, participant: SelectedParticipant) -> CoordinatedTurn: ...


class SelectedWork(ABC):
    """What a selection found for its participant: nothing pending, or the participant whose
    turn begins. Each answers the execution that selected it, so nothing asks which it is."""

    @abstractmethod
    async def considered_by(self, execution: ConsidersSelectedWork) -> CoordinatedTurn | None: ...


class NoSelectedWork(SelectedWork):
    """Nothing is pending for this participant, so there is no turn."""

    async def considered_by(self, execution: ConsidersSelectedWork) -> None:
        return None


@dataclass
class SelectedParticipant(MroDispatch, RegistryOwnerSource[RegistryOwner], SelectedWork):
    comms: Comms
    bus: MessageBus
    store: Coordination
    root_id: RootId
    owner: RegistryOwner
    identity: ParticipantOwner
    lookup: ThreadLookup
    batch: SelectedSourceBatch
    sent_outcomes: tuple[SentMessageOutcome, ...]
    model: SelectedModel
    cursor_delivery: CursorDelivery
    goal_permission: TurnGoalPermission = field(init=False, repr=False)
    # The runtime's live publication: viewers attached to this session see the turn as it runs.
    publication: MroDispatch = field(default_factory=NoLivePublication, kw_only=True, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.goal_permission = TurnGoalPermission(OwnerGoalPermission(self.owner.thread.goal.authority))

    async def observe_triage(self, event) -> None:
        await self.dispatch(event)
        await TriagePublication(self.publication).dispatch(event)

    @handles(ObservedAttestation)
    async def native_source(self, event: ObservedAttestation) -> None:
        """Publish the original child identity before its one input is authorized.

        The registry remains the current-source owner. This acquired operation
        receives its returned snapshot; input checks use this SAME context,
        rather than maintaining another independently refreshed owner snapshot.
        """
        for identity in event.identity.present():
            self.owner = await Coordination.run_worker(partial(
                self.comms.registry.attach_native_session, self.owner, identity.session_file
            ))
            break
        else:
            raise IdentityConflict("Native source publication lacks its attested identity")

    async def fail_before_send(self, max_attempts: int) -> None:
        """Settle captured originals when acquisition fails before any native send."""
        await self.require_current()
        snapshot = self.store.create(
            self.batch.execution_id, WireOrigin,
            self.lookup, max_attempts,
            sources=self.batch.sources,
        ).value
        self.store.fail_unstarted(
            self.batch.execution_id, expected_revision=snapshot.execution.revision,
            reason_code=GivenReasonCode("session-acquisition-failed"),
        )

    async def prepare_attempt(self, snapshot: RecoverySnapshot) -> None:
        """Acquire the strictly newer coordinator owner required by a retry."""
        if snapshot.execution.lifecycle.is_retry():
            participant = self.store.participants.advance_generation(
                self.lookup,
                expected_generation=self.identity.generation,
            ).value
            self.identity = ParticipantOwner(
                self.owner.thread, participant.owner_identity.generation
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

    @handles(ContextObserved)
    async def observe_context(self, event: ContextObserved) -> None:
        self.owner.require_active_turn()
        await event.record(
            self.comms.contexts, self.owner.thread, self.owner.thread.require_turn_lease())

    @handles(NativePhaseChanged)
    async def native_phase(self, event: NativePhaseChanged) -> None:
        await Coordination.run_worker(partial(self.record_native_phase, event))

    def record_native_phase(self, event: NativePhaseChanged) -> None:
        self.comms.agents.observe_native_phase(self.owner.thread.require_turn_lease(), event.phase)

    @classmethod
    @asynccontextmanager
    async def select(
        cls, comms: Comms, store: Coordination, root_id: RootId, name: str, after_seq: int,
        *, cursor_delivery: CursorDelivery,
        publication: MroDispatch = NoLivePublication(),
    ):
        bus = MessageBus(comms.root, comms.registry, ParticipantPublisher)
        measured = cursor_delivery.measurements.named("claim_prepare")
        with measured.measuring():
            prepared = await Coordination.run_async(
                store.session.path,
                lambda resource: cls.prepare(
                    comms, bus, resource, root_id, name, after_seq, measured),
            )
        async with prepared.selected(
            comms, bus, store, root_id, name, cursor_delivery, publication,
        ) as work:
            yield work

    async def considered_by(self, execution: ConsidersSelectedWork) -> CoordinatedTurn:
        return await execution.consider(self)

    @staticmethod
    def sent_pages(
        viewer: OwnerOutcomes, sources: tuple[Delivered, ...],
    ) -> Iterator[tuple[Delivered, ...]]:
        """Newest sent messages first, in pages that double. The prompt's limit counts outcomes,
        and a message has one for each recipient its viewer sees, so a page holds as many
        messages as name that many recipients: one channel message fills the prompt where five
        direct ones do. A page of five channel messages built sixty outcomes for five lines.
        A thread with no outcome to show still reads its whole window in a few pages."""
        start, wanted = 0, SelectedWakeSegment.sent_limit
        while start < len(sources):
            end, named = start, 0
            while end < len(sources) and named < wanted:
                named += len(viewer.positions(sources[end]))
                end += 1
            yield sources[start:end]
            start, wanted = end, wanted * 2

    @staticmethod
    def capture_sent_outcomes(
        comms: Comms, assignments: AssignmentSource, assignment_read: NotificationAssignmentRead,
    ) -> tuple[SentMessageOutcome, ...]:
        """Project optional prompt context from the drain's captured source and SQL cuts, in
        the registry the assignments were read under."""
        notifications = MessageNotification.for_sources(
            comms.root, comms.registry, assignment_read, snapshot=assignments.registry,
        )
        return tuple(
            outcome
            for notification in notifications
            if (outcome := notification.sent_outcome()) is not None
        )

    @staticmethod
    def prepare(
        comms: Comms, bus: MessageBus, store: Coordination,
        root_id: RootId, name: str, after_seq: int, measured: PublicationMeasurements,
    ) -> PreparedSelection:
        """Capture detached registry, participant and optional context in one worker.

        ``measured`` is this claim's own counter. Each part of the capture names itself under
        it, as the prompt's send does, so a turn's diagnostics say where its claim went."""
        with measured.operation("registry_owner"):
            privacy = comms.registry.store.privacy()
            privacy.require_root(root_id, IdentityConflict("Selected claim namespace changed"))
            try:
                thread, generation = comms.registry.live_owner_with_claim(name)
            except (RelationViolationError, ValueError) as error:
                raise StaleFence("recipient registry identity stopped or changed") from error
            owner = RegistryOwner(thread=thread, claimed_generation=generation)
            owner.require_registry(comms.registry)
            lookup = thread.incarnation.lookup
        with measured.operation("sent_sources"):
            sent_owner, sent_sources = MessageNotification.sent_sources(
                comms.root, comms.registry, comms.bus.bus, name,
                limit=NotificationSelection.window_limit,
            )
        with measured.operation("retained_response"):
            LiveResponseOwner(
                thread=thread, claimed_generation=generation
            ).resolve_retained(store, bus)
        with store.session.read():
            with measured.operation("participant_and_pending"):
                CohortTable.require_installed(store.session._connection)
                ResponseTable.require_installed(store.session._connection)
                NativeRuntimeTable.require_installed(store.session._connection)
                participant = store.participants.get(lookup)
                identity = ParticipantOwner(thread, participant.owner_identity.generation)
                identity.require(store, lookup)
                pending = pending_sealed_assignments(
                    store, lookup, after_seq=after_seq
                )
            if not pending:
                return NothingPending()
            # The prompt shows sent_limit outcomes and only a turn with pending work has a
            # prompt. Reading all 120 sent messages' assignments (about 1,500 rows) to fill
            # five slots cost every turn a quarter second; newest pages stop when they are full.
            viewer = OwnerOutcomes.of(sent_owner.incarnation)
            pages = SelectedParticipant.sent_pages(viewer, sent_sources)
            with measured.operation("sent_assignments"):
                sent_read = NotificationAssignment.capture(
                    store.session._connection,
                    NotificationSelection.of(viewer, next(pages, ())), privacy,
                )
        participant.pointer.require_idle()
        assignments = AssignmentSource(comms.root)
        with measured.operation("sent_evidence"):
            sent_evidence = NotificationAssignment.capture_detached(assignments, sent_read)
        with measured.operation("sent_outcomes"):
            sent_outcomes = SelectedParticipant.capture_sent_outcomes(
                comms, assignments, sent_evidence)
        with measured.operation("sent_further_pages"):
            for page in pages:
                if len(sent_outcomes) >= SelectedWakeSegment.sent_limit:
                    break
                sent_outcomes += SelectedParticipant.capture_sent_outcomes(
                    comms, assignments,
                    assignments.read_assignments(NotificationSelection.of(viewer, page)))
        return PendingSelection(owner, participant, pending, sent_outcomes)

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
                TurnId.mint(),
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
            # Historical public records have no delivery capability. Native identity
            # belongs to the original registry guard, not a second bus metadata file.
            bus._registry.store.privacy().require_root(
                root_id, IdentityConflict("Selected source namespace changed"))
            with bus.bus.reading() as source:
                return source.deliveries_for_references(tuple(
                    MessageReference(assignment.wire_seq, assignment.message_id)
                    for assignment in assignments
                ))

        originals = await Coordination.run_worker(capture)

        def verify(resource: Coordination):
            with resource.session.capture(bus) as boundary, resource.session.read():
                boundary.privacy.require_root(root_id, IdentityConflict("Selected source namespace changed"))
                CohortTable.require_installed(resource.session._connection)
                for assignment, initial in zip(assignments, originals, strict=True):
                    acknowledgement = _claim_record(resource.session._connection, initial, boundary.privacy)
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


class PreparedSelection(ABC):
    """What preparing a selection read for its participant. The pending rows get their type
    once, where they are read, and each member opens the selection it stands for."""

    @abstractmethod
    def selected(
        self, comms: Comms, bus: MessageBus, store: Coordination, root_id: RootId, name: str,
        cursor_delivery: CursorDelivery, publication: MroDispatch,
    ) -> AbstractAsyncContextManager[SelectedWork]: ...


class NothingPending(PreparedSelection):
    """No sealed assignment waits for this participant."""

    @asynccontextmanager
    async def selected(
        self, comms: Comms, bus: MessageBus, store: Coordination, root_id: RootId, name: str,
        cursor_delivery: CursorDelivery, publication: MroDispatch,
    ) -> AsyncIterator[SelectedWork]:
        yield NoSelectedWork()


@dataclass(frozen=True)
class PendingSelection(PreparedSelection):
    """The participant's pending assignments and the sent outcomes its prompt shows; only a
    selection with work has a prompt, so only this member carries them."""

    owner: RegistryOwner
    participant: ParticipantSnapshot
    pending: tuple[WakeAssignment, ...]
    sent_outcomes: tuple[SentMessageOutcome, ...]

    @asynccontextmanager
    async def selected(
        self, comms: Comms, bus: MessageBus, store: Coordination, root_id: RootId, name: str,
        cursor_delivery: CursorDelivery, publication: MroDispatch,
    ) -> AsyncIterator[SelectedWork]:
        identity = ParticipantOwner(self.owner.thread, self.participant.owner_identity.generation)
        with cursor_delivery.measurements.operation("model_selection"):
            model = await Coordination.run_worker(
                lambda: SelectedParticipant.configured_model(comms, name))
        lease = SelectedParticipant.lease(comms, self.owner)
        try:
            with cursor_delivery.measurements.operation("claim_lease"):
                leased = await Coordination.run_worker(lambda: next(lease))
            with cursor_delivery.measurements.operation("claim_sources"):
                sources = await SelectedParticipant.sources(
                    bus, store, root_id, self.pending, identity)
            batch = SelectedSourceBatch.first_route(sources)
            selected = SelectedParticipant(
                comms,
                bus,
                store,
                root_id,
                leased,
                ParticipantOwner(leased.thread, identity.generation),
                self.participant.lookup,
                batch,
                self.sent_outcomes,
                model,
                cursor_delivery,
                publication=publication,
            )
            with cursor_delivery.measurements.operation("claim_current"):
                await selected.require_current()
            # The consideration that takes this claim states what it prepares: a triage or
            # a response. A phase published here was replaced by that one a moment later.
            yield selected
        finally:
            # The blocking call may acquire the lease even if its result is not delivered.
            # The original generator owns retirement even in that case.
            await Coordination.run_worker(lease.close)
