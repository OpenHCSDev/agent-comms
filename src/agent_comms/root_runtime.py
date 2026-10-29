"""A bus root belongs to exactly one published runtime."""

from __future__ import annotations

import socket
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, ExitStack
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Annotated, TextIO

from .bus import Bus
from .child_process import NoProcess, ObservedProcess, OutlivesLauncherLifetime, ProcessIdentity, ProcessState, SpawnedProcess
from agent_comms.nominal.declared_family import DeclaredFamily
from .errors import RelationViolationError
from agent_comms.nominal.field_codec import PathText, WireOmitDefault
from .launch_runtime import LaunchRuntime
from .locked_store import DocumentChange, LockedStore, RegistryFile, RollbackRetained
from .maintenance_barrier import MaintenanceBarrier
from .owner_connection import socket_path
from .private_path import FileIdentity, PrivateSocketRole
from .recorded_bus import RecordedBus, RecordedBusIdentity
from .store_files import DependentStateTier, SingleFileStore


class ForeignRuntimeError(RelationViolationError):
    """The caller is neither this root's owner nor its maintenance target."""


class Attachment(ABC):
    @abstractmethod
    def proceed[Result](
        self, attach: Callable[[], Result], succeed: Callable[[LaunchRuntime], Result],
    ) -> Result: ...

    @abstractmethod
    def require(self) -> None: ...


class Attached(Attachment):
    def proceed[Result](
        self, attach: Callable[[], Result], succeed: Callable[[LaunchRuntime], Result],
    ) -> Result:
        return attach()

    def require(self) -> None:
        return


@dataclass(frozen=True)
class Succession(Attachment):
    owner: LaunchRuntime
    root: Path
    caller: LaunchRuntime

    def proceed[Result](
        self, attach: Callable[[], Result], succeed: Callable[[LaunchRuntime], Result],
    ) -> Result:
        return succeed(self.owner)

    def require(self) -> None:
        raise ForeignRuntimeError(
            f"Root {self.root} belongs to runtime {self.owner.prefix}; "
            f"foreign runtime {self.caller.prefix} cannot attach"
        )


class RootHost(DeclaredFamily, affix="RootHost"):
    def observed(self, root: Path) -> ProcessState:
        return NoProcess()

    def retire(self, root: Path, ready: Callable[[ProcessIdentity], bool]) -> bool:
        return True


@dataclass(frozen=True)
class NoRootHost(RootHost):
    pass


@dataclass(frozen=True)
class RecordedRootHost(RootHost):
    process: ProcessIdentity
    bus: RecordedBus

    def retire(self, root: Path, ready: Callable[[ProcessIdentity], bool]) -> bool:
        observed = self.observed(root)
        if observed.alive():
            process = observed.require()
            if not ready(process):
                return False
            ObservedProcess(process).stop_sync()
        return True

    def observed(self, root: Path) -> ProcessState:
        # A copied store cannot borrow the source root's live process.
        if not self.bus.matches(RecordedBusIdentity.capture(Bus.path_in(root))):
            return NoProcess()
        return self.process


@dataclass(frozen=True)
class RootRuntime:
    prefix: Annotated[Path, PathText]
    host: Annotated[RootHost, WireOmitDefault()] = NoRootHost()

    def runtime(self) -> LaunchRuntime:
        return LaunchRuntime.at_prefix(self.prefix)

    def available_host(self, root: Path, runtime: LaunchRuntime) -> ProcessState:
        if self.prefix != runtime.prefix:
            raise ForeignRuntimeError("A foreign runtime cannot launch this root's host")
        return self.host.observed(root)

    @classmethod
    def attachment(cls, root: Path, caller: LaunchRuntime) -> Attachment:
        store = RootRuntimeStore.at(root)
        owner = store.owner(caller)
        if owner.prefix == caller.prefix:
            return Attached()
        if MaintenanceBarrier(RegistryFile.path_in(root)).read().allows_runtime(caller):
            return Attached()
        return Succession(owner.runtime(), root=root, caller=caller)

    @classmethod
    def require_attachable(cls, root: Path, caller: LaunchRuntime) -> None:
        cls.attachment(root, caller).require()

    @classmethod
    def publish(cls, root: Path, runtime: LaunchRuntime) -> None:
        RootRuntimeStore.at(root).publish_runtime(runtime)


