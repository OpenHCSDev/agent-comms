"""Capacity reports the existing load formula and live forks without policy advice."""

import pytest


@pytest.mark.parametrize("unjudged", [0, 1, 395, 1_000_000])
def test_capacity_reports_load_and_forks(commander_script, unjudged):
    script, _ = commander_script
    load = script.capacity.Load(24, 14, unjudged, ("judge",))
    assert load.total == 24 + 2 * 14 + unjudged / 10
    assert str(load) == (
        f"load {load.total:.0f} (24 open orders, 14 overdue, {unjudged} unjudged edits), "
        "1 live forks ['judge']"
    )
