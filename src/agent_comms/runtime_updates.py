"""What an agent runtime reports about its thread, and where those reports land in Core.

A runtime (an external CLI's transcript, an owned runtime's events) yields RuntimeUpdate records.
ThreadOutput is their sink: it assembles item drafts per (turn, item), coalesces superseded
previews, and publishes finished items, drafts and activity to the thread's transcript.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, ClassVar, Self

from . import activity
from .agent_activity import AgentActivity
from .cli_protocol import EarlierItems
from .external_transcript import ExternalPage, ExternalTranscript
from .recorded_time import EventTime, UnrecordedEventTime
from .transcript_events import (
    LiveTextTranscript,
    ToolStartTranscript,
    TranscriptEvent,
)
from .thread_text import NoThreadText, ThreadText
from .thread_identity import OfferedTools
from .turn_phase import IdlePhase, ModelWaitPhase, TurnPhase
from .turn_item import NativeTurnId, TurnItem

if TYPE_CHECKING:
    from .cli_bridges.shared import TranscriptBridge
    from .importing import ImportBuffer
    from .comms import Comms
    from .messaging import Messaging


@dataclass
class DraftAccumulator:
    """One subscription's item prefix; the shared output owns its lifetime."""

    started_at: EventTime

    def restarted(self, replacement: DraftAccumulator) -> DraftAccumulator:
        return replacement

    def started(self, update: StartedItem) -> tuple[RuntimeUpdate, ...]:
        return (
            *self.activity(ActiveItem(update.turn_id, update.item_id, update.phase)),
            *self.snapshot(update.turn_id, update.item_id),
        )

    def activity(self, update: ActiveItem) -> tuple[RuntimeUpdate, ...]:
        return (update,)

    def snapshot(self, turn_id: NativeTurnId, item_id: str) -> tuple[DraftItem, ...]:
        return ()

    def append(self, update: AppendDraftItem) -> tuple[RuntimeUpdate, ...]:
        raise ValueError("Received a text delta for a non-text item")

    def finish(self, update: CompletedItem) -> tuple[ClosedDraft, FinishedItem]:
        """Close with the completed record's own already decoded event times."""
        return ClosedDraft(self.started_at), FinishedItem(update.turn_id, update.item_id, update.events)


class NoPrefixDraft(DraftAccumulator):
    """No observed prefix; retain no per-item state so the instance can be shared."""

    def __init__(self) -> None:
        super().__init__(UnrecordedEventTime())

    def append(self, update: AppendDraftItem) -> tuple[RuntimeUpdate, ...]:
        """Drop the suffix: without an observed prefix it cannot form a whole item."""
        return ()


NO_PREFIX = NoPrefixDraft()


class PresentableDraft(DraftAccumulator, ABC):
    """A recorded prefix that can be projected as a live transcript row."""

    @property
    @abstractmethod
    def events(self) -> tuple[TranscriptEvent, ...]: ...

    def snapshot(self, turn_id: NativeTurnId, item_id: str) -> tuple[DraftItem, ...]:
        return (DraftItem(turn_id, item_id, self),)


@dataclass
class ToolDraft(PresentableDraft):
    prefix: tuple[ToolStartTranscript, ...]

    @property
    def events(self) -> tuple[TranscriptEvent, ...]:
        return self.prefix

    def snapshot(self, turn_id: NativeTurnId, item_id: str) -> tuple[DraftItem, ...]:
        return super().snapshot(turn_id, item_id) if self.prefix else ()


@dataclass
class TextDraft(PresentableDraft):
    event_type: type[LiveTextTranscript]
    sections: dict[int, list[str]]
    position: int | str
    separator: ClassVar[str] = ""

    @classmethod
    def from_parts(
        cls,
        event_type: type[LiveTextTranscript],
        parts: Iterable[str],
        started_at: EventTime,
        position: int | str,
    ) -> Self:
        return cls(
            started_at, event_type, {index: [text] for index, text in enumerate(parts)}, position
        )

    def append(self, update: AppendDraftItem) -> tuple[RuntimeUpdate, ...]:
        self.sections.setdefault(update.section, []).append(update.text)
        return self.snapshot(update.turn_id, update.item_id)

    @property
    def events(self) -> tuple[TranscriptEvent, ...]:
        text = self.separator.join(
            text for index in sorted(self.sections) if (text := "".join(self.sections[index]))
        )
        return (
            (self.event_type(text, position=self.position,
                             timestamp=self.started_at.recorded_at("A transcript event")),) if text else ()
        )


class ParagraphDraft(TextDraft):
    separator = "\n\n"


