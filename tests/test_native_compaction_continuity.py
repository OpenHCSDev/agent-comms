"""A second compaction must retain history even when it only cuts a turn prefix."""

import subprocess
from pathlib import Path


def test_split_compaction_preserves_prior_summary(pinned_native):
    subprocess.run(
        ["node", str(Path(__file__).parent / "fixtures/pi_helpers/compaction_continuity.mjs"),
         str(pinned_native)],
        check=True, capture_output=True, text=True, timeout=15,
    )
