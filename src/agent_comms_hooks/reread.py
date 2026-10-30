"""Keep a bound session's sources in front of it while it works.

Claude Code runs this after every tool call. The session start hook orders the sources read
once and deals them into a deck of short slices beside the transcript: the voice, the
commander's indictment, each followed rule's wake-up with its tell, and the recorded
failures, interleaved. Every `every` tool calls one slice goes
into context inline, the next one each time. Nothing repeats verbatim on every call, nothing
waits on a read the commander may skip, and the cadence counts tool calls, so no transcript
parse can stall it.

In the commander's session the same call carries the RE-ARM line while its bridge runs with no Monitor
reading it, since every other route to the commander goes through that dead reader, and the
heartbeat's board, which the bridge derives from Core whenever this hook asks.

Every tool call runs this and the read gate, so neither decides in the hook's own process:
importing Core cost each call about two core-seconds per hook (2026-10-30, six agents keeping
4-6 cores busy on hook startup). The session bridge, which holds Core open, decides both
(ToolHookEvent); the hooks Claude Code runs are its standard-library clients
(agent_comms_hooks.bridged).
"""

from __future__ import annotations

import json
from abc import abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from typing import ClassVar

from agent_comms.declared_family import DeclaredFamily
from agent_comms_hooks import HookOutput
from agent_comms_hooks.read_gate import GatedCall
from agent_comms_hooks.skill import DeckReader, DeckStart


@dataclass(frozen=True)
class SliceReminder(DeckStart):
    """Every `every` tool calls, the next slice of the deck this session's role is dealt."""

    every: ClassVar[int] = 5

    def taken(self, reader: DeckReader) -> tuple[str, ...]:
        if said := self.widened(reader):
            return said
        count = self.count_call()
        if count % self.every:
            return ()
        slices = json.loads(self.deck(reader).read_text(encoding="utf-8"))
        return (slices[(count // self.every - 1) % len(slices)],)


@dataclass(frozen=True)
class DeafReminder(SliceReminder):
    """Beside the deck's slice, the RE-ARM line while the commander session's bridge has no
    Monitor reading it. The line rides the next tool call, never the bridge it reports dead."""

    def taken(self, reader: DeckReader) -> tuple[str, ...]:
        return super().taken(reader) + reader.deaf(self.session_id)


@dataclass(frozen=True)
class BoardReminder(DeafReminder):
    """New and changed current lines once, then one unchanged count per tool call.

    The bridge derives current lines from Core and owns their delivery evidence.
    """

    def taken(self, reader: DeckReader) -> tuple[str, ...]:
        return super().taken(reader) + reader.board(self.session_id)


class ToolHookEvent(DeclaredFamily):
    """What the session bridge decides for one of its session's tool-call hooks; a member
    declares the event Claude Code names in the record."""

    @classmethod
    def answered(cls, line: str) -> str:
        """The answer the hook prints for this record, as its own process would have."""
        record = json.loads(line)
        event = cls.member_named(record["hook_event_name"])()
        return HookOutput.captured(lambda: event.decide(record))

    @abstractmethod
    def decide(self, record: Mapping[str, object]) -> None:
        """Answer this event through the hook's output."""


class BeforeToolCall(ToolHookEvent, declared_name="PreToolUse"):
    """The read gate judges the call before it runs."""

    def decide(self, record: Mapping[str, object]) -> None:
        GatedCall.of(record).judge()


class AfterToolCall(ToolHookEvent, declared_name="PostToolUse"):
    """A completed tool call first records what it read for the read gate, then takes its
    reminder; both decode the one record Claude Code sent."""

    def decide(self, record: Mapping[str, object]) -> None:
        GatedCall.of(record).completed()
        hook = BoardReminder.decode(record)
        if texts := hook.take():
            hook.warn(*texts)
