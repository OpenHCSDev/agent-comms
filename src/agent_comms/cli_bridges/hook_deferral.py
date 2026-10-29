"""Resident Claude deferral hook behavior, behind the existing session socket."""
from __future__ import annotations

import json
import re
from abc import (
    ABC,
    abstractmethod,
)
from collections.abc import (
    Iterator,
    Mapping,
)
from dataclasses import dataclass, field
from functools import cached_property
from typing import (
    ClassVar,
    Self,
)

from agent_comms_hooks import ClaudeTranscriptHook, HookRecord

from .shared import Binding
from ..comms import Comms
from ..commander.standing import UnownedWheelSpin


class Tagged(HookRecord):
    """A record Claude Code tags with "type"; each member declares its tag, and the family's
    one `Untagged` member answers for every tag nobody declared."""

    tag: ClassVar[str]

    @classmethod
    def of(cls, record: Mapping[str, object]) -> Self:
        members = cls.__subclasses__()
        (other,) = (kind for kind in members if issubclass(kind, Untagged))
        kinds = {kind.tag: kind for kind in members if not issubclass(kind, Untagged)}
        return kinds.get(record.get("type"), other).decode(record)


class Untagged:
    """The member of a tagged family that stands for every record whose tag it does not read."""


class ReplyBlock(Tagged):
    """One content block of a model reply."""

    acts: ClassVar[bool] = False
    """Whether the model went on to act, which makes the text before it narration."""

    def said(self) -> str:
        return ""


@dataclass(frozen=True)
class TextBlock(ReplyBlock):
    tag = "text"
    text: str

    def said(self) -> str:
        return self.text


class ToolUseBlock(ReplyBlock):
    tag = "tool_use"
    acts = True


class OtherBlock(ReplyBlock, Untagged):
    pass


@dataclass(frozen=True)
class ReplyMessage(HookRecord):
    content: list[Mapping[str, object]]


class TranscriptRecord(Tagged):
    """One transcript line, read backwards to find the reply that ends the turn."""

    ends_reply: ClassVar[bool] = False

    def said(self) -> str:
        return ""


@dataclass(frozen=True)
class AssistantRecord(TranscriptRecord):
    tag = "assistant"
    message: ReplyMessage

    @property
    def blocks(self) -> tuple[ReplyBlock, ...]:
        return tuple(ReplyBlock.of(block) for block in self.message.content)

    @property
    def ends_reply(self) -> bool:
        return any(block.acts for block in self.blocks)

    def said(self) -> str:
        return "".join(block.said() for block in self.blocks)


class UserRecord(TranscriptRecord):
    """A prompt or a tool result; the final reply comes after it."""

    tag = "user"
    ends_reply = True


class OtherRecord(TranscriptRecord, Untagged):
    pass


class Deferral(ABC):
    """A shape of reply owns when its deferred work or attention blocks a stop."""

    phrases: ClassVar[tuple[str, ...]]

    @classmethod
    def pattern(cls) -> re.Pattern[str]:
        return re.compile("|".join(rf"\b{phrase}\b" for phrase in cls.phrases), re.IGNORECASE)

    @classmethod
    def found(cls, reply: str) -> Iterator[str]:
        return (match.group(0) for match in cls.pattern().finditer(reply))

    @classmethod
    def reasons(cls, deferred: tuple[str, ...], stop: StopHook) -> Iterator[str]:
        yield from stop.idle.reasons(deferred)


class NextTick(Deferral):
    phrases = (r"(?:goes|go|going|comes)(?: to(?: \w+){1,2})? next", r"after this", r"next tick")


class Later(Deferral):
    phrases = (r"later", r"follow-?up", r"when I get to", r"in a bit", r"for now")


class FutureOrder(Deferral):
    phrases = (
        r"will (?:order|assign|dispatch)",
        r"goes? to a unit",
        r"I['’]?ll (?:order|assign|dispatch|hand)",
        r"to be ordered",
        r"(?:has|have) no owner",
    )


class Waiting(Deferral):
    phrases = (
        r"waiting\s+on",
        r"still\s+waiting",
        r"once\s+[^\n.!?;:]+?\s+reports?",
        r"when\s+the\s+forks\s+report",
        r"next:\s*[^\n]*?\bas\s+[^\n.!?;:]+?\s+reports?",
    )

    @classmethod
    def reasons(cls, deferred: tuple[str, ...], stop: StopHook) -> Iterator[str]:
        yield (f"You ended on {quoted(deferred)}; every wait is prep: read the next unit's "
               "trajectory or hunt a hot path now")


class IdleRead(ABC):
    """Core's idle answer or an explicit failed read, on the admitted resident context."""

    @abstractmethod
    def reasons(self, deferred: tuple[str, ...]) -> Iterator[str]: ...

    @staticmethod
    def read(comms: Comms, binding: Binding) -> IdleRead:
        try:
            return ReadIdle(tuple(unit.name for unit in UnownedWheelSpin.read_idle(comms, binding.owner)))
        except Exception as failed:
            # A failed read is an explicit blocking result, never an empty/successful answer.
            return FailedIdleRead(str(failed))


@dataclass(frozen=True)
class ReadIdle(IdleRead):
    units: tuple[str, ...]

    def reasons(self, deferred: tuple[str, ...]) -> Iterator[str]:
        if self.units:
            yield (f"You deferred {quoted(deferred)} while {', '.join(self.units)} hold no order:"
                   " order it now.")


@dataclass(frozen=True)
class FailedIdleRead(IdleRead):
    error: str

    def reasons(self, deferred: tuple[str, ...]) -> Iterator[str]:
        yield (f"You deferred {quoted(deferred)} and Core's idle read failed ({self.error}):"
               " repair the Core idle read and order every idle unit now.")


def quoted(phrases: tuple[str, ...]) -> str:
    return ", ".join(f"'{phrase}'" for phrase in dict.fromkeys(phrases))


@dataclass(frozen=True)
class StopHook(ClaudeTranscriptHook):
    stop_hook_active: bool
    comms: Comms = field(kw_only=True, compare=False, repr=False)
    binding: Binding = field(kw_only=True, compare=False, repr=False)

    @cached_property
    def idle(self) -> IdleRead:
        return IdleRead.read(self.comms, self.binding)

    def reply(self) -> str:
        """The text of the reply that ends the turn: what the model said after its last act."""
        said: list[str] = []
        for line in reversed(self.tail()):
            record = TranscriptRecord.of(json.loads(line))
            if record.ends_reply:
                break
            said.insert(0, record.said())
        return "\n".join(said)

    def guard(self) -> None:
        """A stop this hook already sent back is let through, so it never loops."""
        if self.stop_hook_active:
            return
        selected = self.comms.registry.snapshot().commander_project.session_output(
            self.binding.owner, commander=self.judge_reply, commanded=lambda: None)
        selected()

    def judge_reply(self) -> None:
        reply = self.reply()
        reasons = tuple(
            reason
            for shape in Deferral.__subclasses__()
            if (deferred := tuple(shape.found(reply)))
            for reason in shape.reasons(deferred, self)
        )
        if reasons:
            self.output.line(json.dumps({"decision": "block", "reason": "\n".join(reasons)}))
