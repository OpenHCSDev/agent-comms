"""Core observation for the comms UI, in a process of its own.

A UI process asks what to observe (the sidebar, the threads its views show)
and receives finished results only when Core's stores actually change: the
service watches the store files that ``HistoryViews.revision_paths`` declares,
reads snapshots and presentations, derives the sidebar rows, and sends them
over a pipe. The UI thread no longer polls revisions, reads stores or derives
rows, and no longer competes with those reads for its interpreter lock.
"""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import traceback
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from agent_comms.coordination_errors import CoordinationReadUnavailable

if TYPE_CHECKING:
    from multiprocessing.connection import Connection

    from agent_comms.presentation import CoordinationSnapshot, WireRevision
    from agent_comms.thread_presentation import ThreadPresentation
    from agent_comms.ui_model.sidebar import ChannelRowModel, ThreadRowModel

# Activity older than this reads as idle; re-derive rows at least this often
# so staleness shows without a store change.
EXPIRY_PERIOD = 30


@dataclass(frozen=True)
class Interest:
    """What the UI currently shows and so wants observed."""

    sidebar: ObserveSidebar | None = None
    threads: frozenset[str] = frozenset()


class ObservationRequest(ABC):
    """A UI request; each declares how it changes what is observed."""

    @abstractmethod
    def apply(self, interest: Interest) -> Interest: ...


@dataclass(frozen=True)
class ObserveSidebar(ObservationRequest):
    worktree: str
    show_stopped: bool
    show_archived: bool

    def apply(self, interest: Interest) -> Interest:
        return replace(interest, sidebar=self)


@dataclass(frozen=True)
class StopSidebar(ObservationRequest):
    def apply(self, interest: Interest) -> Interest:
        return replace(interest, sidebar=None)


@dataclass(frozen=True)
class ObserveThreads(ObservationRequest):
    names: frozenset[str]

    def apply(self, interest: Interest) -> Interest:
        return replace(interest, threads=self.names)


@dataclass(frozen=True)
class Observed:
    """A result, as of one revision of Core's stores."""

    revision: WireRevision

    def accepted(self) -> Observed:
        return self


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
class ObservationFailed:
    """The service failed; the UI raises this, it does not run on stale data."""

    error: str

    def accepted(self) -> Observed:
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
        self.retry_threads = False
        self.closed = False

    def receive(self) -> None:
        while self.connection.poll():
            try:
                request = self.connection.recv()
            except EOFError:
                self.closed = True
                self.changed.set()
                return
            self.interest = request.apply(self.interest)
            self.changed.set()

    def store_names(self) -> frozenset[bytes]:
        def paths(value) -> Iterator[Path]:
            yield from value if isinstance(value, tuple) else (value,)

        return frozenset(os.fsencode(path.name) for value in self.comms.views.revision_paths().values()
                         for path in paths(value) if path.parent == self.comms.root)

    async def watch(self) -> None:
        from agent_comms.wire_watch import WireWatch

        async for _ in WireWatch.observations(self.comms.root, self.store_names(), modified=True):
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

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        loop.add_reader(self.connection.fileno(), self.receive)
        watching = asyncio.create_task(self.watch())
        try:
            while not self.closed:
                await self.changed.wait()
                self.changed.clear()
                if not self.closed:
                    self.observe()
        finally:
            watching.cancel()
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
    """The UI side: start the service for a root, send requests, drain results."""

    def __init__(self, root: Path):
        context = multiprocessing.get_context("spawn")
        self.connection, child = context.Pipe()
        self.process = context.Process(target=serve, args=(str(root), child), daemon=True,
                                       name="agent-comms-observation")
        self.process.start()
        child.close()

    def fileno(self) -> int:
        return self.connection.fileno()

    def request(self, request: ObservationRequest) -> None:
        self.connection.send(request)

    def results(self) -> list[Observed]:
        """Every result waiting on the pipe; an ObservationFailed is raised here."""
        results = []
        while self.connection.poll():
            try:
                result = self.connection.recv()
            except (EOFError, ConnectionResetError) as error:
                raise RuntimeError("The comms observation service exited") from error
            results.append(result.accepted())
        return results

    def close(self) -> None:
        self.connection.close()
        self.process.join(timeout=2)
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(timeout=2)
