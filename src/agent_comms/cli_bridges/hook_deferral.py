"""Resident Claude deferral hook behavior, behind the existing session socket."""
from __future__ import annotations

import json
import re
from abc import ABC
from collections.abc import (
    Iterable,
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

from .claude_transcript import ClaudeJsonLines
from .session_watch import WatchClaim
from .shared import Binding
from ..comms import Comms
from ..nominal.declared_family import DeclaredFamily
from ..commander.standing import UnownedWheelSpin
from ..rule_deal import MeantStore, RuleDeal, SessionAudit


class Tagged(HookRecord, DeclaredFamily):
    """A record Claude Code tags with "type"; each member declares its tag, and the family's
    one `Untagged` member answers for every tag nobody declared."""

    family_discriminator = "type"

    @classmethod
    def of(cls, record: Mapping[str, object]) -> Self:
        return cls.member_or_default(record.get(cls.family_discriminator), Untagged).decode(record)


class Untagged:
    """The member of a tagged family that stands for every record whose tag it does not read."""


class ReplyBlock(Tagged, declared_name=None):
    """One content block of a model reply."""

    acts: ClassVar[bool] = False
    """Whether the model went on to act, which makes the text before it narration."""

    def said(self) -> str:
        return ""


@dataclass(frozen=True)
class TextBlock(ReplyBlock, declared_name="text"):
    text: str

    def said(self) -> str:
        return self.text


class ToolUseBlock(ReplyBlock, declared_name="tool_use"):
    acts = True


class OtherBlock(ReplyBlock, Untagged):
    pass


class ReplyBlocks(tuple[ReplyBlock, ...]):
    """The message decodes each content block once at its existing field boundary."""

    def __new__(cls, blocks: Iterable[Mapping[str, object]]) -> Self:
        return super().__new__(cls, (ReplyBlock.of(block) for block in blocks))


@dataclass(frozen=True)
class ReplyMessage(HookRecord):
    content: ReplyBlocks


class TranscriptRecord(Tagged, declared_name=None):
    """One transcript line, read backwards to find the reply that ends the turn."""

    @property
    def ends_reply(self) -> bool:
        return False

    def said(self) -> str:
        return ""


@dataclass(frozen=True)
class AssistantRecord(TranscriptRecord, declared_name="assistant"):
    message: ReplyMessage

    @property
    def ends_reply(self) -> bool:
        return any(block.acts for block in self.message.content)

    def said(self) -> str:
        return "".join(block.said() for block in self.message.content)


class UserRecord(TranscriptRecord, declared_name="user"):
    """A prompt or a tool result; the final reply comes after it."""

    @property
    def ends_reply(self) -> bool:
        return True


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


@dataclass(frozen=True)
class ReadIdle:
    """The idle owner's answer; Core read bugs propagate instead of becoming a reply."""

    units: tuple[str, ...]

    @classmethod
    def read(cls, comms: Comms, binding: Binding) -> Self:
        return cls(tuple(unit.name for unit in UnownedWheelSpin.read_idle(comms, binding.owner)))

    def reasons(self, deferred: tuple[str, ...]) -> Iterator[str]:
        if self.units:
            yield (f"You deferred {quoted(deferred)} while {', '.join(self.units)} hold no order:"
                   " order it now.")


def quoted(phrases: tuple[str, ...]) -> str:
    return ", ".join(f"'{phrase}'" for phrase in dict.fromkeys(phrases))


@dataclass(frozen=True)
class StopHook(ClaudeTranscriptHook):
    stop_hook_active: bool
    comms: Comms = field(kw_only=True, compare=False, repr=False)
    binding: Binding = field(kw_only=True, compare=False, repr=False)

    idle_turns_asked: ClassVar[int] = 3

    @cached_property
    def idle(self) -> ReadIdle:
        return ReadIdle.read(self.comms, self.binding)

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
        snapshot = self.comms.registry.snapshot()
        snapshot.born(self.binding.owner.require_recorded().lookup)
        deaf = tuple(watch.line() for watch in WatchClaim(self.session_id).deaf())
        if reasons := (*deaf, *self.deferred()):
            self.output.line(json.dumps({"decision": "block", "reason": "\n".join(reasons)}))

    def deferred(self) -> tuple[str, ...]:
        """Every bound collaborator answers for its own deferred work."""
        reply = self.reply()
        return (*(
            reason
            for shape in Deferral.__subclasses__()
            if (deferred := tuple(shape.found(reply)))
            for reason in shape.reasons(deferred, self)
        ), *self.unkept_word())

    def unkept_word(self) -> Iterator[str]:
        """A yes opens the deal, because a session mid-deal can only read and could never prove
        one. The proof comes at the turn's end: turns in a row that changed nothing are held up
        against what the session wrote, and he asks again. stop_hook_active lets the next stop
        through, so this asks once per stop and never loops. A question still open is the
        session answering, not idling."""
        if MeantStore(self.stem.with_suffix(".meant")).read().open:
            return
        receipts = ClaudeJsonLines().receipts(self.tail())
        if receipts.idle_turns() >= self.idle_turns_asked:
            asked = SessionAudit.read(RuleDeal.audit_beside(self.stem)).his_question(receipts)
            yield (f"Tristan is asking you:\n\n{asked}\n\nYour last {receipts.idle_turns()} turns"
                   " ran nothing but reads. Compare that with what you wrote and tell him the"
                   " truth, then change something. No is a real answer.")
