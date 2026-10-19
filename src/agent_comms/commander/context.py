"""The selected root's services, shared by commander command families."""

from __future__ import annotations

import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Self, cast

from pydantic import PrivateAttr

from ..comms import Comms
from ..external_transcript import ExternalTranscript
from ..goals import OrderCheck
from ..messages import Message
from ..recorded_time import EventTime
from ..todos import OrderProgress, Todo, VerdictAudience
from ..tool_caller import ThreadCaller
from ..tool_result import ToolResult
from ..tools import CommsSendTool, CommsTodoListTool, MessagePublished, TodoList, ToolRequest
from .config import CommanderConfig


class CommanderReply(ABC):
    @abstractmethod
    def suffix(self) -> str: ...


@dataclass(frozen=True)
class NoCommanderReply(CommanderReply):
    def suffix(self) -> str:
        return "; no reply since the order"


@dataclass(frozen=True)
class RecordedCommanderReply(CommanderReply):
    message: Message

    def suffix(self) -> str:
        latest = self.message
        at = time.strftime("%H:%M", time.localtime(latest.timestamp))
        return (
            f"; REPLIED {at} [{latest.message_id}]: "
            f"{' '.join(latest.body.split())[:160]} -- read it before pushing"
        )


class CommanderContext(CommanderConfig):
    """A command's selected configuration and opened bus, never an import-time singleton."""

    _comms: Comms = PrivateAttr()

    def model_post_init(self, context: object) -> None:
        self._comms = Comms(self.root)

    @property
    def comms(self) -> Comms:
        return self._comms

    @classmethod
    def of(cls, config: CommanderConfig) -> Self:
        return cls.model_construct(
            **{name: getattr(config, name) for name in type(config).model_fields}
        )

    def call(self, tool: ToolRequest[ThreadCaller]) -> ToolResult:
        """Every write uses the selected commander's identity and root."""
        return ThreadCaller(self.commander).invoke(tool, self.comms)

    def post(self, audience: VerdictAudience, body: str) -> str:
        target = audience.threads[0] if len(audience.threads) == 1 else "#comms"
        message = cast(
            MessagePublished,
            self.call(
                CommsSendTool(
                    sender=self.commander, target=target, body=body, audience=audience.threads
                )
            ),
        )
        return message.id

    def todos(self) -> tuple[Todo, ...]:
        return self.comms.goals.todos.for_commander(
            self.comms.registry.require(self.commander).incarnation
        )

    def held_orders(self) -> tuple[Todo, ...]:
        return self.comms.goals.todos.held_for_commander(
            self.comms.registry.require(self.commander).incarnation
        )

    def held_by(self, unit: str) -> tuple[Todo, ...]:
        return self.comms.goals.todos.held_by(
            self.comms.registry.require(self.commander).incarnation, unit
        )

    def units(self) -> frozenset[str]:
        return self.comms.goals.todos.units(self.comms.registry.require(self.commander).incarnation)

    def progress(self) -> dict[str, OrderProgress]:
        listed = cast(TodoList, self.call(CommsTodoListTool()))
        return listed.progress

    def importers(self, modules: set[str], check: OrderCheck) -> list[str]:
        pattern = rf"^\s*(from|import) agent_comms\b.*\b({'|'.join(sorted(modules))})\b"
        return [
            str(repo.checkout)
            for repo in self.repos
            if repo.checkout != Path(check.directory).resolve()
            and str(repo.checkout) not in check.command
            and subprocess.run(
                ["grep", "-rqE", "--include=*.py", pattern, str(repo.checkout / repo.source)]
            ).returncode
            == 0
        ]

    def last_activity(self, unit: str) -> EventTime:
        return ExternalTranscript(
            self.comms.root, self.comms.registry.require(unit).incarnation
        ).last_tool_call()

    def reply(self, unit: str, since: float) -> CommanderReply:
        with self.comms.bus.log.opened_wire_snapshot() as source:
            return next(
                (
                    RecordedCommanderReply(message)
                    for message in source.newest_messages()
                    if message.sender == unit
                    and message.timestamp > since
                    and message.target in (self.commander, "#comms")
                ),
                NoCommanderReply(),
            )
