"""Runtime observations and runtime-owned child lifetime, in one store owner."""

from __future__ import annotations

import os
import time
from abc import abstractmethod
from collections.abc import Iterable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, ClassVar

from .child_process import (ChildRegistration, ChildRetirementFailed, GroupLeader,
                            IdentityMismatchError, RecordedRuntimeGroup, Platform, ProcessIdentity)
from .context_reading import ContextReading, UnmeasuredContext
from .declared_family import DeclaredFamily
from .errors import RelationViolationError
from .field_codec import FieldCodec, PathText
from .launch_runtime import CallingThreadUnavailable, LaunchRuntime, RestartEnvironment, ThreadLaunch
from .locked_store import LockedStore, RootFile
from .pi_counts import PiCount, UnreportedCount
from .thread_owned_state import ThreadOwnedState

if TYPE_CHECKING:
    from .agent_events import RuntimeInfoObservation
    from .threads import Thread


class RuntimeInfo(DeclaredFamily, affix="RuntimeInfo"):
    @abstractmethod
    def context_reading(self) -> ContextReading: ...

    @abstractmethod
    def model_or(self, default: str) -> str: ...

    context_used: PiCount
    context_size: PiCount


@dataclass(frozen=True, slots=True)
class UnobservedRuntimeInfo(RuntimeInfo):
    def context_reading(self) -> ContextReading:
        return UnmeasuredContext()

    context_used = context_size = UnreportedCount()

    def model_or(self, default: str) -> str:
        return default


@dataclass(frozen=True, slots=True)
class AgentRuntimeInfo(RuntimeInfo):
    thread: str
    model: str | None = None
    context_used: PiCount = field(default_factory=UnreportedCount)
    context_size: PiCount = field(default_factory=UnreportedCount)
    timestamp: float = field(metadata={"wire_name": "ts", "wire_required": True}, kw_only=True)

    def __post_init__(self) -> None:
        FieldCodec.decode(float, self.timestamp)
        if not self.thread:
            raise RelationViolationError("Runtime-info thread cannot be empty.")

    def context_reading(self) -> ContextReading:
        return self.context_size.reading(self.context_used)

    def model_or(self, default: str) -> str:
        return self.model or default


class ChildInitiator(DeclaredFamily, affix="ChildInitiator"):
    @classmethod
    def capture(cls, environment: dict[str, str]) -> ChildInitiator:
        try:
            return ThreadChildInitiator(ThreadLaunch.current(environment).name)
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
    """An application launch without a thread must not invent a thread identity."""
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
            # POSIX cannot reuse a leader's PID while its original process
            # group exists. Windows resolves the birth-bound named job instead.
            return ()

    def terminate(self) -> None:
        if self.members():
            Platform.current().terminate_group(self.identity)

    def retire(self, deadline: float) -> None:
        if not self.members():
            return
        try:
            RecordedRuntimeGroup(self.identity).stop_sync(deadline=deadline)
        except (OSError, RuntimeError) as error:
            survivors = self.members()
            if not survivors:
                return
            pids = ", ".join(str(process.pid) for process in survivors)
            raise ChildRetirementFailed(
                f"Runtime {self.runtime_prefix}: surviving pid(s) {pids}, "
                f"{self.initiator.describe()} (leader {self.identity.pid})"
            ) from error


@dataclass(frozen=True)
class RuntimeChildren(DeclaredFamily, affix="RuntimeChildren"):
    records: tuple[RuntimeChild, ...] = ()

    @abstractmethod
    def require_open(self) -> None: ...

    def add(self, child: RuntimeChild) -> RuntimeChildren:
        self.require_open()
        return replace(self, records=(*self.records, child))

    def finished(self, identity: ProcessIdentity) -> RuntimeChildren:
        return replace(self, records=tuple(child for child in self.records if child.identity != identity))


@dataclass(frozen=True)
class AcceptingRuntimeChildren(RuntimeChildren):
    def require_open(self) -> None:
        pass


@dataclass(frozen=True)
class RetiredRuntimeChildren(RuntimeChildren):
    def require_open(self) -> None:
        raise ChildRetirementFailed("Cannot launch a child from a retired runtime")


