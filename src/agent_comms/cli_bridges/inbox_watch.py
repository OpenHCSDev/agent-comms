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
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field, replace
from functools import cached_property, partial
from pathlib import Path
from typing import ClassVar

from aiohttp import WSCloseCode, WSMsgType, web
from yarl import URL

from agent_comms.cli_bridges.claude_protocol import ClaudeMonitorFrame
from agent_comms.cli_bridges.claude_transcript import (
    WATCH_DEFERRED,
    WATCH_REPORT,
    WATCH_STOPPED,
    WINDOW_ENDED,
    ClaudeJsonLines,
    ReceivesWatchReports,
    ReconnectUrl,
    ReportedLedger,
    WatchReport,
)
from agent_comms.cli_bridges.harness_delivery_states import (
    EmittedOutcome,
    TakingLedger,
    UnconfirmedOutcome,
)
from agent_comms.cli_bridges.history import RemoteWatchHistory
from agent_comms.cli_bridges.open_viewer import OpenViewer
from agent_comms.cli_bridges.runtime_updates import RuntimeUpdate
from agent_comms.cli_bridges.shared import MonitorReadFile, TranscriptConfirmsDelivery
from agent_comms.comms import Comms, client_wire
from agent_comms.coordination.coordinator import Coordination
from agent_comms.coordination.database import CoordinationStore
from agent_comms.coordination.tables.harness_deliveries import HarnessDelivery
from agent_comms.errors import RelationViolationError
from agent_comms.execution.child_process import ProcessIdentity
from agent_comms.messaging.agent_inbox import NoWait, TakenInputPosition, TakenInputStart
from agent_comms.messaging.message_reference import MessageReference
from agent_comms.messaging.messages import Message
from agent_comms.messaging.watched import ChangeSignal, FileSource
from agent_comms.nominal.field_codec import FieldCodec, ThreadLookup
from agent_comms.package_stamp import STAMP
from agent_comms.storage.locked_store import RegistryFile
from agent_comms.storage.maintenance_barrier import MaintenanceHeldError
from agent_comms.transport.connector_profile import BearerSecret
from agent_comms.transport.connector_store import ConnectorStore, digest
from agent_comms.transport.tool_result import ToolResult
from agent_comms.transport.tools.inbox import CommsInboxTool


@dataclass(frozen=True)
class InboxWatchUrl(ToolResult):
    """Where to open this thread's push; it opens once and only until it expires."""

    url: str
    expires_at: float


@dataclass(frozen=True)
class WatchTicket(ABC):
    """One thread's right to open one socket."""

    thread: str

    @abstractmethod
    def live(self, now: float) -> bool: ...

    opened_on: ClassVar[str]
    """How the connector's log names a socket opened with this kind of ticket."""

    async def opened(self, offer: ReconnectUrl, viewer: web.WebSocketResponse) -> None:
        """What the new socket is sent before anything else: by default nothing, since only a
        client that says it reconnects can read a ReconnectUrl frame."""

    def redeemed(self, store: ConnectorStore, token: BearerSecret) -> WatchTicket:
        """The ticket the URL opens with once memory has answered: what memory held."""
        return self


@dataclass(frozen=True)
class ArmedTicket(WatchTicket):
    """What comms_watch hands the model: good for one minute, in the connector's memory, so a
    restart forgets it and the model asks again, as it would once the minute passed."""

    expires_at: float

    seconds: ClassVar[int] = 60
    opened_on = "an armed ticket"

    def live(self, now: float) -> bool:
        return self.expires_at > now


@dataclass(frozen=True)
class ReconnectTicket(WatchTicket):
    """What a reset socket's client re-dials with. A network reset can strike at any point in a
    watch's life, so it has no clock: it stands until it is redeemed or any newer watch of its
    thread opens. It is kept in the connector's store (WatchReconnect), because the reset that
    matters most is the connector's own restart."""

    opened_on = "a reconnect ticket"

    def live(self, now: float) -> bool:
        return True

    async def opened(self, offer: ReconnectUrl, viewer: web.WebSocketResponse) -> None:
        """Only a client that reconnects alone redeems this ticket, so its next URL goes first,
        before any message: resets come in bursts, and a socket hit again within seconds must
        already hold it (2026-11-05 21:51: a re-dialled socket dropped 8 s after opening, while
        its URL still waited on the transcript reader)."""
        await viewer.send_str(offer.line())
        logging.info("Watch for %s sent its reconnect URL on opening", self.thread)


