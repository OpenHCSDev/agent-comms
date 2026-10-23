"""Send the commander back when its final reply parks work or its own attention.

Claude Code runs this when the model stops. A final reply that parks work for "next",
"later" or a future order, while Core counts a unit idle, is work the commander could
hand out this tick. Waiting parks the commander's attention regardless of idle units:
every wait is prep. The hook imports only the standard library and reads idle units from
Core's own `agent-comms-commander order idle` only for shapes that need that answer.
"""

from __future__ import annotations

import json
import re
import subprocess
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from functools import cached_property
from typing import ClassVar, Self

from agent_comms_hooks import ClaudeHookProgram, ClaudeTranscriptHook, HookRecord


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
    def reasons(cls, deferred: tuple[str, ...], idle: IdleRead) -> Iterator[str]:
        yield from idle.reasons(deferred)

    @staticmethod
    def phrases_in(reply: str) -> tuple[str, ...]:
        return tuple(phrase for shape in Deferral.__subclasses__() for phrase in shape.found(reply))


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
    def reasons(cls, deferred: tuple[str, ...], idle: IdleRead) -> Iterator[str]:
        yield (f"You ended on {quoted(deferred)}; every wait is prep: read the next unit's "
               "trajectory or hunt a hot path now")


class IdleRead(ABC):
    """Core's answer to which units hold no order, read through its own command."""

    command: ClassVar[tuple[str, ...]] = ("agent-comms-commander", "order", "idle")
    timeout: ClassVar[float] = 30

    @abstractmethod
    def reasons(self, deferred: tuple[str, ...]) -> Iterator[str]: ...

    @staticmethod
    def read() -> IdleRead:
        """Core's exit status is its answer; a command that cannot start or never answers
        is a broken install, and the hook fails with it instead of guessing."""
        run = subprocess.run(IdleRead.command, capture_output=True, text=True,
                             timeout=IdleRead.timeout)
        if run.returncode:
            return FailedIdleRead(run.stderr.strip()[-300:] or f"exit status {run.returncode}")
        return ReadIdle(tuple(run.stdout.split()))


class UnreadIdleRead(IdleRead):
    """One stop's idle answer, acquired only when a matched shape asks for it."""

    @cached_property
    def answer(self) -> IdleRead:
        return self.read()

    def reasons(self, deferred: tuple[str, ...]) -> Iterator[str]:
        yield from self.answer.reasons(deferred)


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
               f" run `{' '.join(self.command)}` and order every idle unit now.")


def quoted(phrases: tuple[str, ...]) -> str:
    return ", ".join(f"'{phrase}'" for phrase in dict.fromkeys(phrases))


@dataclass(frozen=True)
class StopHook(ClaudeTranscriptHook):
    stop_hook_active: bool

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
        if self.stop_hook_active or not self.parts.exists():
            return
        reply, idle = self.reply(), UnreadIdleRead()
        reasons = tuple(
            reason
            for shape in Deferral.__subclasses__()
            for deferred in (tuple(shape.found(reply)),)
            if deferred
            for reason in shape.reasons(deferred, idle)
        )
        if reasons:
            print(json.dumps({"decision": "block", "reason": "\n".join(reasons)}))


class DeferralGuard(ClaudeHookProgram):
    events = ("Stop",)

    @classmethod
    def command(cls) -> str:
        return cls.console_script(f"{main.__module__}:{main.__name__}")


def main() -> None:
    StopHook.read().guard()


if __name__ == "__main__":
    main()
