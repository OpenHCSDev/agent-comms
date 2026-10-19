"""Read durable native evidence and order its informational ACP publication.

Delivery bookkeeping belongs to the connection, never to input permission or
the durable cursor. Every observation reads NativeSourceCursor's original proof.
"""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass, field
from functools import partial

from acp.schema import SessionInfoUpdate

from .acp_extension import (
    CursorAdvancedUpdate,
    CursorEnvelope,
    LiveCursorScope,
    CursorScope,
    NoCursorScope,
    EmptyCursorObservation,
    UnavailableCursorObservation,
    VerifiedCursorObservation,
    encode_updates,
)
from .comms import Comms
from .coordination_errors import CoordinationError
from .coordinator import Coordination
from .errors import RelationViolationError
from .message_bus import MessageBus
from .publisher import ResponseWrites
from .native_entries import NativeEvidenceScope
from .native_source_cursor import NativeSourceCursor
from .runtime import RuntimeServer
from .thread_identity import ClaimedIdentity


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


class CursorPublication:
    """Connection-owned observer; durable proof and execution stay elsewhere."""

    def __init__(self, comms: Comms, runtime: RuntimeServer):
        self.comms, self.runtime = comms, runtime
        self.deliveries: dict[str, CursorDelivery] = {}

    def delivery(self, session_id: str) -> CursorDelivery:
        return self.deliveries.setdefault(session_id, CursorDelivery())

    def scope(self, thread_name: str, session_id: str) -> CursorScope:
        return self.comms.owners.launch_authority.cursor_scope(self, thread_name, session_id)

    def shared_scope(self) -> CursorScope:
        """A shared root's scope: it launches no owner whose cursor this process publishes."""
        return NoCursorScope()

    def live_scope(self, root_id: str, thread_name: str, session_id: str) -> CursorScope:
        """A private root's scope: this process's live owner of ``thread_name``."""
        try:
            owner, generation = self.comms.registry.live_owner_with_claim(thread_name)
        except RelationViolationError:
            return NoCursorScope()  # This process has no live owner input grant.
        if not owner.process_identity.runs_here():
            return NoCursorScope()
        return LiveCursorScope(
            session_id,
            root_id,
            ClaimedIdentity(owner.incarnation, generation),
            owner.require_process().pid,
        )

    async def observe(
        self, thread_name: str, session_id: str, *, defer_busy: bool = False,
        advance: bool = False,
    ) -> CursorEnvelope:
        delivery = self.delivery(session_id)
        async with delivery.observation_lock:
            return await self._observe(
                delivery, thread_name, session_id, defer_busy=defer_busy, advance=advance
            )

    async def _observe(
        self, delivery: CursorDelivery, thread_name: str, session_id: str, *,
        defer_busy: bool, advance: bool,
    ) -> CursorEnvelope:
        root_id = self.comms.owners.require_private_launch().wire_root_id
        revision = delivery.next_revision()
        scope = await Coordination.run_worker(partial(self.scope, thread_name, session_id))
        unavailable = CursorEnvelope(scope, revision, UnavailableCursorObservation())
        if not scope.matches_session(session_id):
            return unavailable
        try:
            bus = MessageBus(self.comms.root / "bus.jsonl", self.comms.registry, ResponseWrites)
            read_cursor = (
                NativeSourceCursor.refresh_async if advance else NativeSourceCursor.read_async
            )
            cursor = await read_cursor(
                bus, wire_root_id=root_id, owner_name=thread_name,
                source_reads=delivery.source_reads,
            )
        except BlockingIOError:
            current = await Coordination.run_worker(partial(self.scope, thread_name, session_id))
            if defer_busy and current == scope:
                raise
            return CursorEnvelope(current, revision, UnavailableCursorObservation())
        except (OSError, ValueError, sqlite3.Error, CoordinationError, KeyError):
            current = await Coordination.run_worker(partial(self.scope, thread_name, session_id))
            return CursorEnvelope(current, revision, UnavailableCursorObservation())
        current = await Coordination.run_worker(partial(self.scope, thread_name, session_id))
        if current != scope:
            return CursorEnvelope(current, revision, UnavailableCursorObservation())
        return CursorEnvelope(
            scope,
            revision,
            EmptyCursorObservation() if cursor is None else VerifiedCursorObservation(cursor),
        )

    async def trusted_metadata(self, thread_name: str, session_id: str) -> tuple:
        launch = self.comms.owners.launch_authority
        return await launch.cursor_metadata(self, thread_name, session_id)

    async def observed_metadata(self, thread_name: str, session_id: str) -> tuple:
        """A private root's trusted cursor read, published as session metadata."""
        observed = await self.observe(thread_name, session_id)
        self.delivery(session_id).trusted_read(observed)
        return (CursorAdvancedUpdate(observed),)

    async def publish(
        self, session_id: str, thread_name: str, *, selected_status: str | None = None,
        advance: bool = False,
    ) -> None:
        delivery = self.delivery(session_id)
        try:
            observed = await self.observe(
                thread_name, session_id, defer_busy=True, advance=advance
            )
        except BlockingIOError:
            # Busy observation is not a new fact. A settled native operation
            # requires another read when the lock clears, without input replay.
            if selected_status is not None:
                delivery.published = None
            return
        delivery.advanced = advance
        if selected_status is None and delivery.already_published(observed):
            return
        try:
            await self.runtime.session_update(
                session_id=session_id,
                update=SessionInfoUpdate(
                    session_update="session_info_update",
                    field_meta=encode_updates(CursorAdvancedUpdate(observed, selected_status)),
                ),
            )
        except (OSError, RuntimeError):
            delivery.published = None
            return
        delivery.published = observed

    async def refresh(self, session_id: str, thread_name: str) -> None:
        if self.delivery(session_id).needs_refresh:
            # Resume only the cursor projection. The immutable native acknowledgement
            # supplies its recorded input grant; no claim or input is resumed.
            await self.publish(session_id, thread_name, advance=True)

    def close(self) -> None:
        """Retire every session-owned verified-prefix reader exactly once."""
        for delivery in self.deliveries.values():
            delivery.source_reads.close()
        self.deliveries.clear()
