"""The Pi leaf of the rule gate (rule_deal): Core deals a Pi thread its rules at its first start
and whenever Pi reports a finished compaction, and every Pi tool transport judges every call
against that deal. The deck readers are the one source list, shared with the Claude hooks.

The deal lives in the thread's scratch directory, the one place its storage rule lets it write
outside its checkouts, so the audit it owes is a file its Write tool may create. A refusal is a
tool denial: Pi shows its text to the model as the tool's error.

Tristan asks a Pi thread "did you mean it?" the way he asks a Claude session: a line in his own
voice, which the thread reads as a user follow-up. Its own audit carries the explanatory
answer to that exact question, through the same owner and policy as every other backend.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Self

from agent_comms.comms import Comms
from agent_comms.coordination.coordinator import Coordination
from agent_comms.messaging.bus_publication import Delivered
from agent_comms.nominal.field_codec import ThreadLookup
from agent_comms.registry.threads import Thread
from agent_comms.team.meant_voice import BusVoice
from agent_comms.team.rule_deal import (
    AuditDue,
    DealDelivery,
    DealTrigger,
    DealtRuleGate,
    Redeals,
    Voice,
)
from agent_comms.transport.tool_denial import SelectedToolDenied
from agent_comms_hooks.skill import DeckReader, RulesOnly

if TYPE_CHECKING:
    from agent_comms.execution.transcript_sources import TranscriptSource
    from agent_comms.registry.registry_document import RegistrySnapshot
    from agent_comms.transport.native_tool_call import NativeToolCall


class RulesUnread(SelectedToolDenied):
    """A Pi tool call refused until the thread has read its dealt rules and written its audit."""


@dataclass(frozen=True)
class PiRuleGate(DealtRuleGate):
    """One thread's deal, judged where its tools run: paths resolve against its worktree. With
    no bus seat it never asks."""

    scratch: Path
    worktree: Path

    @classmethod
    def of(cls, comms: Comms, thread: Thread) -> PiThreadGate:
        """The gate a running thread's tools are judged by: seated on its bus, so it asks."""
        return PiThreadGate.seated(comms, thread)

    @property
    def stem(self) -> Path:
        return self.scratch / "compaction-deal"

    def refuse(self, reason: str) -> None:
        raise RulesUnread(reason)

    async def judge_call[Call: NativeToolCall](self, call: Call) -> None:
        """Blocking gate/store work stays with the existing coordination worker."""
        await Coordination.run_worker(partial(call.judge_under, self))


@dataclass(frozen=True)
class PiThreadGate(PiRuleGate, BusVoice):
    """A thread's deal seated on its bus: publish the owner's question and format receipts.

    Successful audit edits settle answers through the shared RuleDeal owner.
    """

    comms: Comms = field(compare=False, repr=False)
    thread: Thread = field(compare=False, repr=False)

    @classmethod
    def seated(cls, comms: Comms, thread: Thread) -> Self:
        return cls(comms.registry.storage_rule.roots_of(thread).scratch, thread.worktree.path,
                   comms, thread)

    def session(self) -> TranscriptSource:
        """The thread's own record of what it said and did."""
        return self.thread.runtime.history_source(self.comms.root, self.thread.incarnation)

    def voice(self) -> Voice:
        return self.session().voice(self)

    def speaker(self) -> str:
        return self.thread.name

    def delivery(self) -> DealDelivery:
        """The thread's runtime knows what its launcher put in the system prompt."""
        return self.thread.runtime.deal_delivery()


@dataclass(frozen=True)
class PiRedeal(PiThreadGate, Redeals):
    """The shared sources and owner's latest words for this independent session."""

    @classmethod
    def of(cls, comms: Comms, thread: Thread) -> PiRedeal:
        return cls.seated(comms, thread)

    def renew(self, trigger: DealTrigger) -> None:
        """A start or compaction deals the papers, then the rules."""
        self.deal_from(DeckReader, trigger)

    def reread(self, trigger: DealTrigger) -> None:
        self.deal_from(RulesOnly, trigger)

    def deal_from(self, reader: type[DeckReader], trigger: DealTrigger) -> None:
        """Material selection is explicit; the captured incarnation still binds the deal."""
        snapshot = self.comms.registry.snapshot()
        snapshot.born(self.thread.incarnation.require_recorded().lookup)
        self.deal(reader, AuditDue(self.owner_words(snapshot),
                                   snapshot.team_project.unique_repositories()), trigger)

    def owner_words(self, snapshot: RegistrySnapshot) -> tuple[str, ...]:
        """What the registry's human last typed to this thread, oldest first: the same person
        whose prompts the Claude deal reads from its transcript. Core's notices (the heartbeat,
        escalations, restart reminders) publish under his thread too; his thread's role asks
        each message's task whether it carries his words, so a notice is never dealt as them."""
        lookup = self.thread.incarnation.require_recorded().lookup
        return tuple(reversed(tuple(
            delivery.message.body
            for user in snapshot.user_thread().existing()
            for delivery in self.typed_by(user, lookup)
        )[:self.owner_prompts]))

    def typed_by(self, user: Thread, lookup: ThreadLookup) -> Iterator[Delivered]:
        """The human's own messages to this thread, newest first, read a page at a time
        until the deal has its prompts or his history ends."""
        sender = (user.incarnation.require_recorded().lookup,)
        before: int | None = None
        found = 0
        while found < self.owner_prompts and (page := self.comms.bus.bus.received(
            lookup, sender_lookups=sender, before=before, limit=self.owner_prompts * 8,
        )):
            for delivery in page:
                if user.role.user_task_facts(delivery.message):
                    found += 1
                    yield delivery
            before = page[-1].seq
