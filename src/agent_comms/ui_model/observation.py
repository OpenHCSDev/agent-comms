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
import signal
import sys
import time
import traceback
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Generic, TypeVar

from agent_comms.coordination_errors import (
    CoordinationError,
    CoordinationReadUnavailable,
    StaleRevision,
)
from agent_comms.errors import RelationViolationError, StoredHistoryUnreadable
from agent_comms.acp_extension import TranscriptDraftUpdate, TranscriptSnapshotUpdate

if TYPE_CHECKING:
    from multiprocessing.connection import Connection

    from agent_comms.comms import Comms
    from agent_comms.history_views import RetiredViews
    from agent_comms.message_reference import MessageReference
    from agent_comms.presentation import CoordinationSnapshot, WireRevision
    from agent_comms.runtime import RuntimeConnection
    from agent_comms.thread_identity import ThreadIncarnation
    from agent_comms.thread_presentation import ThreadPresentation
    from agent_comms.transcripts import TranscriptCursor, TranscriptRead, TranscriptReadIdentity
    from agent_comms.ui_model.delivery import InputDelivery
    from agent_comms.ui_model.goal import OwnerGoalSnapshot
    from agent_comms.ui_model.sidebar import ChannelRowModel, ThreadRowModel

# Activity older than this reads as idle; re-derive rows at least this often
# so staleness shows without a store change.
EXPIRY_PERIOD = 30


class ObservationRequest(ABC):
    """A UI request; each declares how the service receives it."""

    @abstractmethod
    def receive(self, service: ObservationService) -> None: ...


class InterestRequest(ObservationRequest):
    """Changes what is observed; the service observes again."""

    @abstractmethod
    def observation(self) -> Observation: ...

    def receive(self, service: ObservationService) -> None:
        observation = self.observation()
        previous = service.observations.get(type(observation))
        if previous is None or previous.request != self:
            service.observations[type(observation)] = observation
        service.observe_due = True
        service.changed.set()


InterestT = TypeVar("InterestT", bound=InterestRequest)


class Observation(ABC, Generic[InterestT]):
    """A requested view owns its read state and yields between store operations."""

    def __init__(self, request: InterestT):
        self.request = request

    @abstractmethod
    def read(self, comms: Comms, revision: WireRevision) -> Iterator[Observed | None]: ...


class UnreadableHistory(ValueError):  # noqa: N818 - nominal stored-read state
    """A strict stored-data read failed, not the observer or the whole UI.

    The reader runtime is known; the writer version is not inferred from a
    missing declaration. Older saved records can fail after a declaration is
    deleted just as newer records can fail in an older reader.
    """

    # Currentness and contention defer the existing read; they do not show
    # broken saved data.
    DEFERRED_READS = (CoordinationReadUnavailable, StaleRevision)

    def __init__(self, reader_runtime: str, reason: str):
        self.reader_runtime = reader_runtime
        self.reason = reason
        # Keep constructor arguments intact for the process pipe's pickle.
        super().__init__(reader_runtime, reason)

    @classmethod
    def capture(cls, error: Exception) -> UnreadableHistory:
        if isinstance(error, cls):
            return error
        reasons = []
        cause: BaseException | None = error
        while cause is not None:
            reasons.append(str(cause))
            cause = cause.__cause__
        return cls(sys.prefix, "\nCaused by: ".join(reasons))

    def __str__(self) -> str:
        return (f"Saved history unreadable: {self.reason}\n"
                f"Reader runtime: {self.reader_runtime}. Restart this process on "
                "the reviewed runtime; if it still fails, the saved records need repair.")


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
        """Return the read owner's named failure without reclassifying it."""
        return ReadRefused(self.request_id, error, traceback.format_exc())


class ReadRequest(AwaitedRead):
    """A read of Core's stores, served one at a time in arrival order."""

    def receive(self, service: ObservationService) -> None:
        service.pending[self.request_id] = self
        service.changed.set()

    @abstractmethod
    def read(self, comms: Comms) -> object: ...

    def answer(self, comms: Comms) -> ReadAnswered | ReadRefused | ReadBusy:
        try:
            self.require_root(comms)
            return ReadAnswered(self.request_id, self.read(comms))
        except CoordinationReadUnavailable:
            return ReadBusy(self.request_id)
        except (RelationViolationError, UnreadableHistory, StaleRevision) as error:
            return self.refused(error)


