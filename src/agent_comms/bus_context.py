"""Nominal bus context contracts inherited by Comms and its view/thread owners.

Client-imported modules declare the bus capabilities they accept through these
ABCs instead of importing the concrete Comms composition root, HistoryViews, or
ThreadManagement.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from pathlib import Path

from agent_comms.messaging.message_bus import MessageBus
from agent_comms.messaging.message_notification import (
    CapturedNotificationEvidence,
    NotificationWindow,
)
from agent_comms.messaging.message_reference import MessageReference
from agent_comms.messaging.notification_contracts import NotificationEvidenceSource
from agent_comms.messaging.notification_evidence import (
    FirstRecipient,
    OutcomeViewer,
    RecipientCursor,
    RecipientLimit,
)
from agent_comms.messaging.sending import Messaging
from agent_comms.planning.goal_management import Goals
from agent_comms.presentation import ChannelView, WireRevision
from agent_comms.registry.agent_activity import AgentActivity
from agent_comms.registry.activity import ObservedActivity
from agent_comms.registry.registration import Registration, RegistryBus
from agent_comms.registry.registry_names import DeletedThread
from agent_comms.registry.relationship_catalog_source import RelationshipCatalogSource
from agent_comms.registry.thread_presentation import ThreadPresentation
from agent_comms.transcript.transcripts import Transcripts


class BusRoot(ABC):
    """A bus attachment exposing its root path."""

    __slots__ = ()
    root: Path


class ThreadPresentationBatch(ABC):
    """One captured batch of thread presentations, implemented by ThreadPresentations."""

    __slots__ = ()

    @abstractmethod
    def view(self, name: str) -> ThreadPresentation: ...

    @abstractmethod
    def require(self, name: str) -> ThreadPresentation: ...


class BusViews(ABC):
    """Read-side view queries, implemented by HistoryViews."""

    __slots__ = ()

    @abstractmethod
    def observed_activity(
        self, revision: WireRevision, names: Iterable[str],
    ) -> dict[str, ObservedActivity | DeletedThread]: ...

    @abstractmethod
    def message_notifications_for_references(
        self,
        viewer: OutcomeViewer,
        references: Sequence[MessageReference],
        *,
        after: RecipientCursor = FirstRecipient(),
        limit: RecipientLimit = RecipientLimit(),
    ) -> NotificationWindow: ...

    @abstractmethod
    def thread_presentations(
        self, names: Iterable[str], *, sent_source_limit: int = 5,
    ) -> ThreadPresentationBatch: ...

    @abstractmethod
    def channel_views(self) -> tuple[ChannelView, ...]: ...


class BusThreads(ABC):
    """Thread lifecycle operations needed by caller retirement, implemented by ThreadManagement."""

    __slots__ = ()

    @abstractmethod
    def archive(self, name: str) -> None: ...


class BusContext(
    RegistryBus,
    BusRoot,
    NotificationEvidenceSource[CapturedNotificationEvidence],
):
    """Shared bus capability contract inherited by Comms."""

    __slots__ = ()

    agents: AgentActivity
    bus: MessageBus
    goals: Goals
    messaging: Messaging
    transcripts: Transcripts
    views: BusViews
    threads: BusThreads
    channels: RelationshipCatalogSource
