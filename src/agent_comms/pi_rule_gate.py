"""The Pi leaf of the rule gate (rule_deal): Core deals a Pi thread its rules at its first start
and whenever Pi reports a finished compaction, and every Pi tool transport judges every call
against that deal. The deck readers are the one source list, shared with the Claude hooks.

The deal lives in the thread's scratch directory, the one place its storage rule lets it write
outside its checkouts, so the audit it owes is a file its Write tool may create. A refusal is a
tool denial: Pi shows its text to the model as the tool's error.

Tristan asks a Pi thread "did you mean it?" the way he asks a Claude session: a line in his own
voice, which the thread reads as a user follow-up. Only the admitted native round's assistant
can answer after its producing context proves that original question was included.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from functools import partial
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Self

from agent_comms_hooks.skill import DeckReader, RulesOnly

from .coordination_errors import IdentityConflict
from .coordinator import Coordination
from .errors import RelationViolationError, StoredHistoryUnreadable
from .input_disposition import InputDispositions
from .native_failures import NativePiUnavailable
from .native_input_proof import NativeContextJournal
from .native_runtime_input import NativeRuntimeInput
from .nominal.field_codec import FieldCodec, MalformedRecord
from .transcripts import TranscriptRead
from .turn_context import RecordedContextTurn
from .turn_item import FreshNativeId
from .bus_publication import Delivered
from .channel_targets import ThreadTarget
from .nominal.field_codec import ThreadLookup
from .rule_deal import Asked, DealTrigger, DealtRuleGate, Redeals, RuleDeal, Speaking, Voice
from .session_receipts import SessionReceipts
from .task_attachments.owner_notices import MeantNotice
from .tool_denial import SelectedToolDenied

if TYPE_CHECKING:
    from .backend import TurnSession
    from .native_entries import NativeEvidenceScope
    from .native_tool_call import NativeToolCall
    from .native_input_proof import NativeContextProof, NativeContextRecord
    from .native_session_files import NativeSessionFiles
    from .messages import MessageInput
    from .native_tool_transport import NativeToolRound, ToolTransport
    from .comms import Comms
    from .registry_document import RegistrySnapshot
    from .threads import Thread
    from .transcript_sources import TranscriptSource


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

    @asynccontextmanager
    async def round_gate[Call: NativeToolCall](
        self, round: NativeToolRound[Call], transport: ToolTransport[Call],
    ) -> AsyncIterator[PiRuleGate]:
        yield self

    async def judge_round[Call: NativeToolCall](
        self, call: Call, round: NativeToolRound[Call], transport: ToolTransport[Call],
    ) -> None:
        async with self.round_gate(round, transport) as gate:
            await Coordination.run_worker(partial(call.judge_under, gate))


@dataclass(frozen=True)
class PiThreadGate(PiRuleGate, Speaking):
    """A thread's deal seated on its bus: publish the owner's question and format receipts.

    Only the round-bound subclass can prove an answer; display history cannot settle it.
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

    def say(self, line: str, deal: RuleDeal) -> Asked:
        """His line goes to the thread under his own thread, as every owner notice does."""
        message = MeantNotice().publish(self.comms.messaging, ThreadTarget(self.thread.name), line)
        return Asked(message.message_id, 0)

    def receipts(self) -> SessionReceipts:
        return self.session().receipts()

    @asynccontextmanager
    async def round_gate[Call: NativeToolCall](
        self, round: NativeToolRound[Call], transport: ToolTransport[Call],
    ) -> AsyncIterator[PiRuleGate]:
        turn = transport.turn
        async with turn.cursor_delivery.reading() as source_reads:
            yield PiRoundGate(
                self.scratch, self.worktree, self.comms, self.thread, round, turn, source_reads,
            )

    def said_after(self, asked: Asked) -> tuple[str, ...]:
        # A historical or out-of-round caller cannot identify a producer.
        return ()


@dataclass(frozen=True)
class PiRoundGate[Call: NativeToolCall](PiThreadGate):
    """Question settlement borrows only this admitted round's producing request."""

    round: NativeToolRound[Call] = field(compare=False, repr=False)
    turn: TurnSession = field(compare=False, repr=False)
    source_reads: NativeEvidenceScope = field(compare=False, repr=False)

    def said_after(self, asked: Asked) -> tuple[str, ...]:
        try:
            self.require_question(asked)
        except (ValueError, MalformedRecord, RelationViolationError, IdentityConflict,
                NativePiUnavailable, StoredHistoryUnreadable) as error:
            raise RulesUnread(f"Original question arrival is unproved: {error}") from error
        return self.round.assistant.said()

    def require_question(self, asked: Asked) -> None:
        lease = self.thread.require_turn_lease()
        manifest = self.comms.contexts.require_current_request(
            self.thread.incarnation, RecordedContextTurn(lease.turn_id, lease.identity),
            self.round.assistant.producing_request.require(),
        )
        identity = self.turn.native.attestation.require_identity()
        context = manifest.committed_context(identity)
        files = self.turn.launch.session.files(self.comms.root)
        message = self.comms.bus.bus.lookup_message(asked.message_id)
        delivery = self.comms.bus.bus.delivery(message.seq)
        lookup = self.thread.incarnation.require_recorded().lookup
        if not any(recipient.lookup == lookup for recipient in delivery.recipients):
            raise ValueError("Original question belongs to another recipient incarnation")
        entries = manifest.user_entries(message.reference)
        if not entries:
            raise ValueError("Original question is absent or transformed in this request")
        for entry in entries:
            with NativeContextJournal.open_evidence(files.input_proof) as db:
                rows = NativeContextJournal.select(
                    db, request_generation=context.request_generation,
                    session_id=identity.session_id,
                    session_entry_id=FieldCodec.decode(FreshNativeId, entry),
                )
            for row in rows:
                if row.llm_context_digest != context.context_digest:
                    raise ValueError("Producing context and native journal disagree")
                for original in self.original_contexts(row, files, message, lookup):
                    if original.input_id != row.input_id:
                        continue
                    observed = row.at(files.session, session_entry_start=original.session_entry_start)
                    original.require_continuation(observed)
                    evidence = self.source_reads.for_source(files.session)
                    if not evidence.corroborates_context(observed, input_id=row.input_id, files=files):
                        raise ValueError("Original question lacks matching native byte evidence")
                    return
        raise ValueError("Original question has no acknowledged input in this request")

    def original_contexts(
        self, row: NativeContextRecord, files: NativeSessionFiles,
        message: MessageInput, lookup: ThreadLookup,
    ) -> tuple[NativeContextProof, ...]:
        root_id = self.comms.registry.store.privacy().namespace
        inputs = InputDispositions(self.comms.root)
        original = inputs.lookup(inputs.bus_key(message, self.thread))
        contexts = original.model_contexts_for(root_id, message.reference)
        if any(context.input_id == row.input_id for context in contexts):
            return contexts
        with TranscriptRead.publication_source(self.comms.root) as db:
            if db is None:
                raise ValueError("Original selected input store is unavailable")
            return tuple(context for selected in NativeRuntimeInput.select(
                db, input_id=row.input_id, session_id=row.session_id,
                session_file=str(files.session), owner_lookup=lookup,
            ) for context in selected.model_contexts_for(
                db, self.comms.bus.bus, lookup, message.reference,
            ))


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
        self.deal(reader, self.owner_words(snapshot), trigger)

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