class ClosedDraft(DraftAccumulator):
    """Completed snapshots reject overlapping older starts and late suffixes."""

    def restarted(self, replacement: DraftAccumulator) -> DraftAccumulator:
        return self

    def started(self, update: StartedItem) -> tuple[RuntimeUpdate, ...]:
        return ()

    def activity(self, update: ActiveItem) -> tuple[RuntimeUpdate, ...]:
        return ()

    def append(self, update: AppendDraftItem) -> tuple[RuntimeUpdate, ...]:
        return ()


@dataclass(frozen=True)
class ThreadOutput(activity.ThreadOutput):
    """Where a CLI's output lands in Core: finished items and current activity."""

    transcript: ExternalTranscript
    agents: AgentActivity
    drafts: dict[TurnItem, DraftAccumulator] = field(default_factory=dict)

    @classmethod
    def for_comms(cls, transcript: ExternalTranscript, comms: Comms) -> Self:
        return cls(transcript, comms.agents)

    def delivered_targets(self, targets: tuple[str, ...]) -> None:
        """Ordinary transcript outputs do not publish session replies."""
        return None

    def publish_item(self, item: FinishedItem) -> None:
        self.transcript.finish(turn_id=item.turn_id, item_id=item.item_id, events=item.events)

    def publish_draft(self, item: DraftItem) -> None:
        self.transcript.draft(turn_id=item.turn_id, item_id=item.item_id, events=item.events)

    def publish_pending(self, pending: list[RuntimeUpdate]) -> None:
        for update in RuntimeUpdate.coalesced(pending):
            update.publish(self)
        pending.clear()

    def publish_batch(self, updates: list[RuntimeUpdate]) -> None:
        pending: list[RuntimeUpdate] = []
        for update in updates:
            update.accumulate_into(self, pending)
        self.publish_pending(pending)

    def draft(self, key: TurnItem) -> DraftAccumulator:
        return self.drafts.get(key, NO_PREFIX)

    def start_item(self, update: StartedItem) -> tuple[RuntimeUpdate, ...]:
        current = self.draft(update.key).restarted(update.accumulator)
        self.drafts[update.key] = current
        return current.started(update)

    def active_item(self, update: ActiveItem) -> tuple[RuntimeUpdate, ...]:
        return self.draft(update.key).activity(update)

    def append_item(self, update: AppendDraftItem) -> tuple[RuntimeUpdate, ...]:
        current = self.draft(update.key)
        return current.append(update)

    def finish_item(self, update: CompletedItem) -> FinishedItem:
        self.drafts[update.key], finished = self.draft(update.key).finish(update)
        return finished

    def end_turn(self, turn_id: NativeTurnId) -> None:
        for key in tuple(self.drafts):
            if key.turn_id == turn_id:
                del self.drafts[key]

    def end_all_turns(self) -> None:
        self.drafts.clear()

    @property
    def activity_thread(self) -> str:
        return self.transcript.incarnation.name


@dataclass(frozen=True)
class ReplyingThreadOutput(ThreadOutput):
    """The last finished assistant item replies to this turn's confirmed deliveries."""

    messaging: Messaging = field(kw_only=True)
    reply_targets: set[str] = field(default_factory=set)
    pending: dict[TurnItem, FinishedItem] = field(default_factory=dict)

    @classmethod
    def for_comms(cls, transcript: ExternalTranscript, comms: Comms) -> Self:
        return cls(transcript, comms.agents, messaging=comms.messaging)

    def delivered_targets(self, targets: tuple[str, ...]) -> None:
        self.reply_targets.update(targets)

    def flush_before(self, key: TurnItem) -> None:
        for previous in tuple(self.pending):
            if previous != key:
                super().publish_item(self.pending.pop(previous))

    def publish_item(self, item: FinishedItem) -> None:
        self.flush_before(item.key)
        if self.reply_targets and item.assistant_text:
            self.pending[item.key] = item
            self.transcript.draft(turn_id=item.turn_id, item_id=item.item_id, events=item.events)
        else:
            super().publish_item(item)

    def publish_draft(self, item: DraftItem) -> None:
        self.flush_before(item.key)
        super().publish_draft(item)

    def end_turn(self, turn_id: NativeTurnId) -> None:
        for key in tuple(self.pending):
            if key.turn_id != turn_id:
                continue
            item = self.pending[key]
            publications = tuple(
                self.messaging.send(
                    sender=self.activity_thread, target=target, body=item.assistant_text,
                ).message.reference
                for target in sorted(self.reply_targets)
            )
            super().publish_item(replace(item, events=tuple(
                event.with_publications(publications) for event in item.events
            )))
            del self.pending[key]
        self.reply_targets.clear()
        super().end_turn(turn_id)

    def end_all_turns(self) -> None:
        for turn_id in dict.fromkeys(key.turn_id for key in self.pending):
            self.end_turn(turn_id)
        self.reply_targets.clear()
        super().end_all_turns()


