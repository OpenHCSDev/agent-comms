from dataclasses import replace

from agent_comms.commander.capacity import Load
from agent_comms.commander_parity.load import LoadComparison, MeasuredOutput
from agent_comms.private_path import FileRevision


def test_load_comparison_requires_every_reported_field_and_no_private_write(tmp_path):
    load = Load(37, 8, 1692, ("merger",))
    observed = MeasuredOutput(str(load) + "\n", load, "capacity.py", "/bus", "lead")
    comparison = LoadComparison(observed, (), ())
    assert comparison.passed
    for different in (
        replace(load, owed=38),
        replace(load, overdue=9),
        replace(load, unjudged=1693),
        replace(load, forks=()),
    ):
        assert not replace(comparison, observation=replace(observed, load=different)).passed
    path = tmp_path / "old-state"
    path.write_text("before")
    before = ((path.name, FileRevision.capture(path)),)
    path.write_text("after")
    after = ((path.name, FileRevision.capture(path)),)
    assert not LoadComparison(observed, before, after).passed
