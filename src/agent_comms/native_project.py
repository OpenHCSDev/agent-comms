"""Original project binding and its native socket lifetime, never environment data."""
from __future__ import annotations

from abc import abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from .cli_options import JsonFlagRecord
from .declared_family import DeclaredFamily
from .project_runtime import ProjectRuntimeRequest
from .runtime_socket import RuntimeSocket
from .thread_presentation import LiveThreadOwnerBinding

if TYPE_CHECKING:
    from .registry_document import RegistryEntry, RegistrySnapshot
    from .threads import Thread


@dataclass(frozen=True)
class NativeProject(JsonFlagRecord, DeclaredFamily, affix="NativeProject"):
    value_flag: ClassVar[str] = "--native-project"

    @classmethod
    def native_extension(cls, package: Path) -> Path:
        """Project synchronization is part of the native binding, not user discovery."""
        return package / "agent-comms-extensions/global-project-sync/index.mjs"

    @abstractmethod
    def bound_arguments(self): ...

    def require_unmanaged(self) -> UnmanagedNativeProject:
        raise ValueError("Ownerless native launch cannot claim a managed project binding")

    def require_managed(self) -> ManagedNativeProject:
        raise ValueError("Managed native launch requires its original project binding")


@dataclass(frozen=True)
class UnmanagedNativeProject(NativeProject):
    @contextmanager
    def bound_arguments(self):
        yield self.cli_arguments

    def require_unmanaged(self) -> UnmanagedNativeProject:
        return self


@dataclass(frozen=True)
class ManagedNativeProject(NativeProject):
    socket: RuntimeSocket
    request: ProjectRuntimeRequest

    @classmethod
    def capture(cls, root: Path, snapshot: RegistrySnapshot, thread: Thread) -> ManagedNativeProject:
        request = ProjectRuntimeRequest.for_native(snapshot, thread)
        return cls(RuntimeSocket(RuntimeSocket.path_for(root, request.binding.process.pid)), request)

    @classmethod
    def from_entry(cls, root: Path, entry: RegistryEntry) -> ManagedNativeProject:
        entry.status.require_active()
        process = entry.thread.require_process()
        request = ProjectRuntimeRequest(thread=entry.thread.name,
            binding=LiveThreadOwnerBinding(entry.owner_identity, process))
        return cls(RuntimeSocket(RuntimeSocket.path_for(root, process.pid)), request)

    def require_managed(self) -> ManagedNativeProject:
        return self

    @contextmanager
    def bound_arguments(self):
        with self.socket.bound() as socket:
            yield replace(self, socket=socket).cli_arguments