@dataclass(frozen=True)
class UnheldTicket(WatchTicket):
    """A URL the connector's memory holds no armed ticket for: a reconnect ticket kept in its
    store, or nothing. Only this asks the store, so opening an armed watch writes nothing."""

    opened_on = "no ticket"

    def live(self, now: float) -> bool:
        return False

    def redeemed(self, store: ConnectorStore, token: BearerSecret) -> WatchTicket:
        refused: WatchTicket = self
        return store.watch_reconnect_taken(token).reopened(ReconnectTicket, refused)


DAMAGED_RECORD = "A record from the session's transcript was not valid JSON and was left out."
"""What a transcript line that is not JSON leaves behind."""


UNHELD_TICKET = UnheldTicket("")
"""What memory answers for a URL it holds no armed ticket for."""


@dataclass(frozen=True)
class WatchRoute:
    """The path of an inbox watch, decoded once at the connector's boundary."""

    ticket: BearerSecret


@dataclass(frozen=True, eq=False, kw_only=True)
class OpenWatch(OpenViewer):
    """One thread's open socket; each is its own, so a newer one can replace it."""

    thread: str

    def watching(self, thread: str) -> bool:
        return self.thread == thread


@dataclass
class WatchTickets:
    """Armed tickets in memory for their minute; reconnect tickets in the connector's store."""

    store: ConnectorStore
    held: dict[str, WatchTicket] = field(default_factory=dict)

    def issued(self, thread: str, issuer: URL) -> InboxWatchUrl:
        now = time.time()
        ticket = ArmedTicket(thread, now + ArmedTicket.seconds)
        token = BearerSecret(secrets.token_urlsafe(32))
        self.held = {key: held for key, held in self.held.items() if held.live(now)}
        self.held[digest(token)] = ticket
        return InboxWatchUrl(self.url(token, issuer), ticket.expires_at)

    def reconnect(self, thread: str, issuer: URL) -> ReconnectUrl:
        """One standing reconnect ticket per thread: minting it ends any older one."""
        return ReconnectUrl(url=self.url(self.store.watch_reconnect_issued(thread), issuer))

    @staticmethod
    def url(token: BearerSecret, issuer: URL) -> str:
        scheme = {"http": "ws", "https": "wss"}[issuer.scheme]
        return str(issuer.with_scheme(scheme) / "watch" / token)

    def taken(self, token: BearerSecret) -> WatchTicket:
        """The ticket is spent here, whatever happens to the socket it opens: a reconnect ticket
        from the store, an armed one from memory. The socket it opens mints its thread's next
        reconnect ticket, which ends any older one."""
        ticket = self.held.pop(digest(token), UNHELD_TICKET).redeemed(self.store, token)
        if not ticket.live(time.time()):
            raise web.HTTPForbidden(text="Unknown, used or expired URL; call comms_watch again.")
        return ticket


@dataclass(frozen=True)
class PushedTranscript(ReceivesWatchReports, TranscriptConfirmsDelivery, ClaudeJsonLines):
    """A session's transcript as lines on its watch socket. Holding the ticket proved which
    thread this is, so these lines publish on that thread and no other, and the Monitor events
    in them confirm that thread's deliveries as a local bridge's transcript does."""

    comms: Comms
    transcript_thread: str
    socket: web.WebSocketResponse
    offer: ReconnectUrl
    reported: ReportedLedger = field(default_factory=lambda: ReportedLedger())

    delivery_kind: ClassVar[type[RemoteWatchHistory]] = RemoteWatchHistory
    incoming_frame: ClassVar[type[ClaudeMonitorFrame]] = ClaudeMonitorFrame

    def exported(self, path: str) -> tuple[MonitorReadFile, ...]:
        """The file is on the session's machine, wherever its client keeps exports: its name
        gives the reference and read_parts judges the lines against the bus message, so the
        directory only locates the file and never vouches for it. The client keeps the file."""
        return MonitorReadFile.exported_at(Path(path).parent, path)

    @cached_property
    def recipient_lookup(self) -> ThreadLookup:
        """The ticket's thread, decoded once for the socket's life."""
        incarnation = self.comms.registry.require(self.transcript_thread).incarnation
        return incarnation.require_recorded().lookup

    async def emitted(self, printed: frozenset[MessageReference]) -> None:
        """The client printed these, or was about to: their attempts are never sent again. One
        read of this recipient's unconfirmed attempts, however long the ledger is."""
        unconfirmed = await self.coordinator_read(lambda db: tuple(HarnessDelivery.select(
            db, bridge=self.delivery_kind, recipient_lookup=self.recipient_lookup,
            outcome=UnconfirmedOutcome)))
        for delivery in unconfirmed:
            if delivery.reference in printed:
                await self.finish(delivery, partial(replace, outcome=EmittedOutcome))

    async def reconnectable(self) -> None:
        """The socket's own standing URL, the one minted when it opened: a second mint would
        end the first while its frame might still be lost in a reset."""
        await self.socket.send_str(self.offer.line())
        logging.info("Watch for %s sent its reconnect URL on request", self.transcript_thread)

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
class TranscriptStopped:
    """What #comms is told when a session's transcript stops reaching the bus."""

    thread: str
    failures: BaseExceptionGroup
    host_commit: str

    def text(self) -> str:
        # A decoder's first line lists its whole union, so what went wrong is at the end.
        causes = "; ".join(f"{type(failure).__name__}: {str(failure)[-400:]}"
                           for failure in self.failures.exceptions)
        # A watch run from a newer commit sends frames this host has never declared;
        # naming the host's build is what tells that apart from a broken frame.
        return (f"{self.thread}'s work stopped reaching the bus, though its push still works: "
                f"{causes}. This host runs {self.host_commit[:12]}; if this watch runs from a "
                f"newer commit, run it from {self.host_commit[:12]}.")


