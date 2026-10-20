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


def test_capacity_observes_actual_cli_selected_root(commander_script, tmp_path, monkeypatch):
    original, _ = commander_script
    selected = tmp_path / "selected"
    shutil.copytree(original.root, selected)
    before = state_files(original.root)
    monkeypatch.setattr(sys, "argv", ["capacity-observation"])

    observed = CapacityObservation().read(selected)

    assert observed.root == str(selected.resolve())
    assert observed.commander == original.commander
    assert observed.current.strip() == observed.native
    assert state_files(original.root) == before
    assert sys.monitoring.get_tool(sys.monitoring.PROFILER_ID) is None


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
        board=tmp_path / "board.md",
        narrative=tmp_path / "narrative.md",
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
