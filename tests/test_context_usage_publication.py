"""Neutral readings stay outside ACP; the ACP consumer owns usage publication."""

import os
import subprocess
import sys
from pathlib import Path


def test_native_launcher_imports_no_acp():
    env = {name: value for name, value in os.environ.items()
           if not name.startswith(("AGENT_COMMS_", "PI_"))}
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    result = subprocess.run(
        [sys.executable, "-c", "import sys; import agent_comms.execution.native_pi; "
         "import agent_comms.installer.active_route; "
         "assert not any(name == 'acp' or name.startswith('acp.') for name in sys.modules)"],
        env=env, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
