"""Read-only root runtime ownership checks, usable without importing Bus."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from agent_comms.errors import RelationViolationError
from agent_comms.execution.child_process import (
    NoProcess,
    ObservedProcess,
    ProcessIdentity,
    ProcessState,
)
from agent_comms.execution.launch_runtime import LaunchRuntime
from agent_comms.messaging.recorded_bus import RecordedBus, RecordedBusIdentity
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import PathText, WireOmitDefault
from agent_comms.storage.locked_store import (
    BusStoreFile,
    FileStore,
    LockedStore,
    RegistryFile,
    RollbackRetained,
)
from agent_comms.storage.maintenance_barrier import MaintenanceBarrier
from agent_comms.storage.store_files import DependentStateTier, SingleFileStore


class Attachment(ABC):
    @abstractmethod
    def proceed[Result](
        self, attach: Callable[[], Result], succeed: Callable[[LaunchRuntime], Result],
    ) -> Result: ...

    def require(self) -> None:
        return

    def run_recorded[Result](
        self, attached: Callable[[], Result], unadopted: Callable[[], Result],
    ) -> Result:
        """Run a read-only fast path on an adopted root, fail on succession, or fall back to adoption."""
        del unadopted
        self.require()
        return attached()


class Attached(Attachment):
    def proceed[Result](
        self, attach: Callable[[], Result], succeed: Callable[[LaunchRuntime], Result],
    ) -> Result:
        return attach()


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
        raise RelationViolationError(
            f"Root {self.root} belongs to runtime {self.owner.prefix}; "
            f"foreign runtime {self.caller.prefix} cannot attach"
        )


class UnadoptedAttachment(Attachment):
    """No ``.root-runtime.json`` exists yet; callers that need a root must adopt it through Bus."""

    def proceed[Result](
        self, attach: Callable[[], Result], succeed: Callable[[LaunchRuntime], Result],
    ) -> Result:
        del succeed
        return attach()

    def run_recorded[Result](
        self, attached: Callable[[], Result], unadopted: Callable[[], Result],
    ) -> Result:
        del attached
        return unadopted()


class HostRetirement(FileStore, ABC):
    """Only a host lifecycle store can decide retirement against its current owners."""

    @abstractmethod
    def host_retired_once_ownerless(self) -> RootHost: ...


class RootHost(DeclaredFamily, affix="RootHost"):
    def observed(self, root: Path) -> ProcessState:
        return NoProcess()

    def retire(self, root: Path, ready: Callable[[ProcessIdentity], bool]) -> bool:
        return True

    def retired_through(self, store: HostRetirement) -> RootHost:
        """Nothing runs, so no owner's registry row can matter: an install's target asks after
        the outgoing runtime retired the host, before a carry lets the target read that registry."""
        return self

    def require_retired(self, root: Path) -> None:
        if self.observed(root).alive():
            raise RelationViolationError("Retire the root's worker host before changing runtime")


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

    def retired_through(self, store: HostRetirement) -> RootHost:
        if self.observed(store.path.parent).alive():
            return store.host_retired_once_ownerless()
        return super().retired_through(store)

    def observed(self, root: Path) -> ProcessState:
        # A copied store cannot borrow the source root's live process.
        if not self.bus.matches(RecordedBusIdentity.capture(BusStoreFile.path_in(root))):
            return NoProcess()
        return self.process


@dataclass(frozen=True)
class RootRuntimeData:
    prefix: Annotated[Path, PathText]
    host: Annotated[RootHost, WireOmitDefault()] = NoRootHost()

    def runtime(self) -> LaunchRuntime:
        return LaunchRuntime.at_prefix(self.prefix)

    def available_host(self, root: Path, runtime: LaunchRuntime) -> ProcessState:
        if self.prefix != runtime.prefix:
            raise RelationViolationError("A foreign runtime cannot launch this root's host")
        return self.host.observed(root)

    @classmethod
    def attachment_for_owner(
        cls, owner: RootRuntimeData, root: Path, caller: LaunchRuntime,
    ) -> Attachment:
        if owner.prefix == caller.prefix:
            return Attached()
        if MaintenanceBarrier(RegistryFile.path_in(root)).read().allows_runtime(caller):
            return Attached()
        return Succession(owner.runtime(), root=root, caller=caller)

    @classmethod
    def recorded_attachment(cls, root: Path, caller: LaunchRuntime) -> Attachment:
        try:
            owner = RootRuntimeReader.at(root).read()
        except FileNotFoundError:
            return UnadoptedAttachment()
        return cls.attachment_for_owner(owner, root, caller)


@dataclass(frozen=True)
class RootRuntimeReader(SingleFileStore, LockedStore[RootRuntimeData], RollbackRetained, DependentStateTier):
    filename = ".root-runtime.json"
    record_type = RootRuntimeData

    def empty(self) -> RootRuntimeData:
        raise FileNotFoundError(self.path)
