import shutil
import sys
from dataclasses import replace

import pytest

from agent_comms.commander.capacity import Load
from agent_comms.commander.config import CommanderConfig
from agent_comms.commander_parity.load import CommandComparison, LoadComparison, state_files
from agent_comms.commander_parity.observations import CapacityObservation, MeasuredOutput


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


@pytest.mark.parametrize(
    ("program", "expected"),
    (
        ("raise SystemExit(7)", "exited with code 7"),
        ("import time; time.sleep(10)", "timed out"),
    ),
)
def test_failed_real_children_remain_named_unverified(tmp_path, program, expected):
    """Negative process checks produce no fabricated native measurement or parity pass."""

    class BriefComparison(CommandComparison):
        timeout = 3

    config = CommanderConfig(
        commander="reader",
        root=tmp_path / "bus",
        state=tmp_path / "state",
        repos=(),
        worktrees=tmp_path / "projects",
        scratch=tmp_path / "scratch",
        disk=tmp_path,
    )
    comparison = BriefComparison.invoke(config, ("-c", program))
    assert not comparison.passed
    assert expected in comparison.describe()
    assert "bound=3s" in comparison.describe()
    assert LoadComparison.timeout == 30
