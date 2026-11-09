"""Load weighs an expired review timer twice and every unjudged edit once."""

from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.team.vibe_check import OverloadDegradation


def test_capacity_reports_load():
    load = OverloadDegradation(24, 14, 395)
    assert load.total == 24 + 2 * 14 + 395
    assert FieldCodec.encode(load)["total"] == load.total
