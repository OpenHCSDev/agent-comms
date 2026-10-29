"""Display projections depend on wire/read authorities, never the reverse."""

from __future__ import annotations

from abc import ABC, abstractmethod
from bisect import bisect_left, bisect_right
from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, ClassVar, Self

from . import relationships
from .activity import Activity, ActivityData, ActivityState, ObservedActivity
from .activity_clocks import ChannelActivity
from .agent_activity import (
    AgentActivity,
    RecordedRecipientActivity,
)
from .audience_manifest import FrozenRecipient
from .bus import DisplayUnread
from .channel_targets import CommandTarget
from .channels import Channel, ChannelMemberIndex, Channels
from .cli_result import CliResult
from .commander_frame_file import CommanderFrameFile
from .coordination_database import CoordinationStore
from .coordination_errors import DMPaintedBoundChanged, StaleRevision
from .declared_family import DeclaredFamily
from .display_order import ChannelSort, NameChannelSort
from .display_references import CurrentDisplayReferences
from .external_transcript import ExternalTranscript
from .field_codec import (
    Differs,
    FieldCodec,
    JsonShapeFamily,
    JsonShapeMember,
    NullMember,
    PresenceExclude,
    RecordMember,
    SubclassUnion,
    ThreadLookup,
    WatchPaths,
    WatchViews,
    WireExclude,
    WireInline,
    WireOmitDefault,
    projected,
)
from .goal_attempts import GoalAttemptStore
from .goal_presentation import GoalExecution
from .goal_waits import GoalWaits
from .mentions import MentionCandidate
from .message_page import (
    NEWEST_PAGE,
    MessagePage,
    PageBounds,
)
from .message_reference import MessageReference
from .messages import Message
from .notification_progress import (
    ConsumedDelivery, ConsumingProgress, DeliveryProgress, HumanReadProgress, WorkingTakenProgress,
)
from .private_path import FileRevision
from .read_basis import (
    AnonymousViewer,
    CapturedSelection,
    ChannelDisplayScope,
    DisplayViewer,
    NamedViewer,
)
from .read_ledger import NoReadNotice, ReadLedger, ReadNotice
from .notification_assignment import NotificationAssignment
from .notification_evidence import (NotificationAssignmentRead, NotificationReadMark, NotificationWindow,
                                    NotificationSelection, RecipientSelection)
from .notification_read import CapturedNotificationWindow
from .recorded_bus import NoRecordedBus, RecordedBusIdentity
from .registry_document import DEFAULT_ROSTER, RosterVisibility
from .revision_views import (
    ActivityView,
    BusSourceView,
    CommanderFramesView,
    ExternalTranscriptsView,
    NotificationEvidenceView,
    NotificationsView,
    RegistrationsView,
    RevisionView,
    SidebarView,
    StatusSourced,
    StoresView,
)
from .runtime_info import RuntimeInfo
from .storage_room import RoomLedger
from .thread_identity import ThreadIncarnation
from .thread_presentation import (
    ThreadOwnerBinding,
    ThreadPresentation,
    UnavailableThreadOwnerBinding,
)
from .thread_status import ActiveThreadPresence, ThreadStatus, ThreadStatusName
from .threads import Thread
from .tool_result import ToolResult
from .transcript_sources import AssignedTranscriptSource
from .ui_model.unread import ExactUnread, IndexingUnread, UnobservedUnread, UnreadPresentation

if TYPE_CHECKING:
    from .agent_activity import AgentActivity, RecipientActivity
    from .bus import Bus, BusSnapshot
    from .bus_publication import Delivered
    from .catalog_document import CatalogDocument
    from .catalog_store import ChannelCatalog
    from .comms import Comms
    from .goal_management import Goals
    from .history_views import HistoryViews
    from .message_bus import MessageBus
    from .messages import Message
    from .pi_counts import PiCount
    from .read_ledger import ReadLedger
    from .registration import Registration
    from .registry_document import RegistrySnapshot
    from .registry_provenance import DeletedThread, RegistryProvenance
    from .threads import ThreadProvenance
    from .ui_model.observation import ThreadsObserved


class RecipientCursor(SubclassUnion, affix=""):
    """Select frozen, lookup-ordered positions before expensive evidence acquisition."""

    @abstractmethod
    def positions(self, source: Delivered) -> range: ...


@dataclass(frozen=True)
class FirstRecipient(NullMember, RecipientCursor):
    def positions(self, source: Delivered) -> range:
        return range(len(source.recipients))


@dataclass(frozen=True)
class AfterRecipient(RecordMember, RecipientCursor):
    recipient: FrozenRecipient

    def positions(self, source: Delivered) -> range:
        start = bisect_right(source.recipients, self.recipient.recipient_lookup,
                             key=lambda item: item.recipient_lookup)
        return range(start, len(source.recipients))


class OutcomeViewer(ABC):
    """Who reads a delivery window decides which of its outcomes are shown."""

    @abstractmethod
    def positions(self, source: Delivered) -> range: ...

    def selection(self, sources: Sequence[Delivered], *, after: RecipientCursor = FirstRecipient(),
                  limit: int = RecipientSelection.max_limit) -> NotificationSelection:
        RecipientSelection.require_limit(limit)
        pages = []
        for source in sources:
            eligible, following = self.positions(source), after.positions(source)
            pages.append(RecipientSelection(
                source, range(max(eligible.start, following.start), min(eligible.stop, following.stop)), limit))
        return NotificationSelection(self, tuple(pages))

    @abstractmethod
    def sees_direct_read(self, source: Delivered) -> bool:
        pass

    @abstractmethod
    def sees(self, source: Delivered) -> bool:
        """Whether this reader may read the message at all."""


