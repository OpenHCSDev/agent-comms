"""The caller bound at tool entry owns caller-relative tool behavior."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from contextlib import contextmanager
from collections.abc import Iterator
from typing import TYPE_CHECKING, Annotated, NoReturn

from .cli_result import ThreadToolCatalogResult, ToolCatalogResult
from .field_codec import WireForm
from .launch_root import LaunchRoot
from .launch_runtime import CallingThreadUnavailable
from .errors import RelationViolationError
from .thread_execution import ExternalThreadExecution
from .thread_status import RunningThreadStatus, ThreadStatus
from .thread_text import ForkedParent
from .store_files import _store_lock
from .presentation import MessageNotification, ThreadRecord
from .tool_result import ToolResult

if TYPE_CHECKING:
    from .comms import Comms
    from .messages import Message
    from .tools import ToolRequest


@dataclass(frozen=True)
class ThreadListing(ToolResult):
    threads: tuple[Annotated[ThreadRecord, WireForm], ...]
    sent_notifications: tuple[MessageNotification, ...]


class ToolCaller(ABC):
    @classmethod
    def from_actor(cls, actor: str | None) -> ToolCaller:
        """Each entry boundary (CLI --from, context action) names its actor explicitly."""
        return ThreadCaller(actor) if actor else NoThreadCaller()

    @abstractmethod
    def invoke(self, request: ToolRequest, comms: Comms) -> ToolResult: ...

    @abstractmethod
    def listing(self, comms: Comms, threads: tuple[ThreadRecord, ...]) -> ThreadListing: ...

    @abstractmethod
    def catalog(self, comms: Comms, tools: tuple[dict[str, object], ...]) -> ToolCatalogResult: ...

    @abstractmethod
    def acting_name(self, default: str) -> str: ...

    @abstractmethod
    def from_flag(self) -> tuple[str, ...]:
        """The command line's --from argument naming this caller, if it names a thread."""

    @abstractmethod
    def own_thread(self) -> str:
        """The thread a tool acts on when its call names none."""

    def subject(self, named: str) -> str:
        return named or self.own_thread()

    def commanding(self, comms: Comms, commander: str) -> ThreadCaller:
        return ThreadCaller(self.acting_name(commander))

    @abstractmethod
    def may_reconfigure(self, comms: Comms, name: str) -> bool: ...

    @abstractmethod
    def require_directive(self, comms: Comms, body: str) -> None:
        """A caller whose sends direct units refuses a body its directive checks refuse."""

    @abstractmethod
    def sent(self, comms: Comms, message: Message) -> None:
        """What a message this caller sent records beyond the bus."""


@dataclass(frozen=True)
class NoThreadCaller(ToolCaller):
    def invoke(self, request: ToolRequest, comms: Comms) -> ToolResult:
        return request.apply_without_thread(comms, self)

    def listing(self, comms: Comms, threads: tuple[ThreadRecord, ...]) -> ThreadListing:
        return ThreadListing(threads, ())

    def catalog(self, comms: Comms, tools: tuple[dict[str, object], ...]) -> ToolCatalogResult:
        return ToolCatalogResult(tools)

    def refuse_thread(self) -> NoReturn:
        raise CallingThreadUnavailable("This tool requires a thread caller.")

    def acting_name(self, default: str) -> str:
        return default

    def from_flag(self) -> tuple[str, ...]:
        return ()

    def own_thread(self) -> NoReturn:
        return self.refuse_thread()

    def may_reconfigure(self, comms: Comms, name: str) -> bool:
        return not comms.registry.status(name).active

    def require_directive(self, comms: Comms, body: str) -> None:
        """A caller with no thread gives no directive."""

    def sent(self, comms: Comms, message: Message) -> None:
        """A message sent without a calling thread records nothing more."""


