"""Capacity reports the existing load formula and live forks without policy advice."""

import pytest

from agent_comms.commander import capacity
from agent_comms.commander.capacity import Load
from agent_comms.field_codec import FieldCodec
from agent_comms.tools import CommsCommanderLoadTool
from test_commander_problem_orders import create


@pytest.mark.parametrize("unjudged", [0, 1, 395, 1_000_000])
def test_capacity_reports_load_and_forks(unjudged):
    load = Load(24, 14, unjudged, ("judge",))
    assert FieldCodec.encode(load)["total"] == load.total
    assert load.total == 24 + 2 * 14 + unjudged / 10
    assert str(load) == (
        f"load {load.total:.0f} (24 open orders, 14 overdue, {unjudged} unjudged edits), "
        "1 live forks ['judge']"
    )