@dataclass(frozen=True)
class InboxWatch:
    """One open socket: the thread's bridge while it stays open."""

    thread: str
    root: Path
    offer: ReconnectUrl
    """The socket's one standing reconnect URL, minted when it opened."""

    recheck_seconds: ClassVar[float] = 5
    """With no registry write to wake it, how often an open socket asks again whether this
    watch still holds its thread."""

    @contextmanager
    def wire(self) -> Iterator[Comms]:
        with client_wire(self.root) as comms:
            yield comms

    def untaken(self, after: TakenInputPosition = TakenInputStart()) -> tuple[Message, ...]:
        """What no harness took after ``after``: a socket that took a message never has an
        older untaken one, so its next read starts past it instead of at the thread's birth."""
        with self.wire() as comms:
            request = CommsInboxTool(
                thread=self.thread, after=after, wait=NoWait())
            return request.messages(comms)

    def taken(self, message: Message, ledger: TakingLedger) -> None:
        """A message leaves the inbox before its frame goes out: the client may ack a print
        before a later write could land, and an attempt recorded but never sent is exactly what
        replay with proof sends again. The attempt stays unconfirmed until the session's
        transcript shows it.
        A message one Monitor event shows inline waits for that one part; one sent as a file
        has no Read this side can check, so it stays unconfirmed. The attempt names the ledger
        it was taken under: only that ledger's silence can prove it was never printed."""
        with self.wire() as comms:
            reference = message.reference
            incarnation = comms.registry.require(self.thread).incarnation
            delivery = HarnessDelivery(
                RemoteWatchHistory,
                incarnation.require_recorded().lookup,
                reference.message_id,
                reference.seq,
                UnconfirmedOutcome,
                monitor=ClaudeMonitorFrame(message).shown_evidence(ledger),
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

    def unconfirmed(self) -> tuple[HarnessDelivery, ...]:
        """What sockets of this incarnation took that no client has said it printed."""
        with self.wire() as comms:
            lookup = comms.registry.require(self.thread).incarnation.require_recorded().lookup
            with comms.registry.store.coordinator_reader.reader_snapshot() as db:
                return tuple(HarnessDelivery.select(
                    db, bridge=RemoteWatchHistory, recipient_lookup=lookup,
                    outcome=UnconfirmedOutcome))

    def unshown(
        self, before: frozenset[MessageReference], ledger: TakingLedger,
    ) -> tuple[Message, ...]:
        """Of the attempts taken before this socket opened, those this ledger took and, its
        report in, still unconfirmed: provably never printed."""
        references = tuple(sorted((
            attempt.reference for attempt in self.unconfirmed()
            if attempt.reference in before and attempt.monitor.taken_under(ledger)),
            key=lambda reference: reference.seq))
        with self.wire() as comms:
            return tuple(comms.bus.bus.messages_for_references(references)) if references else ()

    async def replayed(self, socket: web.WebSocketResponse, before: frozenset[MessageReference],
                       reported: ReportedLedger) -> None:
        """Tristan's replay with proof: a message goes again only when the ledger that took it
        reports it never printed. A client that sends no ledger, or a ledger newer than the
        attempt, proves nothing, so nothing is replayed."""
        await reported.arrival.wait()
        for message in await asyncio.to_thread(self.unshown, before, reported.current):
            await socket.send_str(FieldCodec.compact(FieldCodec.encode(message)))

    def commit_sources(self) -> tuple[FileSource, ...]:
        """The bus's commit signal, read in a thread so no Comms is built or held on the loop."""
        with self.wire() as comms:
            return comms.bus.bus.commit_sources()

    def registry_sources(self) -> tuple[FileSource, ...]:
        """The registry file a stop on the bus writes."""
        return tuple(FileSource(path) for path in RegistryFile.source_paths(self.root))

    async def pushed(self, socket: web.WebSocketResponse, here: ProcessIdentity) -> None:
        """Frames until it closes, or until the thread is stopped on the bus; a reader must run
        for the socket to notice closing. What earlier sockets took unconfirmed is read before
        this one sends anything, so a message this socket sends is never replayed by it. Nothing
        waits on the client's ledger: a message taken before it lands is taken under none.
        Whichever of sending, holding and reading ends first ends the socket, and its result is
        read, so a failed change signal is raised instead of dying in its task."""
        reported = ReportedLedger()
        taken = await asyncio.to_thread(self.unconfirmed)
        before = frozenset(attempt.reference for attempt in taken)
        commits = await asyncio.to_thread(self.commit_sources)
        replay = asyncio.create_task(self.replayed(socket, before, reported))
        running = {
            asyncio.create_task(self.read_until_closed(socket, reported)),
            asyncio.create_task(self.sending(socket, commits, reported)),
            asyncio.create_task(self.holding(socket, here)),
        }
        try:
            finished, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
            for task in finished:
                task.result()
        except ConnectionResetError:
            return
        finally:
            for task in (*running, replay):
                task.cancel()

    async def sending(
        self, socket: web.WebSocketResponse, commits: tuple[FileSource, ...],
        reported: ReportedLedger,
    ) -> None:
        """Send what no harness took, woken by each bus commit; nothing blocks a thread between
        commits, so a closed socket ends its handler at once."""
        after: TakenInputPosition = TakenInputStart()
        async with ChangeSignal.opened(*commits) as changes:
            while True:
                # Drained before the read, so a commit landing during it wakes the wait.
                changes.prepare()
                for message in await asyncio.to_thread(self.untaken, after):
                    await asyncio.to_thread(self.taken, message, reported.current)
                    await socket.send_str(FieldCodec.compact(FieldCodec.encode(message)))
                    after = after.advanced(message.reference)
                await changes.wait()

    async def holding(self, socket: web.WebSocketResponse, here: ProcessIdentity) -> None:
        """Close the socket once this watch no longer holds its thread, and end once the socket
        has closed. A stop is a registry write, which wakes this at once; the recheck covers
        what no file announces, a close included: a client that never answers the server's
        CLOSE leaves the reader waiting on frames that never come, so this is what ends the
        handler instead of aiohttp's shutdown timeout."""
        async with ChangeSignal.opened(*self.registry_sources()) as writes:
            while not socket.closed:
                writes.prepare()
                if not await asyncio.to_thread(self.holds, here):
                    await socket.close(code=WATCH_STOPPED, message=b"stopped on the bus")
                    return
                try:
                    await asyncio.wait_for(writes.wait(), self.recheck_seconds)
                except TimeoutError:
                    continue

    async def read_until_closed(
        self, socket: web.WebSocketResponse, reported: ReportedLedger,
    ) -> None:
        """Publish what the session sends. A failure there must not cost it its push, so it is
        said on the bus and the socket keeps being read."""
        # Read before the transcript runs, so a failing read cannot cost the report below.
        host_commit = await asyncio.to_thread(STAMP.commit)
        with self.wire() as comms:
            try:
                await PushedTranscript(
                    comms, self.thread, socket, self.offer, reported,
                ).publish_transcript()
            except* Exception as failures:
                logging.exception("Transcript from %s stopped", self.thread)
                comms.messaging.send(self.thread, "#comms", TranscriptStopped(self.thread, failures, host_commit).text())
        async for _ in socket:
            pass

    async def carried(self, socket: web.WebSocketResponse) -> None:
        here = ProcessIdentity.capture(os.getpid())
        attachment = self.attached(here)
        try:
            await asyncio.to_thread(attachment.__enter__)
        except MaintenanceHeldError as held:
            await socket.close(code=WATCH_DEFERRED, message=str(held)[:120].encode())
            return
        except RelationViolationError as refused:
            await socket.close(
                code=WSCloseCode.POLICY_VIOLATION, message=str(refused)[:120].encode()
            )
            return
        try:
            await self.pushed(socket, here)
        finally:
            await asyncio.to_thread(attachment.__exit__, None, None, None)