@dataclass(frozen=True)
class OwnerRead(AwaitedRead):
    """A read the live owner of thread incarnation ``owner`` answers over its socket.

    Served as it arrives, beside store reads; the owner's reply is decoded
    here. The owner's refusals and a changed owner identity reach the UI as
    ValueError, as the UI's owner actions report them.
    """

    owner: ThreadIncarnation

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
            return ReadAnswered(self.request_id, await self.ask(comms))
        except Exception as error:
            return self.refused(error)

    def connect(self, comms: Comms) -> RuntimeConnection:
        from agent_comms.runtime import OwnerReplyConnection, socket_path

        owner = comms.registry.require(self.owner.name)
        if owner.incarnation != self.owner:
            raise ValueError("The owner incarnation changed while preparing the request.")
        return OwnerReplyConnection(comms, owner.name, socket_path(comms.root, owner.pid))

    async def ask(self, comms: Comms) -> object:
        connection = await asyncio.to_thread(self.connect, comms)
        try:
            return await self.read(connection)
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


TranscriptResult = TypeVar("TranscriptResult")


@dataclass(frozen=True)
class TranscriptRequest(ReadRequest, Generic[TranscriptResult]):
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

    def read(self, comms: Comms) -> TranscriptResult:
        try:
            transcripts = comms.transcripts
            window = dict(before=self.before, after=self.after, through=self.through)
            if self.read_identity is None:
                read = transcripts.capture_page_read(
                    self.thread, historical_source=self.historical_source, **window
                )
            else:
                if (self.historical_source is not None
                        and self.historical_source != self.read_identity.historical_source):
                    raise StaleRevision("Published transcript belongs to another recorded source")
                read = transcripts.bind_page_read(self.thread, self.read_identity, **window)
            # The read checks its witness before and after preparing the page.
            return self.result(read)
        except StoredHistoryUnreadable as error:
            raise UnreadableHistory.capture(error) from error

    @abstractmethod
    def result(self, read: TranscriptRead) -> TranscriptResult: ...


@dataclass(frozen=True)
class ReadTranscript(TranscriptRequest[TranscriptSnapshotUpdate]):
    """Read a complete page, including its current draft."""

    def result(self, read: TranscriptRead) -> TranscriptSnapshotUpdate:
        return TranscriptSnapshotUpdate.from_read(read)


@dataclass(frozen=True)
class ReadTranscriptUpdate(TranscriptRequest[TranscriptSnapshotUpdate | TranscriptDraftUpdate]):
    """Refresh a published page, reading only its draft while its contents agree."""

    previous: TranscriptReadIdentity | None = None

    def result(self, read: TranscriptRead) -> TranscriptSnapshotUpdate | TranscriptDraftUpdate:
        previous = self.previous
        if (previous is not None and previous.records_cut == read.identity.records_cut
                and previous.same_content(read.identity)):
            return TranscriptDraftUpdate(read.identity, read.current_draft())
        return TranscriptSnapshotUpdate.from_read(read)


@dataclass(frozen=True)
class RefreshTranscript(ReadRequest):
    """A published page's witness: None while its content is current, else the current page."""

    identity: TranscriptReadIdentity

    def read(self, comms: Comms) -> TranscriptSnapshotUpdate | None:
        identity = self.identity
        read = comms.transcripts.bind_page_read(
            identity.requested_name,
            identity,
            before=identity.before,
            after=identity.after,
            through=identity.through,
        )
        if read.content_current():
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

    def observation(self) -> SidebarReading:
        return SidebarReading(self)

    def _answer(
        self, revision: WireRevision, read: Callable[..., CoordinationSnapshot], subject: str
    ) -> SidebarObserved | SidebarUnreadable | None:
        from agent_comms.ui_model.sidebar import SidebarModel

        try:
            snapshot = read(
                subject, show_stopped=self.show_stopped, show_archived=self.show_archived)
            return SidebarObserved(revision, self, snapshot, SidebarModel.derive(snapshot))
        except UnreadableHistory.DEFERRED_READS:
            return None  # No new snapshot; keep the original interest for the next observation.
        except StoredHistoryUnreadable as error:
            return SidebarUnreadable(revision, self, UnreadableHistory.capture(error))

    def roster(
        self, comms: Comms, revision: WireRevision
    ) -> SidebarObserved | SidebarUnreadable | None:
        viewer = comms.messaging.user_identity(self.worktree).name
        return self._answer(revision, comms.views.presentation.roster, viewer)

    def answer(
        self, comms: Comms, revision: WireRevision
    ) -> SidebarObserved | SidebarUnreadable | None:
        return self._answer(revision, comms.views.viewer_snapshot, self.worktree)


