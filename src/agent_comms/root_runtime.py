"""A bus root belongs to exactly one published runtime."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from .bus import Bus
from .errors import RelationViolationError
from .field_codec import PathText
from .launch_runtime import LaunchRuntime
from .locked_store import DurableFile, LockedStore, RegistryFile
from .maintenance_barrier import MaintenanceBarrier
from .store_files import DependentStateTier


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


@dataclass(frozen=True)
class RootRuntime:
    prefix: Annotated[Path, PathText]

    def runtime(self) -> LaunchRuntime:
        return LaunchRuntime.at_prefix(self.prefix)

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
        RootRuntimeStore.at(root).replace(cls(runtime.prefix))


@dataclass(frozen=True)
class RootRuntimeStore(LockedStore[RootRuntime], DurableFile, DependentStateTier):
    filename = ".root-runtime.json"
    record_type = RootRuntime

    def __post_init__(self) -> None:
        # This is the first file on a new root, before publisher preparation.
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    def empty(self) -> RootRuntime:
        raise FileNotFoundError(self.path)

    def owner(self, creator: LaunchRuntime) -> RootRuntime:
        try:
            return self.read()
        except FileNotFoundError:
            return self.adopt(creator)

    def adopt(self, runtime: LaunchRuntime) -> RootRuntime:
        """A root without a runtime record belongs to ``runtime``; a recorded owner stays.

        Adoption is where a directory becomes a root, so its bus is created here, empty:
        from then on an absent bus file means a deleted bus, never one not yet written."""
        with self.locked():
            if not self.path.exists():
                Bus.install(Bus.path_in(self.path.parent))
                self._publish_unlocked(RootRuntime(runtime.prefix))
            return self._read_unlocked()
