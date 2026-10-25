"""Only a heartbeat line the commander can dispose of locks its writing verbs."""

from agent_comms.heartbeat_subjects import UnitSubject
from agent_comms.build_identity import InstalledCommitInstallation, MissingCommitInstallation, RecordedBuildIdentity
from agent_comms.task_attachments.commander_pings import RulePing, UnitPing
from agent_comms.order_runs import ApplicationOrderRun, MechanicalOrderRun
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.text_digest import TextDigest
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.ui_model.unit_state import AwaitingApplicationRun, DeliveredUnclosed, UnitState


RUN = "The real application shows the merged fix working after a restart."


UNIT = RecordedThreadIncarnation("unit", 1.0)


def unit_line(state: type[UnitState], since: float) -> UnitPing:
    return UnitPing(UNIT, state, (), (), TextDigest.of(state.verb), text=state.verb,
                    subjects=(UnitSubject(UNIT),), since=RecordedEventTime(since))


def test_only_commander_disposable_stale_lines_are_undisposed(comms):
    """2026-10-25: AWAIT RUN lines, the holder's to dispose of, were restated as UNDISPOSED and
    printed on every verb refusal beside the lines that actually locked."""
    cut, actors = 1000.0, frozenset({"lead"})
    judge = RulePing(TextDigest.of("JUDGE"), text="JUDGE", subjects=(UnitSubject(UNIT),),
                     since=RecordedEventTime(10.0))
    assert unit_line(DeliveredUnclosed, 10.0).undisposed(comms, actors, cut)
    assert judge.undisposed(comms, actors, cut)
    assert not unit_line(DeliveredUnclosed, 2000.0).undisposed(comms, actors, cut)
    assert not unit_line(AwaitingApplicationRun, 10.0).undisposed(comms, actors, cut)


def test_an_installed_application_order_waits_on_its_holder_not_the_commander():
    installed, missing = InstalledCommitInstallation(), MissingCommitInstallation("a" * 40, RecordedBuildIdentity(()))
    assert installed.awaits_holder(ApplicationOrderRun(RUN))
    assert not installed.awaits_holder(MechanicalOrderRun())
    assert not missing.awaits_holder(ApplicationOrderRun(RUN))
