"""A thread's inbox pushed over a WebSocket, so a session on another machine is woken by a message.

The session already holds a connector login, but only its model can use that login; a process it
starts cannot. So a tool call on that login returns a short-lived URL, and agent-comms-inbox-watch
opens it under the session's Monitor: no credential is copied. The session's transcript comes back
up the same socket, so its work shows on the bus as a local bridge's does.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field, replace
from functools import cached_property, partial
from pathlib import Path
from typing import ClassVar

from aiohttp import WSCloseCode, WSMsgType, web
from yarl import URL

from .agent_inbox import SecondsWait, TakenInputPosition, TakenInputStart
from .child_process import ProcessIdentity
from .cli_bridges.claude_transcript import (
    WATCH_REPORT, WATCH_STOPPED, WINDOW_ENDED, ClaudeJsonLines, WatchReport,
)
from .cli_bridges.claude_protocol import ClaudeMonitorFrame
from .cli_bridges.history import RemoteWatchHistory
from .cli_bridges.shared import TranscriptConfirmsDelivery
from .comms import Comms, client_wire
from .connector_store import BearerSecret, digest
from .coordination_database import CoordinationStore
from .coordination_tables.harness_deliveries import HarnessDelivery
from .coordinator import Coordination
from .errors import RelationViolationError
from .harness_delivery_states import EmittedOutcome, ReceivingMonitor, UnconfirmedOutcome, UnrecordedMonitor
from .message_reference import MessageReference
from .messages import Message
from .nominal.field_codec import FieldCodec, ThreadLookup
from .runtime_updates import RuntimeUpdate
from .tool_result import ToolResult
from .tools import CommsInboxTool


@dataclass(frozen=True)
class InboxWatchUrl(ToolResult):
    """Where to open this thread's push; it opens once and only until it expires."""

    url: str
    expires_at: float


@dataclass(frozen=True)
class WatchTicket:
    """One thread's right to open one socket. It lives in the connector's memory, so a restart
    forgets it and the session asks again, as it does whenever its watch ends."""

    thread: str
    expires_at: float

    seconds: ClassVar[int] = 60

    def live(self, now: float) -> bool:
        return self.expires_at > now


DAMAGED_RECORD = "A record from the session's transcript was not valid JSON and was left out."
"""What a transcript line that is not JSON leaves behind."""

UNISSUED_TICKET = WatchTicket("", 0.0)
"""An unknown or spent ticket is simply one that has already expired."""


@dataclass(frozen=True)
class WatchRoute:
    """The path of an inbox watch, decoded once at the connector's boundary."""

    ticket: BearerSecret


@dataclass(frozen=True, eq=False)
class OpenWatch:
    """One thread's open socket; each is its own, so a newer one can replace it."""

    thread: str
    viewer: web.WebSocketResponse


@dataclass
class WatchTickets:
    held: dict[str, WatchTicket] = field(default_factory=dict)

    def issued(self, thread: str, issuer: URL) -> InboxWatchUrl:
        now = time.time()
        self.held = {key: ticket for key, ticket in self.held.items() if ticket.live(now)}
        token = BearerSecret(secrets.token_urlsafe(32))
        ticket = WatchTicket(thread, now + WatchTicket.seconds)
        self.held[digest(token)] = ticket
        scheme = {"http": "ws", "https": "wss"}[issuer.scheme]
        return InboxWatchUrl(str(issuer.with_scheme(scheme) / "watch" / token), ticket.expires_at)

    def taken(self, token: BearerSecret) -> WatchTicket:
        """The ticket is spent here, whatever happens to the socket it opens."""
        ticket = self.held.pop(digest(token), UNISSUED_TICKET)
        if not ticket.live(time.time()):
            raise web.HTTPForbidden(text="Unknown, used or expired URL; call comms_watch again.")
        return ticket


@dataclass(frozen=True)
class PushedTranscript(TranscriptConfirmsDelivery, ClaudeJsonLines):
    """A session's transcript as lines on its watch socket. Holding the ticket proved which
    thread this is, so these lines publish on that thread and no other, and the Monitor events
    in them confirm that thread's deliveries as a local bridge's transcript does."""

    comms: Comms
    transcript_thread: str
    socket: web.WebSocketResponse
    ledger_read: asyncio.Event = field(default_factory=asyncio.Event)
    """Set once the client's ledger is in: only then is an unconfirmed attempt provably unshown."""

    delivery_kind: ClassVar[type[RemoteWatchHistory]] = RemoteWatchHistory

    @cached_property
    def recipient_lookup(self) -> ThreadLookup:
        """The ticket's thread, decoded once for the socket's life."""
        return self.comms.registry.require(self.transcript_thread).incarnation.require_recorded().lookup

    async def emitted(self, reference: MessageReference) -> None:
        """The client printed this message, or was about to: its attempt is never sent again."""
        for delivery in await self.attempts(reference.message_id):
            if delivery.reference == reference:
                await self.finish(delivery, partial(replace, outcome=EmittedOutcome))

    async def transcript(self) -> AsyncIterator[RuntimeUpdate]:
        state, offset = self.start_state(), 0
        async for frame in self.socket:
            if frame.type is WSMsgType.TEXT and frame.data.startswith(WatchReport.wire_prefix):
                await WATCH_REPORT.validate_json(frame.data).received(self)
            elif frame.type is WSMsgType.TEXT and frame.data == WINDOW_ENDED:
                # The same gate a local bridge opens after replaying its window: without it the
                # reader stays in replay, so chat publishes and activity never does.
                for update in state.phase.window_ended(state):
                    yield update
            elif frame.type is WSMsgType.TEXT:
                raw = frame.data.encode()
                for update in self.line_updates(raw, state, offset, DAMAGED_RECORD):
                    yield update
                offset += len(raw) + 1


