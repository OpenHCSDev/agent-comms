"""Declaring or sending a runtime request loads no ACP: #4170 measured 0.7 s of it."""

import os
import subprocess
import sys
from pathlib import Path


def cold_runtime(program: str) -> subprocess.CompletedProcess[str]:
    env = {name: value for name, value in os.environ.items()
           if not name.startswith(("AGENT_COMMS_", "PI_"))}
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    return subprocess.run(
        [sys.executable, "-c", program],
        env=env, capture_output=True, text=True, timeout=20,
    )


def test_runtime_wire_imports_no_acp():
    result = cold_runtime(
        "import sys; from agent_comms.transport.owner_requests import ReadHostResourcesRuntimeRequest; "
        "assert not any(name == 'acp' or name.startswith('acp.') for name in sys.modules)"
    )
    assert result.returncode == 0, result.stderr


def test_cold_server_decodes_the_served_request_without_prior_producer_imports():
    result = cold_runtime('''
from agent_comms.transport.runtime_server import ServedRuntimeRequest
from agent_comms.transport.owner_requests import ReadHostResourcesRuntimeRequest
from agent_comms.transport.runtime_request import RuntimeRequest
from agent_comms.nominal.field_codec import FieldCodec

request = FieldCodec.decode(ServedRuntimeRequest, FieldCodec.encode(ReadHostResourcesRuntimeRequest(thread="cold")))
assert isinstance(request, ServedRuntimeRequest), type(request)
members = RuntimeRequest.members_with(RuntimeRequest)
assert members
assert all(issubclass(member, ServedRuntimeRequest) for member in members), members
assert RuntimeRequest.member_named("start_owner").bind
print(f"served {len(members)} of {len(members)} runtime request members")
''')
    assert result.returncode == 0, result.stderr
