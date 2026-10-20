from dataclasses import replace

from agent_comms.commander.capacity import Load
from agent_comms.commander_parity.load import LoadComparison, state_files
from agent_comms.commander_parity.observations import MeasuredOutput


def test_load_comparison_requires_every_reported_field_and_no_private_write(tmp_path):
    load = Load(37, 8, 1692, ("merger",))
    observed = MeasuredOutput(str(load) + "\n", str(load), "capacity.py", "/bus", "lead")
    comparison = LoadComparison(observed, (), ())
    assert comparison.passed
    for different in (
        replace(load, owed=38),
        replace(load, overdue=9),
        replace(load, unjudged=1693),
        replace(load, forks=()),
    ):
        assert not replace(comparison, observation=replace(observed, native=str(different))).passed
    path = tmp_path / "old-state"
    path.write_text("before")
    before = state_files(tmp_path)
    path.write_text("after")
    after = state_files(tmp_path)
    assert not LoadComparison(observed, before, after).passed
