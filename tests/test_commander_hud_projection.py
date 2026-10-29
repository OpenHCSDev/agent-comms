"""The heartbeat and table borrow the same declared HUD projection."""

from agent_comms.commander_hud import CommanderOrder, CommanderUnit
from agent_comms.field_codec import DisclosureIdentity, FieldCodec, OrderId
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.todos import OpenPrProgress
from agent_comms.ui_model.unit_state import Moving, NoPendingClock


def test_commander_hud_projection_has_one_progress_answer() -> None:
    progress = OpenPrProgress(3572, "87dabd883", at=1.0)
    order = CommanderOrder(
        "P927", "Make the screen readable", OrderId("123456789abc"),
        "pending", progress, (), 0, 0,
    )
    unit = CommanderUnit(
        RecordedThreadIncarnation("worker", 2.0), Moving, NoPendingClock(),
        (order,), 0, (), (),
    )
    assert tuple(FieldCodec.projections(CommanderUnit, "hud")) == ("work", "change", "next")
    assert tuple(plan.name for plan in FieldCodec.field_plans(CommanderUnit)
                 if plan.records and not plan.excluded_in("hud")) == ("unit", "state")
    assert unit.work == "P927 pending"
    assert unit.change == progress.line
    assert unit.next == Moving.action
    assert unit.brief().count("#3572") == 1
    assert tuple(plan.name for plan in FieldCodec.field_plans(type(unit.unit))
                 if DisclosureIdentity.read(plan.annotation, False)) == ("name",)
