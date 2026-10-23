"""Headless failures retain inspectable structural evidence before their channel notice."""

import json
import os

from agent_comms.diagnostics import record_terminal_failure
from agent_comms.turn_failure import NativePreflightTimeout


def test_terminal_diagnostic_excludes_untrusted_payloads(tmp_path):
    path = record_terminal_failure(
        tmp_path,
        turn_id="a" * 32,
        thread="worker",
        sequences=(6943,),
        failure=NativePreflightTimeout,
        measurements={
                "wait_ms": 5000,
                "session_bytes": 88255627,
                "exit_code": -15,
                "token": "SECRET",
                "spawn_ms": "SECRET",
        },
    )
    raw = path.read_text()
    value = json.loads(raw)
    assert value["failure"] == NativePreflightTimeout.declared_name
    assert value["measurements"] == {"wait_ms": 5000, "session_bytes": 88255627, "exit_code": -15}
    assert value["sequences"] == [6943]
    assert all(fragment not in raw for fragment in ("SECRET", "private user", "base64"))
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700