@dataclass(frozen=True)
class RootRuntimeStore(SingleFileStore, LockedStore[RootRuntime], RollbackRetained, DependentStateTier):
    filename = ".root-runtime.json"
    record_type = RootRuntime

    def __post_init__(self) -> None:
        # This is the first file on a new root, before publisher preparation.
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    def empty(self) -> RootRuntime:
        raise FileNotFoundError(self.path)

    def retire_host(self, ready: Callable[[ProcessIdentity], bool]) -> None:
        def retire(data: RootRuntime) -> DocumentChange[RootRuntime, None]:
            if data.host.retire(self.path.parent, ready):
                return DocumentChange(replace(data, host=NoRootHost()), None)
            return DocumentChange(data, None)
        self.update(retire)

    def acquire_host(
        self, runtime: LaunchRuntime, command: tuple[str, ...], *,
        environment: Mapping[str, str], cwd: str,
        output: Callable[[], AbstractContextManager[TextIO]],
        room_for_start: Callable[[], AbstractContextManager[None]],
    ) -> ProcessIdentity:
        """Publish the root's one host while its existing exec gate is still closed."""
        observed = self.read().available_host(self.path.parent, runtime)
        if observed.alive():
            return observed.require("Recorded worker host has no process")
        # Wait for memory without holding either writer.
        # Re-read under the writer before launch so concurrent starters share one host.
        # The launch remains gated until update has published the winning host.
        with room_for_start(), ExitStack() as resources:
            def launch(current: RootRuntime) -> DocumentChange[RootRuntime, ProcessIdentity]:
                observed = current.available_host(self.path.parent, runtime)
                if observed.alive():
                    return DocumentChange(current, observed.require("Recorded worker host has no process"))
                capture = resources.enter_context(output())
                listener = resources.enter_context(socket.socket(socket.AF_UNIX))
                child = resources.enter_context(SpawnedProcess.launching(
                    (*command, "--listener-fd", str(listener.fileno())),
                    env=environment, cwd=cwd, output=capture, runtime=runtime,
                    pass_fds=(listener.fileno(),), lifetime=OutlivesLauncherLifetime,
                ))
                path = socket_path(self.path.parent, child.identity.pid)
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with PrivateSocketRole.address(path) as address:
                    listener.bind(str(address))
                listener.listen()
                bus = RecordedBus(FileIdentity.from_stat(Bus.path_in(self.path.parent).stat()))
                return DocumentChange(replace(current, host=RecordedRootHost(child.identity, bus)), child.identity)
            return self.update(launch)

    def publish_runtime(self, runtime: LaunchRuntime) -> None:
        self.owner(runtime)
        def publish(current: RootRuntime) -> DocumentChange[RootRuntime, RootRuntime]:
            if current.prefix == runtime.prefix:
                return DocumentChange.to(current)
            if current.host.observed(self.path.parent).alive():
                raise ForeignRuntimeError("Retire the root's worker host before changing runtime")
            return DocumentChange.to(RootRuntime(runtime.prefix))
        self.update(publish)

    def owner(self, creator: LaunchRuntime) -> RootRuntime:
        try:
            return self.read()
        except FileNotFoundError:
            return self.adopt(creator)

    def adopt(self, runtime: LaunchRuntime) -> RootRuntime:
        """A root without a runtime record belongs to ``runtime``; a recorded owner stays.

        Adoption is where a directory becomes a root, so its bus is created here, empty:
        from then on an absent bus file means a deleted bus, never one not yet written."""
        def create() -> RootRuntime:
            Bus.install(Bus.path_in(self.path.parent))
            return RootRuntime(runtime.prefix)
        return self.initialize(create)