class SidebarReadStage(ABC):
    @abstractmethod
    def read(
        self, request: ObserveSidebar, comms: Comms, revision: WireRevision
    ) -> SidebarObserved | SidebarUnreadable | None: ...

    def next(self) -> SidebarReadStage:
        return self


class RosterSidebarRead(SidebarReadStage):
    def read(
        self, request: ObserveSidebar, comms: Comms, revision: WireRevision
    ) -> SidebarObserved | SidebarUnreadable | None:
        return request.roster(comms, revision)

    def next(self) -> SidebarReadStage:
        return CompleteSidebarRead()


class CompleteSidebarRead(SidebarReadStage):
    def read(
        self, request: ObserveSidebar, comms: Comms, revision: WireRevision
    ) -> SidebarObserved | SidebarUnreadable | None:
        return request.answer(comms, revision)


class SidebarReading(Observation[ObserveSidebar]):
    def __init__(self, request: ObserveSidebar):
        super().__init__(request)
        self.stage: SidebarReadStage = RosterSidebarRead()
        self.sent: SidebarSent | None = None

    def read(self, comms: Comms, revision: WireRevision) -> Iterator[Observed | None]:
        while True:
            key = SidebarSent(revision.expiry_tick // EXPIRY_PERIOD, type(self.stage), revision)
            changed = self.sent is None or revision.status_changed_since(self.sent.revision)
            if not changed and key == self.sent:
                return
            result = self.stage.read(self.request, comms, revision)
            if result is None:
                return
            self.sent = key
            result.finish_read(self)
            yield result


@dataclass(frozen=True)
class SidebarSent:
    expiry_period: int
    stage: type[SidebarReadStage]
    revision: WireRevision = field(compare=False)


@dataclass(frozen=True)
class StopSidebar(ObservationRequest):
    def receive(self, service: ObservationService) -> None:
        service.observations.pop(SidebarReading, None)


@dataclass(frozen=True)
class StopObserving(ObservationRequest):
    """Views are retiring; finish their outstanding reads without observing more state."""

    def receive(self, service: ObservationService) -> None:
        service.observations.clear()


@dataclass(frozen=True)
class ObserveThreads(InterestRequest):
    names: frozenset[str]

    def observation(self) -> ThreadReading:
        return ThreadReading(self)


@dataclass(frozen=True)
class ObserveViews(InterestRequest):
    """The thread incarnations and channel views open in the UI, to learn when one is retired."""

    threads: frozenset
    channels: frozenset[str]

    def observation(self) -> ViewReading:
        return ViewReading(self)


class ThreadReading(Observation[ObserveThreads]):
    def __init__(self, request: ObserveThreads):
        super().__init__(request)
        self.revision: WireRevision | None = None
        self.retry = False

    def read(self, comms: Comms, revision: WireRevision) -> Iterator[Observed | None]:
        if self.revision is not None and not revision.status_changed_since(self.revision) and not self.retry:
            return
        presentations, unavailable, busy = {}, {}, set()
        for name in self.request.names:
            try:
                presentations[name] = comms.views.thread_presentation(name)
            except UnreadableHistory.DEFERRED_READS:
                busy.add(name)
            except StoredHistoryUnreadable as error:
                unavailable[name] = UnreadableHistory.capture(error)
            yield None
        self.revision, self.retry = revision, bool(busy)
        yield ThreadsObserved(revision, presentations, unavailable, frozenset(busy))


class ViewReading(Observation[ObserveViews]):
    def __init__(self, request: ObserveViews):
        super().__init__(request)
        self.revision: WireRevision | None = None

    def read(self, comms: Comms, revision: WireRevision) -> Iterator[Observed | None]:
        if self.revision is not None and not revision.registrations_changed_since(self.revision):
            return
        retired = comms.views.retired_views(self.request.threads, self.request.channels)
        self.revision = revision
        if retired.threads or retired.channels:
            yield ViewsRetired(revision, retired)


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
class ReadBusy(ServiceResult):
    """Source busy: the original read remains pending, not a failed input."""

    request_id: int

    @property
    def title(self) -> str:
        return "History loading"

    def received(self, process: ObservationProcess, observed: list[Observed]) -> None:
        # Keep the same waiter. The service owns waiting for the source and
        # answers this request when its next observation can acquire a read.
        pass


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
class SidebarResult(Observed):
    request: ObserveSidebar

    @abstractmethod
    def finish_read(self, reading: SidebarReading) -> None: ...


@dataclass(frozen=True)
class SidebarObserved(SidebarResult):
    snapshot: CoordinationSnapshot
    derived: tuple[dict[str, ChannelRowModel], dict[str, ThreadRowModel]]

    def finish_read(self, reading: SidebarReading) -> None:
        reading.stage = reading.stage.next()


@dataclass(frozen=True)
class SidebarUnreadable(SidebarResult):
    """The requested sidebar could not be read; no stale snapshot is offered."""

    error: UnreadableHistory

    def finish_read(self, reading: SidebarReading) -> None:
        """Keep this stage until its source revision changes."""


@dataclass(frozen=True)
class ThreadsObserved(Observed):
    """Presentations of the observed threads; a busy store leaves some unread this time."""

    presentations: Mapping[str, ThreadPresentation | None]
    unavailable: Mapping[str, UnreadableHistory]
    busy: frozenset[str]


@dataclass(frozen=True)
class ViewsRetired(Observed):
    """Open views whose thread was deleted or replaced, or whose channel is gone or archived."""

    retired: RetiredViews


@dataclass(frozen=True)
class ObservationFailed(ServiceResult):
    """The service failed and is exiting; ``error`` is its traceback."""

    error: str

    def received(self, process: ObservationProcess, observed: list[Observed]) -> None:
        raise process.ended(self.error)


class ObserverDied(CoordinationError):
    """The observation service's process ended while the UI was using it.

    The UI does not run on what it last observed: it reports this and exits.
    """

    def __init__(self, pid: int, exit_status: int | None, error: str | None):
        self.pid, self.exit_status, self.error = pid, exit_status, error
        if exit_status is None:
            how = "closed its pipe and has not exited"
        elif exit_status < 0:
            how = f"was killed by signal {-exit_status} ({signal.strsignal(-exit_status)})"
        else:
            how = f"exited with status {exit_status}"
        super().__init__(f"The comms observer (pid {pid}) {how}" + (f":\n{error}" if error else ""))


class ObservationService:
    """The child process side: one Comms service, the UI's interest, and what was sent."""

    def __init__(self, comms, connection: Connection):
        self.comms = comms
        self.connection = connection
        self.observations: dict[type[Observation], Observation] = {}
        self.changed = asyncio.Event()
        self.revision: WireRevision | None = None
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
        from agent_comms.wire_watch import ModifiedWatchSource, WireWatch

        source = ModifiedWatchSource(self.comms.root, self.store_names())
        async for _ in WireWatch.observations(source):
            self.observe_due = True
            self.changed.set()

    async def answer_pending(self, attempted: set[int]) -> None:
        """Finish each waiting store read once, accepting cancellations between reads."""
        await asyncio.sleep(0)
        self.receive()
        for request_id, request in tuple(self.pending.items()):
            if self.closed:
                return
            if request_id in attempted or self.pending.get(request_id) is not request:
                continue
            attempted.add(request_id)
            answer = request.answer(self.comms)
            self.connection.send(answer)
            if not isinstance(answer, ReadBusy):
                self.pending.pop(request_id)
            await asyncio.sleep(0)

    async def observe(self, attempted: set[int]) -> None:
        revision = self.comms.views.revision()
        if self.revision is None or revision.stores_changed_since(self.revision):
            self.connection.send(RevisionObserved(revision))
        self.revision = revision
        for kind, observation in tuple(self.observations.items()):
            # Fresh foreground requests take precedence at every read boundary.
            await self.answer_pending(attempted)
            if self.closed:
                return
            if self.observations.get(kind) is not observation:
                continue
            for result in observation.read(self.comms, revision):
                if result is not None:
                    self.connection.send(result)
                await self.answer_pending(attempted)
                if self.closed or self.observations.get(kind) is not observation:
                    break

    async def expire(self) -> None:
        """Time-based sidebar state belongs to the sidebar observer, not file watching."""
        while not self.closed:
            now = time.time()
            await asyncio.sleep(EXPIRY_PERIOD - now % EXPIRY_PERIOD)
            self.observe_due = True
            self.changed.set()

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        loop.add_reader(self.connection.fileno(), self.receive)
        watching = asyncio.create_task(self.watch())
        expiring = asyncio.create_task(self.expire())
        try:
            while not self.closed:
                await self.changed.wait()
                self.changed.clear()
                attempted: set[int] = set()
                await self.answer_pending(attempted)
                if self.observe_due and not self.closed:
                    self.observe_due = False
                    await self.observe(attempted)
        finally:
            watching.cancel()
            expiring.cancel()
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

    @classmethod
    @contextmanager
    def awaited(cls, root: Path):
        """Own pipe readiness and retirement for an attached asynchronous reader."""
        process = cls(root)
        loop = asyncio.get_running_loop()
        descriptor = process.fileno()
        loop.add_reader(descriptor, process.results)
        try:
            yield process
        finally:
            loop.remove_reader(descriptor)
            process.close()

    def __init__(self, root: Path):
        context = multiprocessing.get_context("spawn")
        self.connection, child = context.Pipe()
        self.process = context.Process(target=serve, args=(str(root), child), daemon=True,
                                       name="agent-comms-observation")
        self.process.start()
        child.close()
        self.request_ids = itertools.count(1)
        self.reads: dict[int, asyncio.Future] = {}
        # Set by ``results`` when it reads that the service ended.
        self.died: ObserverDied | None = None

    def fileno(self) -> int:
        return self.connection.fileno()

    def request(self, request: ObservationRequest) -> None:
        """Tell the service what the UI shows now; a service that ended is not told.

        Its end is reported by ``results`` alone. A pipe that breaks here
        belongs to a service ``results`` has not read the end of yet: the
        same closed pipe is readable there.
        """
        if self.died is None:
            try:
                self.connection.send(request)
            except BrokenPipeError:
                pass

    def ended(self, error: str | None) -> ObserverDied:
        """The service ended (``error``: the traceback it sent): fail its awaited reads and say how it exited."""
        self.process.join(timeout=2)
        self.died = ObserverDied(self.process.pid, self.process.exitcode, error)
        self.abandon_reads(self.died)
        return self.died

    async def read(self, request: AwaitedRead):
        """Send ``request`` and await its answer, delivered by ``results``.

        Cancelling the caller cancels the read; an unserved read is dropped.
        """
        if self.died is not None:
            raise self.died
        request = replace(request, request_id=next(self.request_ids))
        waiter = asyncio.get_running_loop().create_future()
        self.reads[request.request_id] = waiter
        try:
            self.request(request)
            return await waiter
        except asyncio.CancelledError:
            if self.reads.pop(request.request_id, None) is not None and not self.connection.closed:
                self.request(CancelRead(request.request_id))
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

        The service's end (its failure, or its pipe closing) is raised here
        as ``ObserverDied``; the UI stops reading this pipe then.
        """
        observed: list[Observed] = []
        while self.connection.poll():
            try:
                result = self.connection.recv()
            except (EOFError, ConnectionResetError):
                raise self.ended(None) from None
            result.received(self, observed)
        return observed

    def close(self) -> None:
        self.connection.close()
        self.process.join(timeout=2)
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(timeout=2)
