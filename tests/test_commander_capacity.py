"""The existing capacity gate counts judging as one slice of work."""

import pytest


@pytest.mark.parametrize("unjudged", [0, 1, 395, 1_000_000])
def test_judging_backlog_is_one_slice_used_by_total_and_description(commander_script, unjudged):
    script, _ = commander_script
    load = script.capacity.Load(24, 14, unjudged, tuple(f"fork-{n}" for n in range(9)))
    assert load.judging_load == (script.capacity.PER_COMMANDER if unjudged else 0)
    assert load.total == 24 + 2 * 14 + load.judging_load
    assert f"{unjudged} unjudged edits: {load.judging_load} judging load" in str(load)
    assert load.needed == (9 if unjudged else 8)
    assert not load.short
    load.require()


def test_nine_forks_cover_the_reported_fleet_and_eight_do_not(commander_script):
    script, _ = commander_script
    forks = tuple(f"fork-{n}" for n in range(9))
    load = script.capacity.Load(24, 14, 395, forks)
    assert load.total == 58 and load.needed == 9
    load.require()
    with pytest.raises(SystemExit, match="needs 9 live fork"):
        script.capacity.Load(24, 14, 395, forks[:-1]).require()
