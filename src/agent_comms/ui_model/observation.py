"""Core observation for the comms UI, in a process of its own.

A UI process asks what to observe (the sidebar, the threads its views show)
and receives finished results only when Core's stores actually change: the
service watches the store files that ``HistoryViews.revision_paths`` declares,
reads snapshots and presentations, derives the sidebar rows, and sends them
over a pipe. The UI also asks it for reads it awaits: store reads (transcript
pages, their currency, message notifications), answered in arrival order, and
reads a thread's live owner answers over its socket (the goal snapshot, input
delivery), answered as they complete. Each answer carries the request id and
the decoded value or the exception the read raised. The UI thread no longer
polls revisions, reads stores, resolves owners, decodes replies or derives
rows, and no longer competes with that work for its interpreter lock.
"""

from __future__ import annotations

import asyncio
import itertools
import multiprocessing
import os
import traceback
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from agent_comms.coordination_errors import CoordinationReadUnavailable, StaleRevision

if TYPE_CHECKING:
    from multiprocessing.connection import Connection

    from agent_comms.acp_extension import TranscriptSnapshotUpdate
    from agent_comms.comms import Comms
    from agent_comms.history_views import RetiredViews
    from agent_comms.message_reference import MessageReference
    from agent_comms.presentation import CoordinationSnapshot, WireRevision
    from agent_comms.runtime import RuntimeConnection
    from agent_comms.thread_identity import ThreadIncarnation
    from agent_comms.thread_presentation import ThreadPresentation
    from agent_comms.transcripts import TranscriptCursor, TranscriptReadIdentity
    from agent_comms.ui_model.delivery import InputDelivery
    from agent_comms.ui_model.goal import OwnerGoalSnapshot
    from agent_comms.ui_model.sidebar import ChannelRowModel, ThreadRowModel

# Activity older than this reads as idle; re-derive rows at least this often
# so staleness shows without a store change.
EXPIRY_PERIOD = 30


@dataclass(frozen=True)
class Interest:
    """What the UI currently shows and so wants observed."""

    sidebar: ObserveSidebar | None = None
    threads: frozenset[str] = frozenset()
    views: ObserveViews | None = None


class ObservationRequest(ABC):
    """A UI request; each declares how the service receives it."""

    @abstractmethod
    def receive(self, service: ObservationService) -> None: ...


class InterestRequest(ObservationRequest):
    """Changes what is observed; the service observes again."""

    @abstractmethod
    def apply(self, interest: Interest) -> Interest: ...

    def receive(self, service: ObservationService) -> None:
        service.interest = self.apply(service.interest)
        service.observe_due = True
        service.changed.set()


@dataclass(frozen=True)
class AwaitedRead(ObservationRequest):
    """A read the UI awaits; the service answers it by ``request_id``.

    ``root`` is the Comms root the UI's view belongs to; a read for another
    root than the observed one is stale, never answered from the wrong stores.
    """

    root: str
    request_id: int = field(default=0, kw_only=True)

    def require_root(self, comms: Comms) -> None:
        if Path(self.root).expanduser().resolve() != comms.root.resolve():
            raise StaleRevision("The read belongs to another Comms root than the observed one")

    def refused(self, error: Exception) -> ReadRefused:
        # The read's own exception, re-raised to the UI caller exactly as an
        # in-process read raised it; the caller's handling decides.
        return ReadRefused(self.request_id, error, traceback.format_exc())


class ReadRequest(AwaitedRead):
    """A read of Core's stores, served one at a time in arrival order."""

    def receive(self, service: ObservationService) -> None:
        service.pending[self.request_id] = self
        service.changed.set()

    @abstractmethod
    def read(self, comms: Comms) -> object: ...

    def answer(self, comms: Comms) -> ReadAnswered | ReadRefused:
        try:
            self.require_root(comms)
            return ReadAnswered(self.request_id, self.read(comms))
        except Exception as error:
            return self.refused(error)


