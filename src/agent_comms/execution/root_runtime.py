"""A bus root belongs to exactly one published runtime."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent_comms.execution.launch_runtime import LaunchRuntime
from agent_comms.execution.root_runtime_read import (
    Attached,
    Attachment,
    HostRetirement,
    NoRootHost,
    RecordedRootHost,
    RootHost,
    RootRuntimeData,
    RootRuntimeReader,
    Succession,
    UnadoptedAttachment,
)
from agent_comms.messaging.bus import Bus
from agent_comms.storage.locked_store import BusStoreFile, DocumentChange, LockedStore


@dataclass(frozen=True)
class RootRuntime(RootRuntimeData):
    @classmethod
    def attachment(cls, root: Path, caller: LaunchRuntime) -> Attachment:
        owner = RootRuntimeStore.at(root).owner(caller)
        return cls.attachment_for_owner(owner, root, caller)

    @classmethod
    def require_attachable(cls, root: Path, caller: LaunchRuntime) -> None:
        cls.attachment(root, caller).require()

    @classmethod
    def publish(cls, root: Path, runtime: LaunchRuntime) -> None:
        RootRuntimeStore.at(root).publish_runtime(runtime)


@dataclass(frozen=True)
class RootRuntimeStore(RootRuntimeReader, LockedStore[RootRuntime]):
    record_type = RootRuntime

    def __post_init__(self) -> None:
        # This is the first file on a new root, before publisher preparation.
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    def empty(self) -> RootRuntime:
        raise FileNotFoundError(self.path)

    def publish_runtime(self, runtime: LaunchRuntime) -> None:
        self.owner(runtime)

        def publish(current: RootRuntime) -> DocumentChange[RootRuntime, RootRuntime]:
            if current.prefix == runtime.prefix:
                return DocumentChange.to(current)
            current.host.require_retired(self.path.parent)
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
            Bus.install(BusStoreFile.path_in(self.path.parent))
            return RootRuntime(runtime.prefix)

        return self.initialize(create)
