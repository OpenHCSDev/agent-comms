"""What an agent runtime reports about its thread, and where those reports land in Core.

A runtime (an external CLI's transcript, an owned runtime's events) yields RuntimeUpdate records.
ThreadOutput is their sink: it assembles item drafts per (turn, item), coalesces superseded
previews, and publishes finished items, drafts and activity to the thread's transcript.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self

from .activity import ActivityState
from .agent_activity import AgentActivity
from .cli_protocol import EarlierItems
from .external_transcript import ExternalPage, ExternalTranscript
from .recorded_time import EventTime, UnrecordedEventTime
from .transcript_events import LiveTextTranscript, TranscriptEvent

if TYPE_CHECKING:
    from .cli_bridges.shared import TranscriptBridge
    from .importing import ImportBuffer


@dataclass
class DraftAccumulator:
    """One subscription's item prefix; the shared output owns its lifetime."""

    started_at: EventTime

    def restarted(self, replacement: DraftAccumulator) -> DraftAccumulator:
        return replacement

    def started(self, update: StartedItem) -> tuple[RuntimeUpdate, ...]:
        return (
            *self.activity(ActiveItem(update.turn_id, update.item_id, update.state, update.detail)),
            *self.snapshot(update.turn_id, update.item_id),
        )

    def activity(self, update: ActiveItem) -> tuple[RuntimeUpdate, ...]:
        return (update,)

    def snapshot(self, turn_id: str, item_id: str) -> tuple[DraftItem, ...]:
        return ()

    def append(self, text: str, section: int) -> None:
        raise ValueError("Received a text delta for a non-text item")

    def finish(self, update: CompletedItem) -> tuple[ClosedDraft, FinishedItem]:
        """Close on a full item, preserving provider time when no start was observed."""
        events = tuple(self.started_at.stamp(event) for event in update.events)
        return ClosedDraft(self.started_at), FinishedItem(update.turn_id, update.item_id, events)


class NoPrefixDraft(DraftAccumulator):
    """No observed prefix; retain no per-item state so the instance can be shared."""

    def __init__(self) -> None:
        super().__init__(UnrecordedEventTime())

    def append(self, text: str, section: int) -> None:
        """Drop the suffix: without an observed prefix it cannot form a whole item."""


NO_PREFIX = NoPrefixDraft()


@dataclass
class TextDraft(DraftAccumulator):
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

    def append(self, text: str, section: int) -> None:
        self.sections.setdefault(section, []).append(text)

    @property
    def events(self) -> tuple[TranscriptEvent, ...]:
        text = self.separator.join(
            text for index in sorted(self.sections) if (text := "".join(self.sections[index]))
        )
        return (
            (self.started_at.stamp(self.event_type(text, position=self.position)),) if text else ()
        )

    def snapshot(self, turn_id: str, item_id: str) -> tuple[DraftItem, ...]:
        return (DraftItem(turn_id, item_id, self),)


class ParagraphDraft(TextDraft):
    separator = "\n\n"


class ClosedDraft(DraftAccumulator):
    """Completed snapshots reject overlapping older starts and late suffixes."""

    def restarted(self, replacement: DraftAccumulator) -> DraftAccumulator:
        return self

    def activity(self, update: ActiveItem) -> tuple[RuntimeUpdate, ...]:
        return ()

    def append(self, text: str, section: int) -> None:
        pass


@dataclass(frozen=True)
class ThreadOutput:
    """Where a CLI's output lands in Core: finished items and current activity."""

    transcript: ExternalTranscript
    agents: AgentActivity
    drafts: dict[tuple[str, str], DraftAccumulator] = field(default_factory=dict)

    def publish_batch(self, updates: list[RuntimeUpdate]) -> None:
        assembled = [part for update in updates for part in update.accumulate(self)]
        for update in RuntimeUpdate.coalesced(assembled):
            update.publish(self)

    def draft(self, key: tuple[str, str]) -> DraftAccumulator:
        return self.drafts.get(key, NO_PREFIX)

    def start_item(self, update: StartedItem) -> tuple[RuntimeUpdate, ...]:
        key = update.turn_id, update.item_id
        current = self.draft(key).restarted(update.accumulator)
        self.drafts[key] = current
        return current.started(update)

    def active_item(self, update: ActiveItem) -> tuple[RuntimeUpdate, ...]:
        return self.draft((update.turn_id, update.item_id)).activity(update)

    def append_item(self, update: AppendDraftItem) -> tuple[RuntimeUpdate, ...]:
        current = self.draft((update.turn_id, update.item_id))
        current.append(update.text, update.section)
        return current.snapshot(update.turn_id, update.item_id)

    def finish_item(self, update: CompletedItem) -> FinishedItem:
        key = update.turn_id, update.item_id
        self.drafts[key], finished = self.draft(key).finish(update)
        return finished

    def end_turn(self, turn_id: str) -> None:
        for key in tuple(self.drafts):
            if key[0] == turn_id:
                del self.drafts[key]

    def end_all_turns(self) -> None:
        self.drafts.clear()

    def status(self, state: ActivityState, detail: str = "") -> None:
        detail = " ".join(detail.split())
        self.agents.set_activity(self.transcript.incarnation.name, state, detail)


