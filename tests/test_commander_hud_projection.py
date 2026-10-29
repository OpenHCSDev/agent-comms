"""Root-owned controls for the shared heartbeat and native HUD presentation."""

from dataclasses import replace

from agent_comms.commander.vibe_check import OverloadDegradation
from agent_comms.commander_hud import CommanderHud, CommanderOrder, CommanderUnit, CodexUsage, HudRecord, UnreadableUsage
from agent_comms.disclosure_text import DisclosureText
from agent_comms.field_codec import DisclosureIdentity, FieldCodec, OrderId
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.standing_rules import ServicePassFailing
from agent_comms.task_attachments.commander_pings import CommanderDecisions, RulePing
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.todos import OpenPrProgress
from agent_comms.ui_model.unit_state import (
    AwaitingApplicationRun, Idle, IdleGroup, InProgressGroup, Moving, NoPendingClock,
    PendingClock, ReadyToTestGroup, Stuck, UrgentGroup,
)


def example_hud() -> CommanderHud:
    """Controlled source example; no provider call or claim about the installed fleet."""
    order = CommanderOrder(
        "P930", "Make the screen readable", OrderId("123456789abc"),
        "pending", OpenPrProgress(3589, "8636a6f28", at=1.0), (), 0, 0,
    )
    worker = CommanderUnit(
        RecordedThreadIncarnation("screen", 2.0), Moving, NoPendingClock(),
        (order,), 0, (), (),
    )
    commander = RecordedThreadIncarnation("lead", 1.0)
    old = replace(RulePing.capture(ServicePassFailing("source", 900, "read refused")),
                  since=RecordedEventTime(900))
    new = replace(RulePing.capture(ServicePassFailing("review", 1000, "head needs review")),
                  since=RecordedEventTime(1000))
    return CommanderHud(
        commander, (
            worker,
            replace(worker, unit=RecordedThreadIncarnation("blocked", 3.0), state=Stuck),
            replace(worker, unit=RecordedThreadIncarnation("run-next", 4.0), state=AwaitingApplicationRun),
            replace(worker, unit=RecordedThreadIncarnation("idle", 5.0), state=Idle, orders=()),
        ), (), OverloadDegradation(3, 1, 2), (),
        CodexUsage(UnreadableUsage(reason="not requested in source example")),
        (CommanderDecisions(commander, (old, new), (new,), (old,)),),
        heartbeat=RecordedEventTime(990), open_pings=(),
    )


def test_commander_hud_projection_has_one_progress_answer() -> None:
    unit = example_hud().units[0]
    assert tuple(FieldCodec.projections(CommanderUnit, "hud")) == ("work", "change", "next")
    columns = FieldCodec.view_columns(CommanderUnit, "hud")
    assert tuple(column.name for column in columns) == ("unit", "state", "work", "change", "next")
    assert DisclosureText.view(unit, "hud") == (
        "screen", "moving", "P930 pending", "PR #3589 open at 8636a6f28", "let it continue",
    )
    assert tuple(plan.name for plan in FieldCodec.field_plans(type(unit.unit))
                 if DisclosureIdentity.read(plan.annotation, False)) == ("name",)
    second = replace(unit.orders[0], problem="P931", order=OrderId("abcdef123456"))
    assert "\n" in DisclosureText.view(replace(unit, orders=(*unit.orders, second)), "hud")[2]


def test_shared_hud_groups_strip_full_alarms_and_plain_text() -> None:
    hud = example_hud()
    groups = hud.groups()
    assert tuple(section.group for section in groups) == (
        UrgentGroup, InProgressGroup, ReadyToTestGroup, IdleGroup,
    )
    assert tuple(unit.unit.name for section in groups for unit in section.records) == (
        "blocked", "screen", "run-next", "idle",
    )
    plain = hud.hud_text(1000, width=120)
    status = tuple(hud.status_strip(1000, width=120).splitlines())
    assert plain[:len(status)] == status
    assert "\x1b" not in "\n".join(plain)
    headings = [section.heading() for section in groups]
    assert [plain.index(heading) for heading in headings] == sorted(plain.index(heading) for heading in headings)
    assert "CommanderUnit" not in "\n".join(plain)
    assert "\x1b[" in "\n".join(hud.hud_text(1000, width=120, colour=True))
    assert sum(len(section.pings) for section in hud.alarms) == len(hud.packets[0].handled)
    assert all(ping.text in "\n".join(hud.alarm_text(hud.alarms, 1000))
               for ping in hud.packets[0].handled)
    assert hud.board == ()
    assert hud.alarm_detail(hud.alarms, 1000) == tuple(
        ping.render(1000) for section in hud.alarms for ping in section.pings
    )
    for section in groups:
        for table in section.tables:
            narrow = "\n".join(table.lines(60))
            assert all(unit.unit.name in narrow for unit in table.records)
    assert tuple(unit.unit for unit in groups[0].records) == (hud.units[1].unit,)


def test_display_facts_ignore_only_hidden_clocks_and_paint_time() -> None:
    hud = example_hud()
    unit = replace(hud.units[0], clock=PendingClock(1, 2))
    hud = replace(hud, units=(unit, *hud.units[1:]))
    later = replace(hud, units=(replace(unit, clock=PendingClock(1, 3)), *hud.units[1:]),
                    heartbeat=RecordedEventTime(999))
    assert hud.display_facts == later.display_facts
    assert hud.status_table(1000).facts == hud.status_table(1060).facts
    assert hud.display_facts != replace(hud, usage=CodexUsage(
        UnreadableUsage(reason="a different visible usage observation"))).display_facts
    assert hud.display_facts != replace(hud, units=(replace(unit, state=Stuck), *hud.units[1:])).display_facts


def test_every_native_record_producer_declares_entity_identity() -> None:
    for declaration in HudRecord.__subclasses__():
        assert any(DisclosureIdentity.read(plan.annotation, False)
                   for plan in FieldCodec.field_plans(declaration)), declaration