@dataclass(frozen=True)
class OwnerRead(AwaitedRead):
    """A read the live owner of thread incarnation ``owner`` answers over its socket.

    Served as it arrives, beside store reads; the owner's reply is decoded
    here. The owner's refusals and a changed owner identity reach the UI as
    ValueError, as the UI's owner actions report them.
    """

    owner: ThreadIncarnation
    # The owner answers from its own stores; a slower answer is a stalled owner.
    TIMEOUT: ClassVar[float] = 3

    def receive(self, service: ObservationService) -> None:
        service.owner_reads[self.request_id] = asyncio.ensure_future(self.serve(service))

    async def serve(self, service: ObservationService) -> None:
        try:
            answer = await self.answer(service.comms)
        finally:
            service.owner_reads.pop(self.request_id, None)
        service.connection.send(answer)

    async def answer(self, comms: Comms) -> ReadAnswered | ReadRefused:
        try:
            self.require_root(comms)
            async with asyncio.timeout(self.TIMEOUT):
                return ReadAnswered(self.request_id, await self.ask(comms))
        except Exception as error:
            return self.refused(error)

    def connect(self, comms: Comms) -> RuntimeConnection:
        from agent_comms.runtime import RuntimeConnection, socket_path

        owner = comms.registry.require(self.owner.name)
        if owner.incarnation != self.owner:
            raise ValueError("The owner incarnation changed while preparing the request.")
        return RuntimeConnection(comms, owner.name, socket_path(comms.root, owner.pid))

    async def ask(self, comms: Comms) -> object:
        from acp.exceptions import RequestError

        connection = await asyncio.to_thread(self.connect, comms)
        try:
            return await self.read(connection)
        except (RuntimeError, RequestError) as error:
            raise ValueError(str(error)) from error
        finally:
            await connection.close()

    @abstractmethod
    async def read(self, owner: RuntimeConnection) -> object: ...


@dataclass(frozen=True)
class CancelRead(ObservationRequest):
    """The UI stopped waiting; a read not yet answered is dropped."""

    request_id: int

    def receive(self, service: ObservationService) -> None:
        service.pending.pop(self.request_id, None)
        if (asking := service.owner_reads.pop(self.request_id, None)) is not None:
            asking.cancel()


@dataclass(frozen=True)
class ReadTranscript(ReadRequest):
    """One bounded transcript page and the witness it was read under.

    Without ``read_identity`` the source is captured now; with it, the page is
    read under that published witness and is stale if the source moved on.
    """

    thread: str
    before: TranscriptCursor | None = None
    after: TranscriptCursor | None = None
    through: TranscriptCursor | None = None
    read_identity: TranscriptReadIdentity | None = None
    historical_source: str | None = None

    def read(self, comms: Comms) -> TranscriptSnapshotUpdate:
        from agent_comms.acp_extension import TranscriptSnapshotUpdate

        transcripts = comms.transcripts
        window = dict(before=self.before, after=self.after, through=self.through)
        if self.read_identity is None:
            read = transcripts.capture_page_read(self.thread, historical_source=self.historical_source, **window)
        else:
            if (self.historical_source is not None
                    and self.historical_source != self.read_identity.historical_source):
                raise StaleRevision("Published transcript belongs to another recorded source")
            read = transcripts.bind_page_read(self.thread, self.read_identity, **window)
        # The read checks its witness before and after preparing the page.
        return TranscriptSnapshotUpdate(read.read(), read.identity)


@dataclass(frozen=True)
class RefreshTranscript(ReadRequest):
    """A published page's witness: None while its content is current, else the current page."""

    identity: TranscriptReadIdentity

    def read(self, comms: Comms) -> TranscriptSnapshotUpdate | None:
        from agent_comms.transcripts import TranscriptRead

        identity = self.identity
        if TranscriptRead(comms.transcripts, identity).content_current():
            return None
        return ReadTranscript(
            self.root, identity.requested_name, before=identity.before, after=identity.after,
            through=identity.through, historical_source=identity.historical_source,
        ).read(comms)


@dataclass(frozen=True)
class ReadNotifications(ReadRequest):
    """Recipient handling of the referenced original messages."""

    references: tuple[MessageReference, ...]

    def read(self, comms: Comms):
        return comms.views.message_notifications_for_references(self.references)


@dataclass(frozen=True)
class ReadOwnerGoal(OwnerRead):
    """The owner's goal snapshot and the turn it read with it."""

    async def read(self, owner: RuntimeConnection) -> OwnerGoalSnapshot:
        from agent_comms.ui_model.goal import OwnerGoalSnapshot

        return OwnerGoalSnapshot.from_owner(await owner.request("goal_snapshot"))


@dataclass(frozen=True)
class ReadInputDelivery(OwnerRead):
    """The owner's delivery notices, checked against its live queue."""

    include_history: bool = False

    async def read(self, owner: RuntimeConnection) -> InputDelivery:
        from agent_comms.ui_model.delivery import InputDelivery

        return InputDelivery.from_wire(
            await owner.request("input_dispositions", include_history=self.include_history))


@dataclass(frozen=True)
class ObserveSidebar(InterestRequest):
    worktree: str
    show_stopped: bool
    show_archived: bool

    def apply(self, interest: Interest) -> Interest:
        return replace(interest, sidebar=self)


@dataclass(frozen=True)
class StopSidebar(InterestRequest):
    def apply(self, interest: Interest) -> Interest:
        return replace(interest, sidebar=None)


