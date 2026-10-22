"""Capacity reports the load formula without policy advice."""

import pytest

from agent_comms.commander.capacity import Load
from agent_comms.field_codec import FieldCodec


@pytest.mark.parametrize("unjudged", [0, 1, 395, 1_000_000])
def test_capacity_reports_load(unjudged):
    load = Load(24, 14, unjudged)
    assert FieldCodec.encode(load)["total"] == load.total
    assert load.total == 24 + 2 * 14 + unjudged / 10
    assert str(load) == (
        f"load {load.total:.0f} (24 open orders, 14 review timers up, {unjudged} unjudged edits)"
    )


