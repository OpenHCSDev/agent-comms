"""Read durable native evidence and order its informational ACP publication.

Delivery bookkeeping belongs to the connection, never to input permission or
the durable cursor. Every observation reads the cursor owner's original proof.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING

from acp.schema import SessionInfoUpdate

from agent_comms.acp_extension import (
    CursorAdvancedUpdate,
    CursorEnvelope,
    UnavailableCursorObservation,
    encode_updates,
)
from agent_comms.comms import Comms
from agent_comms.coordination.coordinator import Coordination
from agent_comms.coordination.errors import DeferredRead
from agent_comms.execution.cursor_owner import CommittedCursorOwner, CursorOwner, CursorState
from agent_comms.execution.native_entries import NativeEvidenceScope
from agent_comms.execution.publication_measurements import PublicationMeasurements
from agent_comms.messaging.message_bus import MessageBus
from agent_comms.messaging.publisher import ResponseWrites
from agent_comms.nominal.field_codec import AcpSessionId, InputId
from agent_comms.registry.privacy_reads import CursorMetadataRead
from agent_comms.transport.attachment_scope import CursorScope
from agent_comms.transport.runtime_server import RuntimeServer

if TYPE_CHECKING:
    from agent_comms.execution.selected.participant import SelectedParticipant
    from agent_comms.registry.registry_store import RegistryCut


class CursorRetry(ABC):
    """Whether a deferred cursor read has its own retry in flight."""

    @abstractmethod
    def armed(self, start: Callable[[], asyncio.Task[None]]) -> CursorRetry: ...

    @abstractmethod
    def consumed(self) -> CursorRetry: ...


class NoCursorRetry(CursorRetry):
    def armed(self, start: Callable[[], asyncio.Task[None]]) -> CursorRetry:
        return PendingCursorRetry(start())

    def consumed(self) -> CursorRetry:
        return self


@dataclass(frozen=True)
class PendingCursorRetry(CursorRetry):
    task: asyncio.Task[None]

    def armed(self, start: Callable[[], asyncio.Task[None]]) -> CursorRetry:
        # A live retry already covers this deferral; a finished one, or the retry
        # deferring again from inside itself, starts the next bounded read.
        if self.task.done() or self.task is asyncio.current_task():
            return PendingCursorRetry(start())
        return self

    def consumed(self) -> CursorRetry:
        if self.task is not asyncio.current_task():
            self.task.cancel()
        return NoCursorRetry()


@dataclass
class CursorDelivery:
    """One session's transport ordering and last successful publication."""

    revision: int = 0
    published: CursorEnvelope | None = None
    # The last completed observation advanced the durable cursor from the
    # canonical source. Its answer is a function of the owner's drain
    # observation revision: repeating it while that revision is unchanged
    # cannot extend it, and a changed revision reruns the drain, whose own
    # observation replaces this one.
    advanced: bool = False
    # This ACP session publication is the one lifetime owner. The reader owns
    # verified prefix position/hash; repeated observations only decode appends.
    source_reads: NativeEvidenceScope = field(default_factory=NativeEvidenceScope, repr=False)
    observation_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    measurements: PublicationMeasurements = field(default_factory=PublicationMeasurements, repr=False)
    # A contended read owns one retry: nothing else announces a writer that
    # changed no rows. ContendedRead already spent the store's whole wait, so
    # each retry is one bounded read; other deferrals keep their own path.
    retry: CursorRetry = field(default_factory=lambda: NoCursorRetry(), repr=False)

    @asynccontextmanager
    async def reading(self) -> AsyncIterator[NativeEvidenceScope]:
        """Serialize every borrower of this recipient's verified source."""
        async with self.observation_lock:
            with self.source_reads.borrow():
                yield self.source_reads

    async def advance(
        self, participant: SelectedParticipant, input_id: InputId,
    ) -> CursorState:
        """Selected settlement borrows this recipient's serialized reader lifetime."""
        async with self.reading() as source_reads:
            owner = CommittedCursorOwner(
                thread=participant.owner.thread,
                claimed_generation=participant.owner.claimed_generation,
                wire_root_id=participant.root_id,
                generation=participant.identity.generation,
                input_id=input_id,
            )
            return await owner.advance_async(participant.bus, source_reads=source_reads)

    def close(self) -> None:
        self.retry = self.retry.consumed()
        self.source_reads.close()

    def next_revision(self) -> int:
        self.revision += 1
        return self.revision

    def already_published(self, observed: CursorEnvelope) -> bool:
        return self.published is not None and observed.same_observation(self.published)

    def trusted_read(self, observed: CursorEnvelope) -> None:
        if self.published is not None and not self.already_published(observed):
            self.published = None

    @property
    def needs_refresh(self) -> bool:
        return self.published is None or (
            not self.advanced and self.published.observation.needs_refresh
        )


