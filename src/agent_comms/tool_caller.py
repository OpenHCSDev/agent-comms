"""The explicit caller owns caller-relative behavior, never the environment."""
from __future__ import annotations

from abc import ABC, abstractmethod
import argparse
from collections.abc import Sequence
from typing import ClassVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, NoReturn

from .launch_runtime import CallingThreadUnavailable
from .cli_options import JsonFlagRecord
from .native_project import NativeProject, UnmanagedNativeProject
from .declared_family import DeclaredFamily
from .pi_vocabulary import ServiceTier
from .thread_identity import RecordedThreadIncarnation, ThreadIncarnation

if TYPE_CHECKING:
    from .comms import Comms
    from .tools import ToolRequest
    from .threads import Thread
    from .registry_document import RegistrySnapshot


@dataclass(frozen=True, kw_only=True)
class CallerRecord(DeclaredFamily, affix="CallerRecord"):
    """Native adapters consume the codec's declared caller variants."""
    arguments: tuple[str, ...]


@dataclass(frozen=True)
class OperatorCallerRecord(CallerRecord):
    label: str = "Local operator"


@dataclass(frozen=True)
class ThreadCallerRecord(CallerRecord):
    name: str
    parent: str | None
    task: str | None
    service_tier: ServiceTier


class ToolCaller(ABC):
    @classmethod
    def from_arguments(cls, parser: argparse.ArgumentParser, arguments: list[str],
                       namespace: argparse.Namespace) -> ToolCaller:
        """Decode the caller once with the command parser; retain only unparsed argv."""
        namespace.caller = NoThreadCaller()
        selected, remaining = parser.parse_known_args(arguments, namespace=namespace)
        arguments[:] = remaining
        return selected.caller

    @abstractmethod
    def native_project(self, arguments: Sequence[str], comms: Comms) -> NativeProject: ...

    @property
    @abstractmethod
    def cli_arguments(self) -> tuple[str, ...]: ...
    @classmethod
    def from_actor(cls, comms: Comms, actor: str | None) -> ToolCaller:
        """Resolve an external context-action actor through its registry once."""
        return ThreadCaller(comms.registry.require(actor).incarnation) if actor else NoThreadCaller()

    @abstractmethod
    def native_metadata(self, comms: Comms) -> CallerRecord: ...

    @abstractmethod
    def require_thread(self) -> RecordedThreadIncarnation: ...

    @abstractmethod
    def owns_thread(self, thread: Thread, snapshot: RegistrySnapshot) -> bool: ...

    @abstractmethod
    def invoke(self, request: ToolRequest, comms: Comms) -> object: ...

    @abstractmethod
    def listing(self, comms: Comms, result: dict[str, object]) -> dict[str, object]: ...

    @abstractmethod
    def subject(self, named: str) -> str: ...

    @abstractmethod
    def may_reconfigure(self, comms: Comms, name: str) -> bool: ...


@dataclass(frozen=True)
class NoThreadCaller(ToolCaller):
    def native_project(self, arguments: Sequence[str], comms: Comms) -> NativeProject:
        return NativeProject.from_arguments(arguments, default=UnmanagedNativeProject()).require_unmanaged()

    @property
    def cli_arguments(self) -> tuple[str, ...]:
        return ()

    def native_metadata(self, comms: Comms) -> OperatorCallerRecord:
        return OperatorCallerRecord(arguments=())

    def require_thread(self) -> NoReturn:
        raise CallingThreadUnavailable("This operation requires an explicit recorded thread caller.")

    def owns_thread(self, thread: Thread, snapshot: RegistrySnapshot) -> bool:
        return False

    def invoke(self, request: ToolRequest, comms: Comms) -> object:
        return request.apply_without_thread(comms, self)

    def listing(self, comms: Comms, result: dict[str, object]) -> dict[str, object]:
        return {**result, "sent_notifications": []}

    def subject(self, named: str) -> str:
        if not named:
            self.require_thread()
        return named

    def may_reconfigure(self, comms: Comms, name: str) -> bool:
        return not comms.registry.status(name).active


@dataclass(frozen=True)
class ThreadCaller(JsonFlagRecord, ToolCaller):
    incarnation: RecordedThreadIncarnation
    value_flag: ClassVar[str] = ThreadIncarnation.value_flag

    @classmethod
    def from_json(cls, payload: str) -> ThreadCaller:
        return cls(ThreadIncarnation.from_json(payload).require_recorded())

    def __post_init__(self) -> None:
        self.incarnation.require_recorded()

    def native_project(self, arguments: Sequence[str], comms: Comms) -> NativeProject:
        project = NativeProject.from_arguments(arguments).require_managed()
        snapshot = comms.registry.snapshot()
        original = project.request.require_original(snapshot)
        if self.incarnation.resolved(snapshot) != original.incarnation:
            raise ValueError("Native project and caller name different original owners")
        return project

    def to_json(self) -> str:
        return self.incarnation.to_json()

    @property
    def cli_arguments(self) -> tuple[str, ...]:
        return self.incarnation.cli_arguments

    @property
    def name(self) -> str:
        return self.incarnation.name

    def native_metadata(self, comms: Comms) -> ThreadCallerRecord:
        snapshot = comms.registry.snapshot()
        if not self.incarnation.current(snapshot):
            raise CallingThreadUnavailable("The original caller is no longer current.")
        thread = snapshot.threads[snapshot.canonical_name(self.name)]
        return ThreadCallerRecord(thread.name, thread.parent, thread.task, thread.service_tier,
                                  arguments=self.cli_arguments)

    def require_thread(self) -> RecordedThreadIncarnation:
        return self.incarnation

    def owns_thread(self, thread: Thread, snapshot: RegistrySnapshot) -> bool:
        return self.incarnation.resolved(snapshot) == thread.incarnation

    def invoke(self, request: ToolRequest[ThreadCaller], comms: Comms) -> object:
        if not self.incarnation.current(comms.registry.snapshot()):
            raise CallingThreadUnavailable("The original caller is no longer current.")
        return request.apply(comms, self)

    def listing(self, comms: Comms, result: dict[str, object]) -> dict[str, object]:
        presentation = comms.views.require_thread_presentation(self.name)
        return {**result, "sent_notifications": list(presentation.sent_notifications)}

    def subject(self, named: str) -> str:
        return named or self.name

    def may_reconfigure(self, comms: Comms, name: str) -> bool:
        return (not comms.registry.status(name).active
                or self.incarnation.current(comms.registry.snapshot())
                and comms.registry.canonical_name(name) == comms.registry.canonical_name(self.name))
