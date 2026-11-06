"""Declaring or sending a runtime request loads no ACP: #4170 measured 0.7 s of it."""

import os
from pathlib import Path
import subprocess
import sys


def test_runtime_socket_imports_no_acp():
    env = {name: value for name, value in os.environ.items()
           if not name.startswith(("AGENT_COMMS_", "PI_"))}
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    result = subprocess.run(
        [sys.executable, "-c", "import sys; import agent_comms.runtime_server; "
         "assert not any(name == 'acp' or name.startswith('acp.') for name in sys.modules)"],
        env=env, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
