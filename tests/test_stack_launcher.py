"""The packaged launcher must use its pinned environment in child processes."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def test_toad_launcher_drops_inherited_pythonpath(tmp_path: Path) -> None:
    stack = tmp_path / "stack"
    bin_dir = stack / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()

    python = bin_dir / "python"
    python.write_text(
        "#!/bin/sh\n"
        'test "${PYTHONPATH-unset}" = unset || exit 86\n'
        'printf "%s\\n" "$TEST_PROJECT"\n'
    )
    python.chmod(0o755)
    toad = bin_dir / "toad"
    toad.write_text(
        "#!/bin/sh\n"
        'printf "PYTHONPATH=%s\\n" "${PYTHONPATH-unset}"\n'
    )
    toad.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "AGENT_COMMS_ROOT": str(tmp_path / "wire"),
            "AGENT_COMMS_STACK_ROOT": str(stack),
            "TEST_PROJECT": str(project),
            "PYTHONPATH": "/stale/toad:/stale/textual:/stale/comms",
        }
    )
    launcher = Path(__file__).resolve().parents[1] / "stack" / "bin" / "toad-comms"
    result = subprocess.run(
        [str(launcher), "thread"], env=env, text=True, capture_output=True, check=False
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "PYTHONPATH=unset\n"