@dataclass(frozen=True)
class ThreadCaller(ToolCaller):
    name: str

    def invoke(self, request: ToolRequest[ThreadCaller], comms: Comms) -> ToolResult:
        return request.apply(comms, self)

    def listing(self, comms: Comms, threads: tuple[ThreadRecord, ...]) -> ThreadListing:
        presentation = comms.views.thread_presentations((self.name,)).require(self.name)
        return ThreadListing(threads, presentation.sent_notifications)

    def catalog(self, comms: Comms, tools: tuple[dict[str, object], ...]) -> ToolCatalogResult:
        return ThreadToolCatalogResult(
            tools,
            comms.registry.require(self.name).service_tier,
            LaunchRoot.from_path(comms.root).arguments(),
        )

    def acting_name(self, default: str) -> str:
        return self.name

    def from_flag(self) -> tuple[str, ...]:
        return ("--from", self.name)

    def own_thread(self) -> str:
        return self.name

    def may_reconfigure(self, comms: Comms, name: str) -> bool:
        return (not comms.registry.status(name).active
                or comms.registry.canonical_name(name) == comms.registry.canonical_name(self.name))

    def require_directive(self, comms: Comms, body: str) -> None:
        """A unit's or a script's send is a message, not a directive."""

    def sent(self, comms: Comms, message: Message) -> None:
        """An agent's message is only the bus record."""

    @contextmanager
    def assigning(self, comms: Comms, unit: str, order: str) -> Iterator[None]:
        comms.goals.todos.take_unit(comms.registry.require(unit).incarnation,
                                   comms.registry.require(self.name).incarnation, order)
        try:
            yield
        finally:
            self.assigned(comms, unit, order)

    def assigned(self, comms: Comms, unit: str, order: str) -> None:
        """A synchronous command releases its assignment when it returns."""
        comms.goals.todos.release_unit(
            comms.registry.require(unit).incarnation,
            comms.registry.require(self.name).incarnation, order)

    def reported(self, comms: Comms, target: str) -> None:
        """An ordinary commander's post does not end its thread."""


class CommanderForkExecution(ExternalThreadExecution):
    """A commander fork reads its inbox through verbs, not a native agent or bridge."""

    @classmethod
    def registered_status(cls, thread, requested: ThreadStatus) -> ThreadStatus:
        return requested

    @classmethod
    def keeps_assignment(cls, status: ThreadStatus) -> bool:
        return status.active


@dataclass(frozen=True)
class CommanderForkCaller(ThreadCaller):
    """The first verb declares this child; its closing report ends its assignments."""

    def commanding(self, comms: Comms, commander: str) -> ThreadCaller:
        from .goal_actions import SetGoalAction
        from .goals import RequestGoalOrder

        registry = comms.registry
        # Registration and the fork's own request use the existing wire transaction.
        # Never copy the parent's Goal ID into a second thread.
        with _store_lock(comms.root / 'wire'):
            with registry.store.editing() as edit:
                parent = edit.document.require(commander)
                name = edit.document.canonical_name(self.name)
                if name != self.name:
                    raise RelationViolationError(f"Commander fork {self.name!r} is an alias.")
                existing = edit.document.threads.get(name)
                if existing is not None:
                    if (existing.execution is not CommanderForkExecution
                            or not existing.parent.named(parent.name)):
                        raise RelationViolationError(f"Thread {name!r} is not a fork of {parent.name!r}.")
                    if not edit.document.status(name).receiving:
                        raise RelationViolationError(f"Commander fork {name!r} has retired; use a new name.")
                    return self
                request = ('\n'.join(goal.text for goal in parent.goal.active())
                           or f'Coordinate assigned work for {parent.name}.')
                thread = registry.new_thread(name, parent.tags, parent.worktree,
                    parent=ForkedParent(parent.name), model=parent.model,
                    thinking_level=parent.thinking_level, service_tier=parent.service_tier,
                    execution=CommanderForkExecution)
                registry._commit_registration(edit, edit.document.prepare_registration(
                    thread, RunningThreadStatus(), new_owner=False))
            comms.goals.update_goal(name, SetGoalAction(text=request, order=RequestGoalOrder()))
        return self

    def assigned(self, comms: Comms, unit: str, order: str) -> None:
        """A fork keeps its units until its closing report, not a verb's return."""

    def reported(self, comms: Comms, target: str) -> None:
        if target == '#comms':
            identity = comms.registry.require(self.name).incarnation
            comms.goals.todos.retire_assigner(comms.registry, identity)
