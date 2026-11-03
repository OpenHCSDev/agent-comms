"""A second compaction must retain history even when it only cuts a turn prefix."""

import subprocess
from pathlib import Path

import pytest


def test_compaction_retains_current_work_without_lifetime_inventory(pinned_native):
    result = subprocess.run(
        ["node", str(Path(__file__).parent / "fixtures/pi_helpers/compaction_continuity.mjs"),
         str(pinned_native)],
        capture_output=True, text=True, timeout=15,
    )

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("mode", [
    "manual", "preflight", "overflow", "send-now-overflow", "send-now-between-turns",
    "send-now-replacement-abort-overflow",
])
def test_compaction_controls_preserve_input_custody(pinned_native, tmp_path, mode):
    result = subprocess.run(
        ["node", str(Path(__file__).parent / "fixtures/pi_helpers/compaction_cancellation.mjs"),
         str(pinned_native), str(tmp_path / mode), mode],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