@dataclass(frozen=True)
class InboxWatch:
    """One open socket: the thread's bridge while it stays open."""

    thread: str
    root: Path

    wait_seconds: ClassVar[int] = 5

    @contextmanager
    def wire(self) -> Iterator[Comms]:
        with client_wire(self.root) as comms:
            yield comms

    def untaken(self, after: TakenInputPosition = TakenInputStart()) -> tuple[Message, ...]:
        """What no harness took after ``after``: a socket that took a message never has an
        older untaken one, so its next read starts past it instead of at the thread's birth."""
        with self.wire() as comms:
            request = CommsInboxTool(
                thread=self.thread, after=after, wait=SecondsWait(self.wait_seconds))
            return request.page(comms).page.messages

    def taken(self, message: Message) -> None:
        """A message leaves the inbox once its frame went out, but a written socket is not a
        shown event: the attempt stays unconfirmed until the session's transcript shows it.
        A message one Monitor event shows inline waits for that one part; one sent as a file
        has no Read this side can check, so it stays unconfirmed."""
        with self.wire() as comms:
            reference = message.reference
            incarnation = comms.registry.require(self.thread).incarnation
            frame = ClaudeMonitorFrame(message)
            delivery = HarnessDelivery(
                RemoteWatchHistory,
                incarnation.require_recorded().lookup,
                reference.message_id,
                reference.seq,
                UnconfirmedOutcome,
                monitor=ReceivingMonitor(len(frame.parts)) if frame.fits_one_event else UnrecordedMonitor(),
            )
            path = str(CoordinationStore.path_in(comms.root))
            with Coordination(path) as store, store.session.writing() as db:
                delivery.begin(store.assignments, db)

    def attached(self, here: ProcessIdentity) -> AbstractContextManager[object]:
        with self.wire() as comms:
            return comms.registry.external_process(self.thread, here)

    def holds(self, here: ProcessIdentity) -> bool:
        """Whether this watch still holds its thread; a stop on the bus releases it."""
        with self.wire() as comms:
            return comms.registry.require(self.thread).same_local_process(here)

    def unshown(self) -> tuple[Message, ...]:
        """The messages a socket of this incarnation took and no client has said it printed."""
        with self.wire() as comms:
            lookup = comms.registry.require(self.thread).incarnation.require_recorded().lookup
            with comms.registry.store.coordinator_reader.reader_snapshot() as db:
                attempts = HarnessDelivery.select(
                    db, bridge=RemoteWatchHistory, recipient_lookup=lookup, outcome=UnconfirmedOutcome)
            references = tuple(sorted((attempt.reference for attempt in attempts), key=lambda r: r.seq))
            return tuple(comms.bus.bus.messages_for_references(references)) if references else ()

    async def replayed(self, socket: web.WebSocketResponse, candidates: tuple[Message, ...],
                       ledger_read: asyncio.Event) -> None:
        """Tristan's replay with proof: a message taken before this socket opened goes again only
        once the client's ledger shows no client printed it. A client that sends no ledger proves
        nothing, so nothing is replayed."""
        await ledger_read.wait()
        still = {message.reference for message in await asyncio.to_thread(self.unshown)}
        for message in candidates:
            if message.reference in still:
                await socket.send_str(FieldCodec.compact(FieldCodec.encode(message)))

    async def pushed(self, socket: web.WebSocketResponse, here: ProcessIdentity) -> None:
        """Frames until it closes, or until the thread is stopped on the bus; a reader must run
        for the socket to notice closing. What earlier sockets took unshown is read before this
        one sends anything, so a message this socket sends is never replayed by it."""
        ledger_read = asyncio.Event()
        candidates = await asyncio.to_thread(self.unshown)
        reader = asyncio.create_task(self.read_until_closed(socket, ledger_read))
        replay = asyncio.create_task(self.replayed(socket, candidates, ledger_read))
        after: TakenInputPosition = TakenInputStart()
        try:
            while not socket.closed:
                for message in await asyncio.to_thread(self.untaken, after):
                    await socket.send_str(FieldCodec.compact(FieldCodec.encode(message)))
                    await asyncio.to_thread(self.taken, message)
                    after = after.advanced(message.reference)
                if not await asyncio.to_thread(self.holds, here):
                    await socket.close(code=WATCH_STOPPED, message=b"stopped on the bus")
        except ConnectionResetError:
            return
        finally:
            reader.cancel()
            replay.cancel()

    async def read_until_closed(self, socket: web.WebSocketResponse, ledger_read: asyncio.Event) -> None:
        """Publish what the session sends. A failure there must not cost it its push, so it is
        said on the bus and the socket keeps being read."""
        with self.wire() as comms:
            try:
                await PushedTranscript(comms, self.thread, socket, ledger_read).publish_transcript()
            except* Exception as failures:
                logging.exception("Transcript from %s stopped", self.thread)
                # A decoder's first line lists its whole union, so what went wrong is at the end.
                causes = "; ".join(f"{type(failure).__name__}: {str(failure)[-400:]}"
                                   for failure in failures.exceptions)
                comms.messaging.send(self.thread, "#comms", (
                    f"{self.thread}'s work stopped reaching the bus, though its push still works: "
                    f"{causes}"))
        async for _ in socket:
            pass

    async def carried(self, socket: web.WebSocketResponse) -> None:
        here = ProcessIdentity.capture(os.getpid())
        attachment = self.attached(here)
        try:
            await asyncio.to_thread(attachment.__enter__)
        except RelationViolationError as refused:
            await socket.close(
                code=WSCloseCode.POLICY_VIOLATION, message=str(refused)[:120].encode()
            )
            return
        try:
            await self.pushed(socket, here)
        finally:
            await asyncio.to_thread(attachment.__exit__, None, None, None)