@dataclass(frozen=True)
class ObserveThreads(InterestRequest):
    names: frozenset[str]

    def apply(self, interest: Interest) -> Interest:
        return replace(interest, threads=self.names)


@dataclass(frozen=True)
class ObserveViews(InterestRequest):
    """The thread incarnations and channel views open in the UI, to learn when one is retired."""

    threads: frozenset
    channels: frozenset[str]

    def apply(self, interest: Interest) -> Interest:
        return replace(interest, views=self)


class ServiceResult(ABC):
    """What the service sends; each declares how the UI side receives it."""

    @abstractmethod
    def received(self, process: ObservationProcess, observed: list[Observed]) -> None: ...


@dataclass(frozen=True)
class Observed(ServiceResult):
    """A result, as of one revision of Core's stores."""

    revision: WireRevision

    def received(self, process: ObservationProcess, observed: list[Observed]) -> None:
        observed.append(self)


@dataclass(frozen=True)
class ReadAnswered(ServiceResult):
    request_id: int
    value: object

    def received(self, process: ObservationProcess, observed: list[Observed]) -> None:
        if (waiter := process.reads.pop(self.request_id, None)) is not None and not waiter.done():
            waiter.set_result(self.value)


@dataclass(frozen=True)
class ReadRefused(ServiceResult):
    """The read raised ``error``; the UI caller receives that same exception."""

    request_id: int
    error: Exception
    trace: str

    def received(self, process: ObservationProcess, observed: list[Observed]) -> None:
        if (waiter := process.reads.pop(self.request_id, None)) is not None and not waiter.done():
            self.error.add_note(f"Raised in the comms observation service:\n{self.trace}")
            waiter.set_exception(self.error)


@dataclass(frozen=True)
class RevisionObserved(Observed):
    """Core's stores changed; consumers that only need to know react to this."""


@dataclass(frozen=True)
class SidebarObserved(Observed):
    request: ObserveSidebar
    snapshot: CoordinationSnapshot
    derived: tuple[dict[str, ChannelRowModel], dict[str, ThreadRowModel]]


@dataclass(frozen=True)
class ThreadsObserved(Observed):
    """Presentations of the observed threads; a busy store leaves some unread this time."""

    presentations: Mapping[str, ThreadPresentation | None]
    unavailable: frozenset[str]
    busy: frozenset[str]


@dataclass(frozen=True)
class ViewsRetired(Observed):
    """Open views whose thread was deleted or replaced, or whose channel is gone or archived."""

    retired: RetiredViews


@dataclass(frozen=True)
class ObservationFailed(ServiceResult):
    """The service failed; the UI raises this, it does not run on stale data."""

    error: str

    def received(self, process: ObservationProcess, observed: list[Observed]) -> None:
        raise RuntimeError(f"The comms observation service failed:\n{self.error}")