class ReadingPhase(ABC):
    """Where a reader is in its source: catching up on what was already written, or following it."""

    @abstractmethod
    def status(self, update: StatusUpdate) -> tuple[RuntimeUpdate, ...]:
        """Current activity now, or held until the reader has caught up."""

    def offered(self, update: OfferedToolsObserved) -> tuple[RuntimeUpdate, ...]:
        return (update,)


@dataclass(frozen=True)
class RuntimeUpdate(ABC):
    """One change a runtime reports about its thread."""

    def during(self, phase: ReadingPhase) -> tuple[RuntimeUpdate, ...]:
        return (self,)

    @abstractmethod
    def publish(self, output: ThreadOutput) -> None: ...

    def accumulate(self, output: ThreadOutput) -> tuple[RuntimeUpdate, ...]:
        return (self,)

    def accumulate_into(self, output: ThreadOutput, pending: list[RuntimeUpdate]) -> None:
        pending.extend(self.accumulate(output))

    async def observed(self, bridge: TranscriptBridge) -> RuntimeUpdate:
        """Carry source-owned confirmation into the ordered publication stream."""
        return self

    def retain_before(self, later: set[TurnItem]) -> bool:
        """Whether this update still needs publication before later updates."""
        return True

    def page_record(self, turn_id: NativeTurnId, item_id: str) -> FinishedItem:
        """Even an invisible item owns a source position and its page continuation."""
        return FinishedItem(turn_id, item_id, ())

    def add_to(self, history: ImportBuffer) -> None:  # noqa: B027 - default hook
        """What this update adds to an imported conversation; activity adds nothing."""

    @staticmethod
    def coalesced(updates: list[RuntimeUpdate]) -> list[RuntimeUpdate]:
        later: set[TurnItem] = set()
        kept = [update for update in reversed(updates) if update.retain_before(later)]
        return list(reversed(kept))


class StatusUpdate(RuntimeUpdate):
    """Reader phases decide when current activity is published."""

    def during(self, phase: ReadingPhase) -> tuple[RuntimeUpdate, ...]:
        return phase.status(self)


@dataclass(frozen=True)
class OfferedToolsObserved(RuntimeUpdate):
    offered: OfferedTools

    def during(self, phase: ReadingPhase) -> tuple[RuntimeUpdate, ...]:
        return phase.offered(self)

    def publish(self, output: ThreadOutput) -> None:
        output.agents.registry.observe_offered_tools(output.transcript.incarnation, self.offered)


@dataclass(frozen=True)
class TurnStarted(StatusUpdate):
    detail: str = ""

    def publish(self, output: ThreadOutput) -> None:
        output.enter(ModelWaitPhase(self.detail))


@dataclass(frozen=True)
class TurnEnded(StatusUpdate):
    def accumulate_into(self, output: ThreadOutput, pending: list[RuntimeUpdate]) -> None:
        output.publish_pending(pending)
        self.publish(output)

    def retire(self, output: ThreadOutput) -> None:
        """An idle status snapshot retires nothing: saved completions refuse their replay."""

    def publish(self, output: ThreadOutput) -> None:
        self.retire(output)
        output.transcript.end_turn()
        output.enter(IdlePhase())


@dataclass(frozen=True)
class ActiveItem(TurnItem, StatusUpdate):
    """An item the CLI started producing: what it is doing now."""

    phase: TurnPhase

    def publish(self, output: ThreadOutput) -> None:
        output.enter(self.phase)

    def accumulate(self, output: ThreadOutput) -> tuple[RuntimeUpdate, ...]:
        return output.active_item(self)


@dataclass(frozen=True)
class FinishedItem(TurnItem, RuntimeUpdate):
    """One CLI item's complete presentation, identified by its producer."""

    events: tuple[TranscriptEvent, ...]

    @property
    def assistant_text(self) -> str:
        return "".join(part for event in self.events for part in event.assistant_parts())

    def publish(self, output: ThreadOutput) -> None:
        output.publish_item(self)

    def retain_before(self, later: set[TurnItem]) -> bool:
        later.add(self.key)
        return True

    def page_record(self, turn_id: NativeTurnId, item_id: str) -> FinishedItem:
        return self

    def add_to(self, history: ImportBuffer) -> None:
        history.add(self.events)


@dataclass(frozen=True)
class PageItem(TurnItem):
    """An item-list identity survives even when its update is invisible in the live view."""

    update: RuntimeUpdate


