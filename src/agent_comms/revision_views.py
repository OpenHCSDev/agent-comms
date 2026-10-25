"""The views a Comms revision feeds; each revision source declares which of them it feeds."""

from __future__ import annotations

from itertools import zip_longest

from .declared_family import DeclaredFamily
from .private_path import FileObservation, MissingFile


class StatusSourced:
    """A view fed by every thread-status source."""

    @classmethod
    def views(cls) -> tuple[type[RevisionView], ...]:
        return RevisionView.members_with(cls)


class RevisionView(DeclaredFamily, affix="View"):
    """A view derived from the revision sources that declare they feed it."""

    def differs(self, before: tuple[FileObservation, ...], current: tuple[FileObservation, ...]) -> bool:
        return before != current


class RegistrationsView(RevisionView):
    pass


class StoresView(StatusSourced, RevisionView):
    """Every observed store; the activity expiry tick is not a store."""


class ActivityView(RevisionView):
    """Participant activity, including ownership, work and timed expiry."""


class StatusView(StatusSourced, ActivityView):
    """Activity, assignments and bus reads supply thread status.

    Draft and finished CLI items share one store. Neither changes the
    sidebar's roster, activity or unread bus counts. Their own observer
    still receives every revision and refreshes the conversation.
    """


class SidebarView(StatusSourced, ActivityView):
    """Native unread progress updates counts without rebuilding thread presentations."""


class ExternalTranscriptsView(RevisionView):
    """CLI drafts and finished items, independently of coordination status."""


class NotificationsView(ActivityView):
    """Whole-window changes; activity is attributed to its message participants."""


class NotificationEvidenceView(RevisionView):
    """Assignment and human read evidence, independent of timed status refreshes."""


class CommanderFramesView(RevisionView):
    """The heartbeat's newest frame per commander; nothing else feeds the commander page."""


class BusSourceView(RevisionView):
    def differs(self, before: tuple[FileObservation, ...], current: tuple[FileObservation, ...]) -> bool:
        return any(
            earlier.lost_in(now)
            for earlier, now in zip_longest(before, current, fillvalue=MissingFile())
        )