class ReadingPhase(ABC):
    """Where a reader is in its source: catching up on what was already written, or following it."""

    @abstractmethod
    def status(self, update: StatusUpdate) -> tuple[RuntimeUpdate, ...]:
        """Current activity now, or held until the reader has caught up."""


@dataclass(frozen=True)
class RuntimeUpdate(ABC):
    """One change a runtime reports about its thread."""

    def during(self, phase: ReadingPhase) -> tuple[RuntimeUpdate, ...]:
        return (self,)

    @abstractmethod
    def publish(self, output: ThreadOutput) -> None: ...

    def accumulate(self, output: ThreadOutput) -> tuple[RuntimeUpdate, ...]:
        return (self,)

    async def observed(self, bridge: TranscriptBridge) -> None:  # noqa: B027 - default hook
        """A source-owned notification may confirm delivery before publication."""

    def retain_before(self, later: set[tuple[str, str]]) -> bool:
        """Whether this update still needs publication before later updates."""
        return True

    def page_record(self, turn_id: str, item_id: str) -> FinishedItem:
        """Even an invisible item owns a source position and its page continuation."""
        return FinishedItem(turn_id, item_id, ())

    def add_to(self, history: ImportBuffer) -> None:  # noqa: B027 - default hook
        """What this update adds to an imported conversation; activity adds nothing."""

    @staticmethod
    def coalesced(updates: list[RuntimeUpdate]) -> list[RuntimeUpdate]:
        later: set[tuple[str, str]] = set()
        kept = [update for update in reversed(updates) if update.retain_before(later)]
        return list(reversed(kept))


class StatusUpdate(RuntimeUpdate):
    """Reader phases decide when current activity is published."""

    def during(self, phase: ReadingPhase) -> tuple[RuntimeUpdate, ...]:
        return phase.status(self)


@dataclass(frozen=True)
class TurnStarted(StatusUpdate):
    detail: str = ""

    def publish(self, output: ThreadOutput) -> None:
        output.status(ActivityState.WORKING, self.detail)


@dataclass(frozen=True)
class TurnEnded(StatusUpdate):
    def publish(self, output: ThreadOutput) -> None:
        output.transcript.end_turn()
        output.status(ActivityState.IDLE)


@dataclass(frozen=True)
class ActiveItem(StatusUpdate):
    """An item the CLI started producing: what it is doing now."""

    turn_id: str
    item_id: str
    state: ActivityState
    detail: str

    def publish(self, output: ThreadOutput) -> None:
        output.status(self.state, self.detail)

    def accumulate(self, output: ThreadOutput) -> tuple[RuntimeUpdate, ...]:
        return output.active_item(self)


@dataclass(frozen=True)
class FinishedItem(RuntimeUpdate):
    """One CLI item's complete presentation, identified by its producer."""

    turn_id: str
    item_id: str
    events: tuple[TranscriptEvent, ...]

    def publish(self, output: ThreadOutput) -> None:
        output.transcript.finish(turn_id=self.turn_id, item_id=self.item_id, events=self.events)

    def retain_before(self, later: set[tuple[str, str]]) -> bool:
        later.add((self.turn_id, self.item_id))
        return True

    def page_record(self, turn_id: str, item_id: str) -> FinishedItem:
        return self

    def add_to(self, history: ImportBuffer) -> None:
        history.add(self.events)


@dataclass(frozen=True)
class PageItem:
    """An item-list identity survives even when its update is invisible in the live view."""

    turn_id: str
    item_id: str
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
    title: str = ""

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
class DraftItem(RuntimeUpdate):
    """A pending view; build its text only if this preview survives coalescing."""

    turn_id: str
    item_id: str
    accumulator: TextDraft

    @property
    def events(self) -> tuple[TranscriptEvent, ...]:
        return self.accumulator.events

    def publish(self, output: ThreadOutput) -> None:
        output.transcript.draft(turn_id=self.turn_id, item_id=self.item_id, events=self.events)

    def retain_before(self, later: set[tuple[str, str]]) -> bool:
        key = self.turn_id, self.item_id
        keep = key not in later
        later.add(key)
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
class AppendDraftItem(DraftUpdate):
    turn_id: str
    item_id: str
    text: str
    section: int = 0

    def accumulate(self, output: ThreadOutput) -> tuple[RuntimeUpdate, ...]:
        return output.append_item(self)


@dataclass(frozen=True)
class FinishedTurn(DraftUpdate, TurnEnded):
    turn_id: str

    def accumulate(self, output: ThreadOutput) -> tuple[RuntimeUpdate, ...]:
        output.end_turn(self.turn_id)
        return (TurnEnded(),)


@dataclass(frozen=True)
class AllTurnsEnded(DraftUpdate, TurnEnded):
    def accumulate(self, output: ThreadOutput) -> tuple[RuntimeUpdate, ...]:
        output.end_all_turns()
        return (TurnEnded(),)
