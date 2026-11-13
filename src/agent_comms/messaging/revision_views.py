"""The views a Comms revision feeds; each revision source declares which of them it feeds."""

from __future__ import annotations

from itertools import zip_longest

from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.storage.private_path import FileObservation, MissingFile


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


class HumanReadEvidenceView(RevisionView):
    """Human display marks, independent of assignment and activity updates."""


class AssignmentEvidenceView(RevisionView):
    """Assignment evidence, independent of human display and activity updates."""


class TeamBoardView(StatusSourced, RevisionView):
    """Store changes requiring board preparation, excluding its own publications and clock."""


class TeamFramesView(TeamBoardView, declared_name="team_frames"):
    """The retained board and the source revisions that require its next preparation."""


class BusSourceView(RevisionView):
    def differs(self, before: tuple[FileObservation, ...], current: tuple[FileObservation, ...]) -> bool:
        return any(
            earlier.lost_in(now)
            for earlier, now in zip_longest(before, current, fillvalue=MissingFile())
        )
