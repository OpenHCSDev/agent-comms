"""A second compaction must retain history even when it only cuts a turn prefix."""

import subprocess
from pathlib import Path

import pytest


def test_split_compaction_preserves_prior_summary(pinned_native):
    subprocess.run(
        ["node", str(Path(__file__).parent / "fixtures/pi_helpers/compaction_continuity.mjs"),
         str(pinned_native)],
        check=True, capture_output=True, text=True, timeout=15,
    )


@pytest.mark.parametrize("mode", ["manual", "preflight", "overflow", "send-now-overflow", "send-now-between-turns",
                                  "send-now-replacement-abort-overflow"])
def test_cancelled_compaction_cannot_send_the_waiting_prompt(pinned_native, tmp_path, mode):
    result = subprocess.run(
        ["node", str(Path(__file__).parent / "fixtures/pi_helpers/compaction_cancellation.mjs"),
         str(pinned_native), str(tmp_path / mode), mode],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
