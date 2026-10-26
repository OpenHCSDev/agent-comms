"""Only a still-true heartbeat line the commander can dispose of locks its verbs."""

from dataclasses import replace

from agent_comms.heartbeat_subjects import CommanderHistory, UnitSubject
from agent_comms.build_identity import InstalledCommitInstallation, MissingCommitInstallation, RecordedBuildIdentity
from agent_comms.task_attachments.commander_pings import RulePing
from agent_comms.order_runs import ApplicationOrderRun, MechanicalOrderRun
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.standing_rules import OrderlessUnreviewedHead
from agent_comms.todos import ProgressRead
from agent_comms.ui_model.unit_state import AwaitingApplicationRun, DeliveredUnclosed

pytest_plugins = ("test_head_review",)

RUN = "The real application shows the merged fix working after a restart."


def test_only_commander_disposable_stale_lines_are_undisposed(head_bus):
    comms, _, _ = head_bus
    rule, = OrderlessUnreviewedHead.scan_reviews(comms.goals, "lead", ProgressRead())
    judge = replace(RulePing.capture(rule), since=RecordedEventTime(10.0))
    history = CommanderHistory.read(comms, "lead", rules=(rule,), observed=())
    cut = 1000.0
    assert judge.undisposed(history, cut)
    future = replace(judge, since=RecordedEventTime(2000.0))
    assert not future.undisposed(history, cut)
    assert not future.waits_on_commander(cut)
    subjects = (UnitSubject(comms.registry.require("worker").incarnation),)
    assert DeliveredUnclosed.disposes_subjects(subjects)
    assert not AwaitingApplicationRun.disposes_subjects(subjects)


def test_an_installed_application_order_waits_on_its_holder_not_the_commander():
    installed, missing = InstalledCommitInstallation(), MissingCommitInstallation("a" * 40, RecordedBuildIdentity(()))
    assert installed.awaits_holder(ApplicationOrderRun(RUN))
    assert not installed.awaits_holder(MechanicalOrderRun())
    assert not missing.awaits_holder(ApplicationOrderRun(RUN))
