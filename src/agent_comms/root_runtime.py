"""A bus root belongs to exactly one published runtime."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from .errors import RelationViolationError
from .field_codec import PathText
from .launch_runtime import LaunchRuntime
from .locked_store import DurableFile, LockedStore
from .maintenance_barrier import MaintenanceBarrier


class ForeignRuntimeError(RelationViolationError):
    """The caller is neither this root's owner nor its maintenance target."""


@dataclass(frozen=True)
class RootRuntime:
    prefix: Annotated[Path, PathText]

    @classmethod
    def require_attachable(cls, root: Path, caller: LaunchRuntime) -> None:
        store = RootRuntimeStore.at(root)
        owner = store.owner(caller)
        if owner.prefix == caller.prefix:
            return
        if MaintenanceBarrier(root / "registry.json").read().allows_runtime(caller):
            return
        raise ForeignRuntimeError(
            f"Root {root} belongs to runtime {owner.prefix}; "
            f"foreign runtime {caller.prefix} cannot attach"
        )

    @classmethod
    def publish(cls, root: Path, runtime: LaunchRuntime) -> None:
        RootRuntimeStore.at(root).replace(cls(runtime.prefix))


class RootRuntimeStore(LockedStore[RootRuntime], DurableFile):
    filename = ".root-runtime.json"
    record_type = RootRuntime

    def empty(self) -> RootRuntime:
        raise FileNotFoundError(self.path)

    def owner(self, creator: LaunchRuntime) -> RootRuntime:
        try:
            return self.read()
        except FileNotFoundError:
            with self.locked():
                if not self.path.exists():
                    self._publish_unlocked(RootRuntime(creator.prefix))
                return self._read_unlocked()
