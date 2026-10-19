"""Children started by one runtime prefix; unrelated to bus observations."""

from __future__ import annotations

import os
import time
from abc import abstractmethod
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Annotated, ClassVar

from .child_process import (
    ChildRegistration,
    ChildRetirementFailed,
    GroupLeader,
    IdentityMismatchError,
    Platform,
    ProcessIdentity,
    RecordedRuntimeGroup,
)
from .declared_family import DeclaredFamily
from .field_codec import PathText
from .launch_runtime import CallingThreadUnavailable, LaunchRuntime, ThreadLaunch
from .locked_store import LockedStore, RootFile


class ChildInitiator(DeclaredFamily, affix="ChildInitiator"):
    @classmethod
    def capture(cls) -> ChildInitiator:
        try:
            return ThreadChildInitiator(ThreadLaunch.current().name)
        except CallingThreadUnavailable:
            return ApplicationChildInitiator(ProcessIdentity.capture(os.getpid()))

    @abstractmethod
    def describe(self) -> str: ...


@dataclass(frozen=True)
class ThreadChildInitiator(ChildInitiator):
    thread: str

    def describe(self) -> str:
        return f"thread {self.thread}"


@dataclass(frozen=True)
class ApplicationChildInitiator(ChildInitiator):
    """A non-thread application must not invent a thread identity."""

    process: ProcessIdentity

    def describe(self) -> str:
        return f"application pid {self.process.pid}"


@dataclass(frozen=True)
class RuntimeChild:
    identity: GroupLeader
    initiator: ChildInitiator
    runtime_prefix: Annotated[Path, PathText]

    def members(self) -> tuple[ProcessIdentity, ...]:
        try:
            return Platform.current().group_members(self.identity)
        except IdentityMismatchError:
            # POSIX cannot reuse a leader's PID while its old group exists.
            # Windows instead resolves the birth-bound named job.
            return ()

    @contextmanager
    def checked(self) -> Iterator[None]:
        try:
            yield
        except (OSError, RuntimeError) as error:
            raise ChildRetirementFailed(
                f"Runtime {self.runtime_prefix}, leader pid {self.identity.pid}, "
                f"{self.initiator.describe()}: {error}"
            ) from error

    def terminate(self) -> None:
        with self.checked():
            if self.members():
                Platform.current().terminate_group(self.identity)

    def retire(self, deadline: float) -> None:
        with self.checked():
            if self.members():
                RecordedRuntimeGroup(self.identity).stop_sync(deadline=deadline)


@dataclass(frozen=True)
class RuntimeChildren(DeclaredFamily, affix="RuntimeChildren"):
    records: tuple[RuntimeChild, ...] = ()

    @abstractmethod
    def require_open(self) -> None: ...

    def add(self, child: RuntimeChild) -> RuntimeChildren:
        self.require_open()
        return replace(self, records=(*self.records, child))

    def finished(self, identity: ProcessIdentity) -> RuntimeChildren:
        return replace(
            self, records=tuple(child for child in self.records if child.identity != identity)
        )


@dataclass(frozen=True)
class AcceptingRuntimeChildren(RuntimeChildren):
    def require_open(self) -> None:
        pass


@dataclass(frozen=True)
class RetiredRuntimeChildren(RuntimeChildren):
    def require_open(self) -> None:
        raise ChildRetirementFailed("Cannot launch a child from a retired runtime")


@dataclass(frozen=True)
class RuntimeChildReservation:
    store: RuntimeChildrenStore
    children: RuntimeChildren
    initiator: ChildInitiator

    @classmethod
    def capture(cls, store: RuntimeChildrenStore) -> RuntimeChildReservation:
        children = store._read_unlocked()
        children.require_open()
        return cls(store, children, ChildInitiator.capture())

    def record(self, identity: ProcessIdentity) -> None:
        child = RuntimeChild(
            GroupLeader(identity.pid, identity.start_time), self.initiator, self.store.path.parent
        )
        self.store._publish_unlocked(self.children.add(child))


@dataclass(frozen=True, slots=True)
class RuntimeChildrenStore(RootFile, ChildRegistration, LockedStore[RuntimeChildren]):
    """One file at the selected prefix covers children from every bus root."""

    filename: ClassVar[str] = "runtime_children.json"
    record_type = RuntimeChildren
    json_indent = 2

    def empty(self) -> RuntimeChildren:
        return AcceptingRuntimeChildren()

    @classmethod
    @contextmanager
    def launching(cls, runtime: LaunchRuntime) -> Iterator[RuntimeChildReservation]:
        store = cls.at(runtime.prefix)
        with store.locked():
            yield RuntimeChildReservation.capture(store)

    @classmethod
    @asynccontextmanager
    async def launching_async(
        cls, runtime: LaunchRuntime
    ) -> AsyncIterator[RuntimeChildReservation]:
        store = cls.at(runtime.prefix)
        async with store.locked_async():
            yield RuntimeChildReservation.capture(store)

    def finished(self, identity: ProcessIdentity) -> None:
        self.update(lambda children: children.finished(identity))

    @classmethod
    def retire(cls, runtime_prefix: Path) -> None:
        store = cls.at(runtime_prefix.resolve())
        children = store.update(lambda current: RetiredRuntimeChildren(current.records))
        deadline = time.monotonic() + 10.0
        failures = []
        for child in children.records:
            try:
                child.terminate()
            except ChildRetirementFailed as error:
                failures.append(str(error))
        for child in children.records:
            try:
                child.retire(deadline)
            except ChildRetirementFailed as error:
                failures.append(str(error))
        if failures:
            raise ChildRetirementFailed("\n".join(failures))
        store.update(lambda current: RetiredRuntimeChildren())
