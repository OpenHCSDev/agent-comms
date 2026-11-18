"""Only a heartbeat line the team can dispose of locks its writing verbs."""

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field, fields, replace
from pickle import dumps, loads

from agent_comms.cli_bridges.harness_delivery_states import DeliveredOutcome
from agent_comms.execution.delivers_input import DeliversInput
from agent_comms.heartbeat_subjects import TeamHistory, TeammateSubject
from agent_comms.installer.build_identity import (
    InstalledCommitInstallation,
    MissingCommitInstallation,
    RecordedBuildIdentity,
)
from agent_comms.messaging.messages import InfoKind, Message
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.nominal.text_digest import TextDigest
from agent_comms.planning.order_runs import ApplicationOrderRun, MechanicalOrderRun
from agent_comms.registry.thread_identity import RecordedThreadIncarnation
from agent_comms.storage.recorded_time import RecordedEventTime
from agent_comms.task_attachments import team_pings
from agent_comms.task_attachments.team_pings import (
    Ping,
    RulePing,
    TeamDecisions,
    TeammatePing,
    TeamUpdateNotice,
)
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


def test_alarm_encoding_preserves_occurrence_evidence_and_json_ownership():
    alarms = (
        teammate_line(DeliveredUnclosed, 10.0),
        RulePing(OrderlessUnreviewedHead, TextDigest.of("review"), text="Read the exact head",
                 subjects=(TeammateSubject(TEAMMATE),), since=RecordedEventTime(10.0)),
    )
    for alarm in alarms:
        process_packet = dumps(alarm)
        original = FieldCodec.encode_record(alarm)
        first = FieldCodec.encode(alarm)
        assert first == original
        assert dumps(alarm) == process_packet
        assert FieldCodec.encode(FieldCodec.decode(Ping, first)) == original
        assert FieldCodec.encode(FieldCodec.decode(type(alarm), first)) == original
        first["subjects"].clear()
        first.clear()
        assert FieldCodec.encode(alarm) == original
        changed = replace(alarm, text="Different evidence", since=RecordedEventTime(22.0))
        assert changed == alarm
        assert FieldCodec.encode(changed) == FieldCodec.encode_record(changed)
        assert FieldCodec.encode(changed) != original
        assert FieldCodec.encode(alarm) == original


def test_process_copies_share_only_class_fields_and_keep_occurrence_data(monkeypatch):
    alarm = RulePing(OrderlessUnreviewedHead, TextDigest.of("original"), text="Original evidence",
                     subjects=(TeammateSubject(TEAMMATE),), since=RecordedEventTime(10.0))
    before = FieldCodec.encode_record(alarm)
    FieldCodec.encode(alarm)
    acquired = []
    declared_fields = team_pings.fields

    def reading(value):
        acquired.append(value)
        return declared_fields(value)

    monkeypatch.setattr(team_pings, "fields", reading)
    first = deepcopy(alarm)
    reads = len(acquired)
    for _ in range(10):
        for copied in (deepcopy(alarm), loads(dumps(alarm))):
            assert FieldCodec.encode_record(copied) == before
            assert copied is not alarm
            assert copied.subjects[0] is not alarm.subjects[0]
            assert "_encoded_record" not in copied.__dict__
    assert len(acquired) == reads
    assert FieldCodec.encode_record(first) == before

    @dataclass(frozen=True)
    class DetailedRulePing(RulePing):
        details: list[str] = field(default_factory=list)
        internal: str = field(default="init-false field", init=False)

    try:
        detail = DetailedRulePing(OrderlessUnreviewedHead, TextDigest.of("detail"), text="detail",
                                  details=["keep"])
        copied = deepcopy(detail)
        assert copied.__getstate__().keys() == {item.name for item in fields(detail)}
        assert copied.internal == detail.internal
        copied.details.append("private")
        assert detail.details == ["keep"]
        assert FieldCodec.encode_record(alarm) == before
    finally:
        DetailedRulePing.withdraw()


def test_packet_wake_choice_uses_the_shared_delivery_contract():
    class Recipient(DeliversInput[Message]):
        def __init__(self):
            self.received = []

        async def deliver(self, message):
            self.received.append(message)
            return DeliveredOutcome

    recipient = Recipient()
    message = Message("sender", "teammate", "original", InfoKind)
    for packet, expected in ((TeamUpdateNotice(TEAMMATE, ()), []),
                             (TeamDecisions(TEAMMATE, (), (), ()), [message])):
        assert asyncio.run(packet.delivered_by(recipient, message)) is DeliveredOutcome
        assert recipient.received == expected


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
