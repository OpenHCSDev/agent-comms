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
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from aiohttp import WSCloseCode, WSMsgType, web
from yarl import URL

from .agent_inbox import SecondsWait
from .child_process import ProcessIdentity
from .cli_bridges.claude_transcript import WINDOW_ENDED, ClaudeJsonLines
from .cli_bridges.history import RemoteWatchHistory
from .comms import Comms, client_wire
from .connector_store import BearerSecret, digest
from .coordination_database import CoordinationStore
from .coordination_tables.harness_deliveries import HarnessDelivery
from .coordinator import Coordination
from .errors import RelationViolationError
from .harness_delivery_states import DeliveredOutcome
from .messages import Message
from .nominal.field_codec import FieldCodec
from .runtime_updates import RuntimeUpdate
from .tool_caller import ThreadCaller
from .tool_result import ToolResult
from .tools import CommsInboxTool
from .transcript_publication import PublishesToBus


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
class PushedTranscript(ClaudeJsonLines, PublishesToBus):
    """A session's transcript as lines on its watch socket. Holding the ticket proved which
    thread this is, so these lines publish on that thread and no other."""

    comms: Comms
    transcript_thread: str
    socket: web.WebSocketResponse

    async def transcript(self) -> AsyncIterator[RuntimeUpdate]:
        state, offset = self.start_state(), 0
        async for frame in self.socket:
            if frame.type is WSMsgType.TEXT and frame.data == WINDOW_ENDED:
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

    def untaken(self) -> tuple[Message, ...]:
        with self.wire() as comms:
            request = CommsInboxTool(thread=self.thread, wait=SecondsWait(self.wait_seconds))
            return request.apply(comms, ThreadCaller(self.thread)).messages

    def taken(self, message: Message) -> None:
        """A message leaves the inbox only after its frame went out, so a dropped socket
        replays at most the one in hand."""
        with self.wire() as comms:
            reference = message.reference
            incarnation = comms.registry.require(self.thread).incarnation
            delivery = HarnessDelivery(
                RemoteWatchHistory,
                incarnation.require_recorded().lookup,
                reference.message_id,
                reference.seq,
                DeliveredOutcome,
            )
            path = str(CoordinationStore.path_in(comms.root))
            with Coordination(path) as store, store.session.writing() as db:
                delivery.begin(store.assignments, db)

    def attached(self) -> AbstractContextManager[object]:
        here = ProcessIdentity.capture(os.getpid())
        with self.wire() as comms:
            return comms.registry.external_process(self.thread, here)

    async def pushed(self, socket: web.WebSocketResponse) -> None:
        """Frames until it closes; a reader must run for the socket to notice closing."""
        reader = asyncio.create_task(self.read_until_closed(socket))
        try:
            while not socket.closed:
                for message in await asyncio.to_thread(self.untaken):
                    await socket.send_str(FieldCodec.compact(FieldCodec.encode(message)))
                    await asyncio.to_thread(self.taken, message)
        except ConnectionResetError:
            return
        finally:
            reader.cancel()

    async def read_until_closed(self, socket: web.WebSocketResponse) -> None:
        """Publish what the session sends. A failure there must not cost it its push, so it is
        said on the bus and the socket keeps being read."""
        with self.wire() as comms:
            try:
                await PushedTranscript(comms, self.thread, socket).publish_transcript()
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
        attachment = self.attached()
        try:
            await asyncio.to_thread(attachment.__enter__)
        except RelationViolationError as refused:
            await socket.close(
                code=WSCloseCode.POLICY_VIOLATION, message=str(refused)[:120].encode()
            )
            return
        try:
            await self.pushed(socket)
        finally:
            await asyncio.to_thread(attachment.__exit__, None, None, None)
