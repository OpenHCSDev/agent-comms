"""The Pi leaf of the rule gate (rule_deal): Core deals a Pi thread its rules at its first start
and whenever Pi reports a finished compaction, and every Pi tool transport judges every call
against that deal. The deck readers are the one source list, shared with the Claude hooks.

The deal lives in the thread's scratch directory, the one place its storage rule lets it write
outside its checkouts, so the audit it owes is a file its Write tool may create. A refusal is a
tool denial: Pi shows its text to the model as the tool's error.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from agent_comms_hooks.skill import (
    CommanderReader,
    CommanderRulesReader,
    DeckReader,
    FleetReader,
    FleetRulesReader,
)

from .rule_deal import DealtRuleGate, Redeals
from .tool_denial import SelectedToolDenied

if TYPE_CHECKING:
    from .comms import Comms
    from .registry_document import RegistrySnapshot
    from .threads import Thread


class RulesUnread(SelectedToolDenied):
    """A Pi tool call refused until the thread has read its dealt rules and written its audit."""


@dataclass(frozen=True)
class PiRuleGate(DealtRuleGate):
    """One thread's deal, judged where its tools run: paths resolve against its worktree."""

    scratch: Path
    worktree: Path

    @classmethod
    def of(cls, comms: Comms, thread: Thread) -> PiRuleGate:
        return cls(comms.registry.storage_rule.roots_of(thread).scratch, thread.worktree.path)

    @property
    def stem(self) -> Path:
        return self.scratch / "compaction-deal"

    def refuse(self, reason: str) -> None:
        raise RulesUnread(reason)


@dataclass(frozen=True)
class PiRedeal(PiRuleGate, Redeals):
    """The deal Core renews for a thread: its role's sources and the owner's latest words."""

    comms: Comms
    thread: Thread

    @classmethod
    def of(cls, comms: Comms, thread: Thread) -> PiRedeal:
        gate = PiRuleGate.of(comms, thread)
        return cls(gate.scratch, gate.worktree, comms, thread)

    def renew(self) -> None:
        """A start or compaction deals the papers, then the rules."""
        self.deal_from(CommanderReader, FleetReader)

    def reread(self) -> None:
        self.deal_from(CommanderRulesReader, FleetRulesReader)

    def deal_from(self, commander: type[DeckReader], commanded: type[DeckReader]) -> None:
        """The registry's commander project picks the deck reader whose sources it deals."""
        snapshot = self.comms.registry.snapshot()
        snapshot.commander_project.deal_rules(
            self,
            self.thread.incarnation,
            commander=commander,
            commanded=commanded,
            prompts=self.owner_words(snapshot),
        )

    def owner_words(self, snapshot: RegistrySnapshot) -> tuple[str, ...]:
        """What the registry's human last sent this thread, oldest first: the same person
        whose prompts the Claude deal reads from its transcript."""
        lookup = self.thread.incarnation.require_recorded().lookup
        return tuple(
            reversed(
                tuple(
                    delivery.message.body
                    for user in snapshot.user_thread().existing()
                    for delivery in self.comms.bus.bus.received(
                        lookup,
                        sender_lookups=(user.incarnation.require_recorded().lookup,),
                        limit=self.owner_prompts,
                    )
                )
            )
        )