@dataclass(frozen=True)
class RuntimeInfoDocument:
    threads: dict[str, AgentRuntimeInfo] = field(default_factory=dict)
    children: RuntimeChildren = field(default_factory=AcceptingRuntimeChildren)


@dataclass(frozen=True)
class RuntimeChildReservation:
    store: RuntimeInfoStore
    document: RuntimeInfoDocument
    initiator: ChildInitiator

    def record(self, identity: ProcessIdentity) -> None:
        child = RuntimeChild(GroupLeader(identity.pid, identity.start_time), self.initiator, self.store.path.parent)
        self.store._publish_unlocked(replace(self.document, children=self.document.children.add(child)))


@dataclass(frozen=True, slots=True)
class RuntimeInfoStore(RootFile, ThreadOwnedState, ChildRegistration, LockedStore[RuntimeInfoDocument], lock_rank=6):
    """Bus-root observations; prefix-root child ownership uses the same owner.

    Child records live at the selected prefix so retirement includes every bus,
    including private roots, without a second registry of roots or processes.
    """
    filename: ClassVar[str] = "runtime_info.json"
    json_indent = 2

    @property
    def record_type(self) -> type[RuntimeInfoDocument]:
        return RuntimeInfoDocument

    def empty(self) -> RuntimeInfoDocument:
        return RuntimeInfoDocument()

    @classmethod
    @contextmanager
    def launching(cls, environment: dict[str, str] | None):
        source = os.environ if environment is None else environment
        configuration = RestartEnvironment.inherit(source)
        prefix = (Path(configuration.virtual_env) if configuration.virtual_env
                  else LaunchRuntime.running().interpreter.parent.parent).resolve()
        store = cls.at(prefix)
        with store.locked():
            document = store._read_unlocked()
            document.children.require_open()
            yield RuntimeChildReservation(store, document, ChildInitiator.capture(dict(source)))

    def finished(self, identity: ProcessIdentity) -> None:
        self.update(lambda document: replace(document, children=document.children.finished(identity)))

    @classmethod
    def retire(cls, runtime_prefix: Path) -> None:
        store = cls.at(runtime_prefix.resolve())
        document = store.update(lambda current: replace(current, children=RetiredRuntimeChildren(current.children.records)))
        deadline = time.monotonic() + 10.0
        for child in document.children.records:
            child.terminate()
        failures = []
        for child in document.children.records:
            try:
                child.retire(deadline)
            except ChildRetirementFailed as error:
                failures.append(str(error))
        if failures:
            raise ChildRetirementFailed("\n".join(failures))
        store.update(lambda current: replace(current, children=RetiredRuntimeChildren()))

    def for_thread(self, thread: str) -> RuntimeInfo:
        return self.for_threads((thread,))[thread]

    def for_threads(self, names: Iterable[str]) -> dict[str, RuntimeInfo]:
        values = self.read().threads
        return {name: values.get(name, UnobservedRuntimeInfo()) for name in names}

    def observe(self, thread: str, timestamp: float, observation: RuntimeInfoObservation) -> None:
        def apply(document: RuntimeInfoDocument) -> RuntimeInfoDocument:
            current = document.threads.get(thread, AgentRuntimeInfo(thread=thread, timestamp=timestamp))
            observed = observation.project_runtime_info(replace(current, timestamp=timestamp))
            return replace(document, threads={**document.threads, thread: observed})
        self.update(apply)

    def remove_threads(self, threads: Sequence[Thread]) -> None:
        names = {thread.name for thread in threads}
        self.update(lambda document: replace(document, threads={
            name: value for name, value in document.threads.items() if name not in names
        }))

    def rename_thread(self, old_name: str, new_name: str) -> None:
        def rename(document: RuntimeInfoDocument) -> RuntimeInfoDocument:
            if old_name not in document.threads or old_name == new_name:
                return document
            return replace(document, threads={
                **{name: value for name, value in document.threads.items() if name != old_name},
                new_name: replace(document.threads[old_name], thread=new_name),
            })
        self.update(rename)