@dataclass(frozen=True)
class PublishedPage(RuntimeUpdate):
    """A bounded harness snapshot publishes items, never current activity or drafts."""

    items: tuple[PageItem, ...]
    earlier: ExternalPage

    def publish(self, output: ThreadOutput) -> None:
        records = tuple(item.update.page_record(item.turn_id, item.item_id) for item in self.items)
        self.write(output.transcript, records)

    def write(self, transcript: ExternalTranscript, records: tuple[FinishedItem, ...]) -> None:
        transcript.publish_page(records, self.earlier)

    def add_to(self, history: ImportBuffer) -> None:
        for item in self.items:
            item.update.add_to(history)


@dataclass(frozen=True)
class EarlierPublishedPage(PublishedPage):
    request: EarlierItems

    def write(self, transcript: ExternalTranscript, records: tuple[FinishedItem, ...]) -> None:
        transcript.prepend(self.request, records, self.earlier)


@dataclass(frozen=True)
class HiddenItem(RuntimeUpdate):
    """Conversation the live view does not show, because the thread shows it another way
    (a bus delivery) or because only the person's own prompts are shown; history keeps it."""

    events: tuple[TranscriptEvent, ...]

    def publish(self, output: ThreadOutput) -> None:
        pass

    def add_to(self, history: ImportBuffer) -> None:
        history.add(self.events)


@dataclass(frozen=True)
class Summarized(RuntimeUpdate):
    """The harness replaced everything before this point with a summary (compaction)."""

    text: str

    def publish(self, output: ThreadOutput) -> None:
        pass

    def add_to(self, history: ImportBuffer) -> None:
        history.summarize(self.text)


@dataclass(frozen=True)
class SessionFacts(RuntimeUpdate):
    """Which session this is, where it ran and its title; a later fact replaces an earlier one."""

    source_id: str = ""
    project: str = ""
    title: ThreadText = NoThreadText()

    def publish(self, output: ThreadOutput) -> None:
        pass

    def add_to(self, history: ImportBuffer) -> None:
        history.describe(self)


@dataclass(frozen=True)
class EarlierRequest(RuntimeUpdate):
    """The newest request before the selected history, looked up only if that history has none."""

    find: Callable[[], str] = field(compare=False)

    def publish(self, output: ThreadOutput) -> None:
        pass

    def add_to(self, history: ImportBuffer) -> None:
        if not history.latest_request:
            history.latest_request = self.find()[: history.limits.per_message]


@dataclass(frozen=True)
class HistoryNotice(RuntimeUpdate):
    """Something about the source that an imported conversation should say it left out."""

    text: str

    def publish(self, output: ThreadOutput) -> None:
        pass

    def add_to(self, history: ImportBuffer) -> None:
        history.notices.add(self.text)


@dataclass(frozen=True)
class DraftItem(TurnItem, RuntimeUpdate):
    """A pending item's events, materialized only if this preview survives coalescing."""

    accumulator: PresentableDraft

    @property
    def events(self) -> tuple[TranscriptEvent, ...]:
        return self.accumulator.events

    def publish(self, output: ThreadOutput) -> None:
        output.publish_draft(self)

    def retain_before(self, later: set[TurnItem]) -> bool:
        keep = self.key not in later
        later.add(self.key)
        return keep


class DraftUpdate(RuntimeUpdate):
    """Part decoding enters the same accumulation path even for direct publication."""

    @abstractmethod
    def accumulate(self, output: ThreadOutput) -> tuple[RuntimeUpdate, ...]: ...

    def publish(self, output: ThreadOutput) -> None:
        output.publish_batch([self])


@dataclass(frozen=True)
class CompletedItem(DraftUpdate, FinishedItem):
    def accumulate(self, output: ThreadOutput) -> tuple[RuntimeUpdate, ...]:
        return (output.finish_item(self),)


@dataclass(frozen=True)
class StartedItem(DraftUpdate, ActiveItem):
    accumulator: DraftAccumulator

    def accumulate(self, output: ThreadOutput) -> tuple[RuntimeUpdate, ...]:
        return output.start_item(self)


@dataclass(frozen=True)
class AppendDraftItem(TurnItem, DraftUpdate):
    text: str
    section: int = 0

    def accumulate(self, output: ThreadOutput) -> tuple[RuntimeUpdate, ...]:
        return output.append_item(self)


@dataclass(frozen=True)
class FinishedTurn(TurnEnded):
    turn_id: NativeTurnId

    def retire(self, output: ThreadOutput) -> None:
        output.end_turn(self.turn_id)


@dataclass(frozen=True)
class AllTurnsEnded(TurnEnded):
    """The source ended every open turn; publication owns their retirement."""

    def retire(self, output: ThreadOutput) -> None:
        output.end_all_turns()
