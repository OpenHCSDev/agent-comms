"""The caller bound at tool entry owns caller-relative tool behavior."""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, NoReturn

from .launch_runtime import CallingThreadUnavailable, RestartEnvironment

if TYPE_CHECKING:
    from .comms import Comms
    from .tools import ToolRequest


class ToolCaller(ABC):
    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> ToolCaller:
        """Decode the CLI/native process boundary once, never inside a tool."""
        name = environment.get(RestartEnvironment.owner_key, "")
        return ThreadCaller(name) if name else NoThreadCaller()

    @classmethod
    def from_actor(cls, actor: str | None) -> ToolCaller:
        """The context-action boundary already supplies its actor explicitly."""
        return ThreadCaller(actor) if actor else NoThreadCaller()

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
    def invoke(self, request: ToolRequest, comms: Comms) -> object:
        return request.apply_without_thread(comms, self)

    def listing(self, comms: Comms, result: dict[str, object]) -> dict[str, object]:
        return {**result, "sent_notifications": []}

    def refuse_thread(self) -> NoReturn:
        raise CallingThreadUnavailable("This tool requires a thread caller.")

    def subject(self, named: str) -> str:
        if not named:
            return self.refuse_thread()
        return named

    def may_reconfigure(self, comms: Comms, name: str) -> bool:
        return not comms.registry.status(name).active


@dataclass(frozen=True)
class ThreadCaller(ToolCaller):
    name: str

    def invoke(self, request: ToolRequest[ThreadCaller], comms: Comms) -> object:
        return request.apply(comms, self)

    def listing(self, comms: Comms, result: dict[str, object]) -> dict[str, object]:
        presentation = comms.views.require_thread_presentation(self.name)
        return {**result, "sent_notifications": list(presentation.sent_notifications)}

    def subject(self, named: str) -> str:
        return named or self.name

    def may_reconfigure(self, comms: Comms, name: str) -> bool:
        return (not comms.registry.status(name).active
                or comms.registry.canonical_name(name) == comms.registry.canonical_name(self.name))
