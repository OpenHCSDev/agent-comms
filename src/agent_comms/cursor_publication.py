"""Read durable native evidence and order its informational ACP publication.

Delivery bookkeeping belongs to the connection, never to input admission or
the durable cursor. Every observation reads NativeSourceCursor's original proof.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial

from acp.schema import SessionInfoUpdate

from .acp_extension import (
    CursorAdvancedUpdate,
    CursorEnvelope,
    CursorScope,
    EmptyCursorObservation,
    UnavailableCursorObservation,
    VerifiedCursorObservation,
    encode_updates,
)
from .comms import Comms
from .coordinator import Coordination
from .coordination_errors import CoordinationError
from .message_bus import MessageBus
from .native_source_cursor import NativeSourceCursor
from .native_runtime_input import CurrentNativeCursor
from .runtime import RuntimeServer
from .thread_identity import AdmissionIdentity


@dataclass
class CursorDelivery:
    """One session's transport ordering and last successful publication."""

    revision: int = 0
    published: CursorEnvelope | None = None

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
        return self.published is None or self.published.observation.needs_refresh


class CursorPublication:
    """Connection-owned observer; durable proof and execution stay elsewhere."""

    def __init__(self, comms: Comms, runtime: RuntimeServer, root_id: str | None):
        self.comms, self.runtime, self.root_id = comms, runtime, root_id
        self.deliveries: dict[str, CursorDelivery] = {}

    def delivery(self, session_id: str) -> CursorDelivery:
        return self.deliveries.setdefault(session_id, CursorDelivery())

    def scope(self, thread_name: str, session_id: str) -> CursorScope | None:
        if self.root_id is None:
            return None
        try:
            owner, generation = self.comms.registry.live_owner_with_admission(thread_name)
        except (OSError, ValueError):
            return None
        if owner.pid != os.getpid():
            return None
        return CursorScope(
            session_id,
            self.root_id,
            AdmissionIdentity(owner.incarnation, generation),
            owner.pid,
        )

    async def observe(
        self, thread_name: str, session_id: str, *, defer_busy: bool = False,
        read_cursor: Callable[..., Awaitable[CurrentNativeCursor | None]] = NativeSourceCursor.read_async,
    ) -> CursorEnvelope:
        if self.root_id is None:
            raise ValueError("Native cursor requires the configured root")
        revision = self.delivery(session_id).next_revision()
        scope = await Coordination.run_worker(partial(self.scope, thread_name, session_id))
        unavailable = CursorEnvelope(scope, revision, UnavailableCursorObservation())
        if scope is None:
            return unavailable
        try:
            bus = MessageBus(
                self.comms.root / "bus.jsonl", self.comms.registry,
                private_response_writes=True,
            )
            cursor = await read_cursor(
                bus, wire_root_id=self.root_id, owner_name=thread_name
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
        if self.root_id is None:
            return ()
        observed = await self.observe(thread_name, session_id)
        self.delivery(session_id).trusted_read(observed)
        return (CursorAdvancedUpdate(observed),)

    async def publish(
        self, session_id: str, thread_name: str, *, selected_status: str | None = None,
        read_cursor: Callable[..., Awaitable[CurrentNativeCursor | None]] = NativeSourceCursor.read_async,
    ) -> None:
        delivery = self.delivery(session_id)
        try:
            observed = await self.observe(
                thread_name, session_id, defer_busy=True, read_cursor=read_cursor
            )
        except BlockingIOError:
            # Busy observation is not a new fact. A settled native operation
            # requires another read when the lock clears, without input replay.
            if selected_status is not None:
                delivery.published = None
            return
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
            # Resume only the cursor projection. The immutable native receipt
            # supplies its original admission; no claim or input is resumed.
            await self.publish(
                session_id, thread_name, read_cursor=NativeSourceCursor.refresh_async
            )
