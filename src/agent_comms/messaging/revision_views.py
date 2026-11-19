"""The views a Comms revision feeds; each revision source declares which of them it feeds."""

from __future__ import annotations

from itertools import zip_longest

from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.storage.file_observation import FileObservation, MissingFile


class RevisionView(DeclaredFamily, affix="View"):
    """A view derived from the revision sources that declare they feed it."""

    def differs(self, before: tuple[FileObservation, ...], current: tuple[FileObservation, ...]) -> bool:
        return before != current


class NotificationEvidenceView(RevisionView):
    """A projection whose answer depends on notification evidence."""


class StatusSourceView(NotificationEvidenceView):
    """A projection fed by every source of thread status."""


class RegistrationsView(RevisionView):
    pass


class StoresView(StatusSourceView):
    """Every observed store; the activity expiry tick is not a store."""


class ActivityView(RevisionView):
    """Participant activity, including ownership, work and timed expiry."""


class RosterView(ActivityView):
    """Thread rows: registration, activity, runtime information and goal execution."""


class StatusView(StatusSourceView, ActivityView):
    """Activity, assignments and bus reads supply thread status.

    Draft and finished CLI items share one store. Neither changes the
    sidebar's roster, activity or unread bus counts. Their own observer
    still receives every revision and refreshes the conversation.
    """


class SidebarBasisView(RevisionView):
    """The stores one sidebar cut counts over: the catalog, the bus, read marks and native
    unread. The registry is read too, but a cut is kept across a registry commit that moved only
    turns and presence (RegistrySnapshot.moved_turns_alone), which a store revision cannot say."""


class SidebarView(RosterView, SidebarBasisView):
    """Every store the sidebar reads: its cut, then activity, goals and relationships for its
    rows. Coordination and CLI transcripts feed no sidebar row."""


class ExternalTranscriptsView(RevisionView):
    """CLI drafts and finished items, independently of coordination status."""


class NotificationsView(NotificationEvidenceView, ActivityView):
    """Whole-window changes; activity is attributed to its message participants."""


class HumanReadEvidenceView(RevisionView):
    """Human display marks, independent of assignment and activity updates."""


class AssignmentEvidenceView(NotificationEvidenceView):
    """Assignment evidence, independent of human display and activity updates."""


class TeamBoardView(StatusSourceView):
    """Store changes requiring board preparation, excluding its own publications and clock."""


class RetainedTeamFramesView(RevisionView):
    """Only the retained frames, plan and their service-health source."""


class TeamFramesView(TeamBoardView, RetainedTeamFramesView, declared_name="team_frames"):
    """The retained board and the source revisions that require its next preparation."""


class BusSourceView(RevisionView):
    def differs(self, before: tuple[FileObservation, ...], current: tuple[FileObservation, ...]) -> bool:
        return any(
            earlier.lost_in(now)
            for earlier, now in zip_longest(before, current, fillvalue=MissingFile())
        )
