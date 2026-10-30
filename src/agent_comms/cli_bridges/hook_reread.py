"""Resident Claude reread hook behavior, behind the existing session socket."""
from __future__ import annotations

import json
from dataclasses import dataclass
from collections.abc import Mapping
from typing import ClassVar

from agent_comms_hooks import ClaudeSubagentHook
from .hook_read_gate import GatedCall

from agent_comms_hooks.skill import DeckStart
from ..comms import Comms
from agent_comms_hooks.skill import DeckReader
from ..registry_document import RegistrySnapshot


@dataclass(frozen=True)
class SliceReminder(DeckStart):
    """Every `every` tool calls, the next slice of the deck this session's role is dealt."""

    every: ClassVar[int] = 5

    def taken(self, reader: DeckReader, number: int, comms: Comms,
              snapshot: RegistrySnapshot) -> tuple[str, ...]:
        deck = self.deck(reader)
        if not deck.exists():
            return self.widened(reader, comms, snapshot)
        if not number or number % self.every:
            return ()
        slices = json.loads(deck.read_text(encoding="utf-8"))
        return (slices[(number // self.every - 1) % len(slices)],)


@dataclass(frozen=True)
class DeafReminder(SliceReminder):
    """Beside the deck's slice, the RE-ARM line while the commander session's bridge has no
    Monitor reading it. The line rides the next tool call, never the bridge it reports dead."""

    def taken(self, reader: DeckReader, number: int, comms: Comms,
              snapshot: RegistrySnapshot) -> tuple[str, ...]:
        return super().taken(reader, number, comms, snapshot) + reader.deaf(self.session_id)


@dataclass(frozen=True)
class BoardReminder(DeafReminder):
    """New and changed current lines once, then one unchanged count per tool call.

    The bridge derives current lines from Core and owns their delivery evidence.
    """

    def taken(self, reader: DeckReader, number: int, comms: Comms,
              snapshot: RegistrySnapshot) -> tuple[str, ...]:
        return super().taken(reader, number, comms, snapshot) + reader.board(self.session_id)


@dataclass(frozen=True)
class ReminderCall(BoardReminder, GatedCall):
    """One completed event supplies both its read result and its inline reminder."""

    @classmethod
    def of(cls, record: Mapping[str, object], **context: object) -> ReminderCall:
        return (SubagentReminderCall if cls.raised_by_subagent(record) else cls).decode(record, **context)


@dataclass(frozen=True)
class SubagentReminderCall(ClaudeSubagentHook, ReminderCall):
    """A completed subagent call updates only its own deal and counter."""