@dataclass(frozen=True, slots=True)
class AllOutcomes(OutcomeViewer):
    """An unscoped reader sees every recipient's outcome and every human read."""

    def positions(self, source: Delivered) -> range:
        return range(len(source.recipients))

    def sees_direct_read(self, source: Delivered) -> bool:
        return True

    def sees(self, source: Delivered) -> bool:
        return True


@dataclass(frozen=True, slots=True)
class OwnerOutcomes(OutcomeViewer):
    """A sender sees all outcomes; a recipient sees only its original outcome."""

    lookup: ThreadLookup

    @classmethod
    def of(cls, owner: ThreadProvenance) -> OwnerOutcomes:
        return cls(owner.incarnation.require_recorded().lookup)

    def positions(self, source: Delivered) -> range:
        if source.sender_lookup == self.lookup:
            return range(len(source.recipients))
        position = bisect_left(source.recipients, self.lookup, key=lambda item: item.recipient_lookup)
        if position < len(source.recipients) and source.recipients[position].recipient_lookup == self.lookup:
            return range(position, position + 1)
        return range(0)

    def sees_direct_read(self, source: Delivered) -> bool:
        return source.sender_lookup == self.lookup

    def sees(self, source: Delivered) -> bool:
        """The message's sender and its original recipients may read it."""
        return self.lookup == source.sender_lookup or bool(self.positions(source))


@dataclass(frozen=True, slots=True)
class MessageNotification:
    """Read-only display of one recipient's durable assignment decision."""

    recipient_identity: FrozenRecipient
    sender_lookup: ThreadLookup
    state: DeliveryProgress
    message: Message | None = None
    displayed_to: tuple[ThreadIncarnation, ...] = ()
    answer: MessageReference | None = None
    answered_at_ms: int | None = None
    changed: bool = False

    @property
    def recipient(self) -> str:
        return self.recipient_identity.canonical_thread

    def activity_names(self) -> tuple[str, ...]:
        return (self.recipient, *(() if self.message is None else (self.message.sender,)))

    @property
    def label(self) -> str:
        return self.state.notification_label(self)

    def sent_outcome(self) -> SentMessageOutcome | None:
        if not self.changed or self.message is None:
            return None
        return SentMessageOutcome(self.recipient_identity, self.sender_lookup, self.state,
                                  message=self.message, answer=self.answer,
                                  answered_at_ms=self.answered_at_ms)

    @classmethod
    def ordered(cls, notifications: Iterable[MessageNotification]) -> tuple[MessageNotification, ...]:
        return tuple(sorted(notifications, key=lambda item: (item.state.rank(), item.recipient.casefold())))

    @classmethod
    def summary(cls, notifications: Iterable[MessageNotification]) -> str:
        """One message's state and totals; clients never classify or count agents."""
        ordered = cls.ordered(notifications)
        if not ordered:
            return "No recorded notification result"
        # This is a bounded observation, not the complete frozen audience and
        # not a set of runtime acknowledgements. Membership proves neither.
        total = len(ordered)
        consumed = sum(notification.state.consumed for notification in ordered)
        read = sum(bool(notification.displayed_to) for notification in ordered)
        counted = ((ConsumingProgress.label, sum(item.state.answering for item in ordered)),
                   (WorkingTakenProgress.label, sum(item.state.seen_working for item in ordered)),
                   (ConsumedDelivery.ignored_label, sum(item.state.seen_not_answering for item in ordered)))
        leading = ordered[0]
        return " · ".join((
            *leading.state.summary_prefix(leading),
            *(f"{label} {count}" for label, count in counted if count),
            f"Observed {total} recipients (bounded)",
            f"{ConsumedDelivery.label} {consumed} observed",
            *((f"{HumanReadProgress.label} {read} observed",) if read else ()),
        ))

    @staticmethod
    def timed_state(label: str, timestamp_ms: int | None) -> str:
        """Format a recorded event in the viewer's local time."""
        if timestamp_ms is None:
            raise ValueError(f"{label} notification requires a recorded time")
        recorded = datetime.fromtimestamp(timestamp_ms / 1000).astimezone()
        now = datetime.now().astimezone()
        when = recorded.strftime("%H:%M")
        if recorded.date() != now.date():
            when = recorded.strftime("%b %d %H:%M").replace(" 0", " ")
        return f"{label} {when}"

    @classmethod
    def references(
        cls, root: Path, registry: Registration, log: Bus,
        references: Sequence[MessageReference], *, viewer: OutcomeViewer,
        after: RecipientCursor = FirstRecipient(), limit: int = RecipientSelection.max_limit,
    ) -> NotificationWindow:
        """Borrow mounted references in the original bounded delivery windows, as this
        viewer may see them."""
        def project(sources: Sequence[Delivered]) -> NotificationWindow:
        cls, root: Path, registry: Registration, assignment_read: NotificationAssignmentRead,
    ) -> tuple[MessageNotification, ...]:
        """Attach notification outcomes to the messages the caller already checked."""
        projected = cls.delivery_window(
            root, registry, assignment_read,
        )
        return tuple(
            replace(notification, message=source.envelope)
            for source in assignment_read.sources
