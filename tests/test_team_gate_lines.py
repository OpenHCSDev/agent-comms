"""Only a heartbeat line the team can dispose of locks its writing verbs."""

from agent_comms.heartbeat_subjects import TeamHistory, TeammateSubject
from agent_comms.installer.build_identity import (
    InstalledCommitInstallation,
    MissingCommitInstallation,
    RecordedBuildIdentity,
)
from agent_comms.nominal.text_digest import TextDigest
from agent_comms.planning.order_runs import ApplicationOrderRun, MechanicalOrderRun
from agent_comms.registry.thread_identity import RecordedThreadIncarnation
from agent_comms.storage.recorded_time import RecordedEventTime
from agent_comms.task_attachments.team_pings import RulePing, TeammatePing
from agent_comms.team.standing_rules import OrderlessUnreviewedHead
from agent_comms.ui_model.teammate_state import (
    AwaitingApplicationRun,
    DeliveredUnclosed,
    TeammateState,
)

RUN = "The real application shows the merged fix working after a restart."


TEAMMATE = RecordedThreadIncarnation("teammate", 1.0)


def teammate_line(state: type[TeammateState], since: float) -> TeammatePing:
    return TeammatePing(TEAMMATE, state, (), (), TextDigest.of(state.question), text=state.question,
                    subjects=(TeammateSubject(TEAMMATE),), since=RecordedEventTime(since))


def test_only_team_disposable_stale_lines_lock_the_verbs(comms):
    """2026-10-25: AWAIT RUN lines, the holder's to dispose of, were restated as UNDISPOSED and
    printed on every verb refusal beside the lines that actually locked. A line locks when it
    is open (on_board) and has waited on the team past the cut (TeamUpdate, team_pings.py)."""
    cut = 1000.0
    history = TeamHistory.read(comms, frozenset())
    judge = RulePing(OrderlessUnreviewedHead, TextDigest.of("JUDGE"), text="JUDGE", subjects=(TeammateSubject(TEAMMATE),),
                     since=RecordedEventTime(10.0))
    stale = teammate_line(DeliveredUnclosed, 10.0)
    assert stale.on_board(history) and stale.waits_on_team(cut)
    assert judge.on_board(history) and judge.waits_on_team(cut)
    assert not teammate_line(DeliveredUnclosed, 2000.0).waits_on_team(cut)
    awaiting_run = teammate_line(AwaitingApplicationRun, 10.0)
    assert not awaiting_run.on_board(history) and not awaiting_run.waits_on_team(cut)


def test_an_installed_application_order_waits_on_its_holder_not_the_team():
    installed, missing = InstalledCommitInstallation(), MissingCommitInstallation("a" * 40, RecordedBuildIdentity(()))
    assert installed.awaits_holder(ApplicationOrderRun(RUN))
    assert not installed.awaits_holder(MechanicalOrderRun())
    assert not missing.awaits_holder(ApplicationOrderRun(RUN))