class CursorPublication(CursorMetadataRead[CursorAdvancedUpdate]):
    """Connection-owned observer; durable proof and execution stay elsewhere."""

    def __init__(self, comms: Comms, runtime: RuntimeServer):
        self.comms, self.runtime = comms, runtime
        self.deliveries: dict[AcpSessionId, CursorDelivery] = {}

    def delivery(self, session_id: AcpSessionId) -> CursorDelivery:
        return self.deliveries.setdefault(session_id, CursorDelivery())

    def scope(self, thread_name: str, session_id: AcpSessionId) -> tuple[RegistryCut, CursorScope]:
        cut = self.comms.registry.store.snapshot_with_authority()
        return cut, cut.privacy.cursor_scope(cut.snapshot, thread_name, session_id)

    def scope_after(
        self, cut: RegistryCut, scope: CursorScope, thread_name: str, session_id: AcpSessionId,
    ) -> CursorScope:
        """The scope now. It is a function of its registry cut, so a cut that is still the
        store's answers without reading the registry again."""
        if cut.matches_current():
            return scope
        return self.scope(thread_name, session_id)[1]

    async def observe(
        self, thread_name: str, session_id: AcpSessionId, *, defer_busy: bool = False,
        advance: bool = False,
    ) -> CursorEnvelope:
        delivery = self.delivery(session_id)
        with delivery.measurements.operation("cursor_observation"):
            async with delivery.reading() as source_reads:
                return await self._observe(
                    delivery, thread_name, session_id, source_reads=source_reads,
                    defer_busy=defer_busy, advance=advance,
                )

    async def _observe(
        self, delivery: CursorDelivery, thread_name: str, session_id: AcpSessionId, *,
        source_reads: NativeEvidenceScope, defer_busy: bool, advance: bool,
    ) -> CursorEnvelope:
        revision = delivery.next_revision()
        cut, scope = await Coordination.run_worker(partial(self.scope, thread_name, session_id))
        current_scope = partial(self.scope_after, cut, scope, thread_name, session_id)
        unavailable = CursorEnvelope(scope, revision, UnavailableCursorObservation())
        if not scope.matches_session(session_id):
            return unavailable
        root_id = scope.require_live().wire_root_id
        try:
            bus = MessageBus(self.comms.root, self.comms.registry, ResponseWrites)
            read_cursor = (
                CursorOwner.refresh_async if advance else CursorOwner.read_async
            )
            state = await read_cursor(
                bus, wire_root_id=root_id, owner_name=thread_name,
                source_reads=source_reads,
            )
        except DeferredRead:
            current = await Coordination.run_worker(current_scope)
            if defer_busy and current == scope:
                raise
            return CursorEnvelope(current, revision, UnavailableCursorObservation())
        current = await Coordination.run_worker(current_scope)
        if current != scope:
            return CursorEnvelope(current, revision, UnavailableCursorObservation())
        return CursorEnvelope(
            scope,
            revision,
            state.observation(),
        )

    async def trusted_metadata(
        self, thread_name: str, session_id: AcpSessionId,
    ) -> tuple[CursorAdvancedUpdate, ...]:
        privacy = await Coordination.run_worker(self.comms.registry.store.privacy)
        return await privacy.cursor_metadata(self, thread_name, session_id)

    async def observed_metadata(
        self, thread_name: str, session_id: AcpSessionId,
    ) -> tuple[CursorAdvancedUpdate, ...]:
        """A private root's trusted cursor read, published as session metadata."""
        observed = await self.observe(thread_name, session_id)
        self.delivery(session_id).trusted_read(observed)
        return (CursorAdvancedUpdate(observed),)

    async def publish(
        self, session_id: AcpSessionId, thread_name: str, *, selected_status: str | None = None,
        advance: bool = False,
    ) -> None:
        delivery = self.delivery(session_id)
        try:
            observed = await self.observe(
                thread_name, session_id, defer_busy=True, advance=advance
            )
        except DeferredRead as deferred:
            # Busy observation is not a new fact. A settled native operation
            # requires another read when the lock clears, without input replay.
            if selected_status is not None:
                delivery.published = None
            delivery.retry = deferred.retried(delivery.retry, lambda: delivery.retry.armed(
                lambda: self.runtime.background(
                    self.publish(session_id, thread_name, advance=advance))))
            return
        delivery.retry = delivery.retry.consumed()
        delivery.advanced = advance
        if selected_status is None and delivery.already_published(observed):
            return
        try:
            await self.runtime.publish_update(
                session_id=session_id,
                update=SessionInfoUpdate(
                    session_update="session_info_update",
                    field_meta=encode_updates(CursorAdvancedUpdate(observed, selected_status)),
                ),
            )
        except (OSError, RuntimeError):
            delivery.published = None
            raise
        delivery.published = observed

    async def refresh(self, session_id: AcpSessionId, thread_name: str) -> None:
        if self.delivery(session_id).needs_refresh:
            # Resume only the cursor projection. The immutable native acknowledgement
            # supplies its recorded input grant; no claim or input is resumed.
            await self.publish(session_id, thread_name, advance=True)

    def close(self) -> None:
        """Retire every session-owned verified-prefix reader exactly once."""
        for delivery in self.deliveries.values():
            delivery.close()
        self.deliveries.clear()
