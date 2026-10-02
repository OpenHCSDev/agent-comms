"""The Toad launcher follows the installed runtime and active route."""

from __future__ import annotations

import os
import fcntl
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(os.name == "nt", reason="toad-comms is a POSIX Bash launcher")
def test_toad_launcher_drops_inherited_pythonpath(tmp_path: Path) -> None:
    bin_dir = tmp_path / "runtime"
    bin_dir.mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()
    home = tmp_path / "home"
    route_directory = home / ".local/state/agent-comms"
    route_directory.mkdir(parents=True)

    python = bin_dir / "python"
    python.write_text(
        "#!/bin/sh\n"
        'test "${PYTHONPATH-unset}" = unset || exit 86\n'
        'test "${AGENT_COMMS_ROOT-unset}" = unset || exit 87\n'
        'printf "%s\\n" "$TEST_PROJECT"\n'
    )
    python.chmod(0o755)
    toad = bin_dir / "toad"
    toad.write_text(
        "#!/bin/sh\n"
        'printf "PYTHONPATH=%s ROOT=%s\\n" "${PYTHONPATH-unset}" "${AGENT_COMMS_ROOT-unset}"\n'
    )
    toad.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "AGENT_COMMS_ROOT": str(tmp_path / "wire"),
            "AGENT_COMMS_RUNTIME_ROOT": str(bin_dir),
            "TEST_PROJECT": str(project),
            "PYTHONPATH": "/stale/toad:/stale/textual:/stale/comms",
            "HOME": str(home),
        }
    )
    launcher = Path(__file__).resolve().parents[1] / "stack" / "bin" / "toad-comms"
    result = subprocess.run(
        [str(launcher), "thread"], env=env, text=True, capture_output=True, check=False
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "PYTHONPATH=unset ROOT=unset\n"
    # The same canonical bootstrap must refuse before invoking either runtime
    # executable when the existing route directory is owned by publication.
    descriptor = os.open(route_directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        refused = subprocess.run(
            [str(launcher), "thread"], env=env, text=True, capture_output=True,
        )
        assert refused.returncode == 1
        assert refused.stdout == ""
        assert "client not launched" in refused.stderr
    finally:
        os.close(descriptor)
