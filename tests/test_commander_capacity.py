"""Capacity reports the existing load formula and live forks without policy advice."""

import pytest

from agent_comms.commander.capacity import Load
from agent_comms.field_codec import FieldCodec


@pytest.mark.parametrize("heads", [0, 1, 395, 1_000_000])
def test_capacity_reports_head_work_and_forks(heads):
    load = Load(24, 14, heads, ("judge",))
    assert FieldCodec.encode(load)["total"] == load.total
    assert load.heads_waiting == heads
    assert load.total == 24 + 2 * 14 + heads
    assert load.forks == ("judge",)


