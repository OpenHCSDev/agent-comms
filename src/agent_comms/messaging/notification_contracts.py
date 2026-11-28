"""Notification callbacks use the original evidence/revision providers."""
from __future__ import annotations
from abc import ABC, abstractmethod
from pathlib import Path
from agent_comms.messaging.revision_views import RevisionView


class NotificationEvidenceSource[Evidence](ABC):
    __slots__ = ()
    root: Path

    @abstractmethod
    def notification_evidence(self) -> Evidence: ...


class TurnStartCheck(ABC):
    """Whether a live recipient turn is actively working on an input acknowledged at ``updated_at_ms``."""

    __slots__ = ()

    @abstractmethod
    def turn_started_by(self, updated_at_ms: int) -> bool: ...


class RecipientActivityRead[Notice, Assignment, Progress](TurnStartCheck):
    __slots__ = ()

    @abstractmethod
    def displayed_recipient(self, notification, source, reads, document, snapshot): ...

    @abstractmethod
    def selected_response(self, notification: Notice, updated_at_ms: int, progress: Assignment) -> Notice: ...

    @abstractmethod
    def taken_response(self, notification: Notice, progress: Progress) -> Notice: ...

    @abstractmethod
    def shows_notification(self, notification: Notice, *, displayed: bool) -> bool: ...


class RevisionChanges(ABC):
    __slots__ = ()

    @abstractmethod
    def changed_since(self, view: RevisionView, earlier: RevisionChanges) -> bool: ...