class ObservationService:
    """The child process side: one Comms service, the UI's interest, and what was sent."""

    # Core's read failures a status line shows as "unavailable"; Core has no
    # common base for them yet, so these are the families its reads raise.
    UNAVAILABLE = (OSError, ValueError, RuntimeError)

    def __init__(self, comms, connection: Connection):
        self.comms = comms
        self.connection = connection
        self.interest = Interest()
        self.changed = asyncio.Event()
        self.revision: WireRevision | None = None
        self.sent_sidebar: tuple[ObserveSidebar, int] | None = None
        self.sent_threads: frozenset[str] | None = None
        self.checked_views: tuple[ObserveViews, WireRevision] | None = None
        self.retry_threads = False
        self.observe_due = True
        # Store reads the UI awaits, in arrival order, until served or cancelled.
        self.pending: dict[int, ReadRequest] = {}
        # Owner reads being asked, until answered or cancelled.
        self.owner_reads: dict[int, asyncio.Future] = {}
        self.closed = False

    def receive(self) -> None:
        while self.connection.poll():
            try:
                request = self.connection.recv()
            except EOFError:
                self.closed = True
                self.changed.set()
                return
            request.receive(self)

    def store_names(self) -> frozenset[bytes]:
        def paths(value) -> Iterator[Path]:
            yield from value if isinstance(value, tuple) else (value,)

        return frozenset(os.fsencode(path.name) for value in self.comms.views.revision_paths().values()
                         for path in paths(value) if path.parent == self.comms.root)

    async def watch(self) -> None:
        from agent_comms.wire_watch import WireWatch

        async for _ in WireWatch.observations(self.comms.root, self.store_names(), modified=True):
            self.observe_due = True
            self.changed.set()

    def observe(self) -> None:
        from agent_comms.ui_model.sidebar import SidebarModel

        views = self.comms.views
        revision = views.revision()
        stores = self.revision is None or revision.stores_changed_since(self.revision)
        self.revision = revision
        if stores:
            self.connection.send(RevisionObserved(revision))
        sidebar = self.interest.sidebar
        if sidebar is not None:
            key = (sidebar, revision.expiry_tick // EXPIRY_PERIOD)
            if stores or key != self.sent_sidebar:
                snapshot = views.viewer_snapshot(
                    sidebar.worktree, show_stopped=sidebar.show_stopped, show_archived=sidebar.show_archived)
                self.connection.send(SidebarObserved(revision, sidebar, snapshot, SidebarModel.derive(snapshot)))
                self.sent_sidebar = key
        threads = self.interest.threads
        if threads and (stores or threads != self.sent_threads or self.retry_threads):
            presentations, unavailable, busy = {}, set(), set()
            for name in threads:
                try:
                    presentations[name] = views.thread_presentation(name)
                except CoordinationReadUnavailable:
                    busy.add(name)
                except self.UNAVAILABLE:
                    unavailable.add(name)
            self.connection.send(ThreadsObserved(revision, presentations, frozenset(unavailable), frozenset(busy)))
            self.sent_threads, self.retry_threads = threads, bool(busy)
        views = self.interest.views
        if views is not None and (self.checked_views is None or self.checked_views[0] != views
                                  or revision.registrations_changed_since(self.checked_views[1])):
            retired = self.comms.views.retired_views(views.threads, views.channels)
            if retired.threads or retired.channels:
                self.connection.send(ViewsRetired(revision, retired))
            self.checked_views = (views, revision)

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        loop.add_reader(self.connection.fileno(), self.receive)
        watching = asyncio.create_task(self.watch())
        try:
            while not self.closed:
                await self.changed.wait()
                self.changed.clear()
                # Awaited reads first, one at a time; between them the loop
                # takes new requests and cancellations.
                while self.pending and not self.closed:
                    request_id = next(iter(self.pending))
                    self.connection.send(self.pending.pop(request_id).answer(self.comms))
                    await asyncio.sleep(0)
                if self.observe_due and not self.closed:
                    self.observe_due = False
                    self.observe()
        finally:
            watching.cancel()
            for asking in self.owner_reads.values():
                asking.cancel()
            loop.remove_reader(self.connection.fileno())


def serve(root: str, connection: Connection) -> None:
    """Child process entry: observe ``root`` for the UI on the other end of ``connection``."""
    from agent_comms.active_route import resolve_comms_route
    from agent_comms.comms import wire

    try:
        route = resolve_comms_route(Path(root))
        with route.admit_client():
            asyncio.run(ObservationService(wire(route), connection).run())
    except BaseException:
        try:
            connection.send(ObservationFailed(traceback.format_exc()))
        except OSError:
            pass
        raise
    finally:
        connection.close()


class ObservationProcess:
    """The UI side: start the service for a root, send requests, await reads, drain results."""

    def __init__(self, root: Path):
        context = multiprocessing.get_context("spawn")
        self.connection, child = context.Pipe()
        self.process = context.Process(target=serve, args=(str(root), child), daemon=True,
                                       name="agent-comms-observation")
        self.process.start()
        child.close()
        self.request_ids = itertools.count(1)
        self.reads: dict[int, asyncio.Future] = {}

    def fileno(self) -> int:
        return self.connection.fileno()

    def request(self, request: ObservationRequest) -> None:
        self.connection.send(request)

    async def read(self, request: AwaitedRead):
        """Send ``request`` and await its answer, delivered by ``results``.

        Cancelling the caller cancels the read; an unserved read is dropped.
        """
        request = replace(request, request_id=next(self.request_ids))
        waiter = asyncio.get_running_loop().create_future()
        self.reads[request.request_id] = waiter
        try:
            self.connection.send(request)
            return await waiter
        except asyncio.CancelledError:
            if self.reads.pop(request.request_id, None) is not None and not self.connection.closed:
                self.connection.send(CancelRead(request.request_id))
            raise
        finally:
            self.reads.pop(request.request_id, None)

    def abandon_reads(self, error: Exception) -> None:
        """The UI stopped using this service: its awaited reads fail with ``error``."""
        reads, self.reads = self.reads, {}
        for waiter in reads.values():
            if not waiter.done():
                waiter.set_exception(error)

    def results(self) -> list[Observed]:
        """Every observation waiting on the pipe; answered reads resolve their waiters.

        An ObservationFailed is raised here.
        """
        observed: list[Observed] = []
        while self.connection.poll():
            try:
                result = self.connection.recv()
            except (EOFError, ConnectionResetError) as error:
                raise RuntimeError("The comms observation service exited") from error
            result.received(self, observed)
        return observed

    def close(self) -> None:
        self.connection.close()
        self.process.join(timeout=2)
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(timeout=2)
