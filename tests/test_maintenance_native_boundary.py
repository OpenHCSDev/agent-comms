"""Private coordinator native adapter: pause between preflight and raw prompt send."""

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_comms import coordinated_runtime as runtime
from agent_comms import native_pi
from agent_comms.comms import Comms
from agent_comms.coordination_store import MutationStore
from agent_comms.errors import RelationViolationError
from maintenance_control_fixture import FixtureMaintenanceControl
from test_coordinated_runtime import _root, tmp_path  # noqa: F401

@pytest.mark.asyncio
async def test_private_native_raw_prompt_refused_after_pause_ack(tmp_path: Path) -> None:
    root, root_id, comms, _initial, _people = _root(tmp_path, direct=True)
    assert isinstance(comms, Comms)
    writes: list[dict] = []
    pause_receipts = []

    class Stdin:
        def write(self, data: bytes) -> None:
            writes.append(json.loads(data))

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            return None

    class Reader:
        def __init__(self, response: bytes = b""):
            self.response = response

        async def readuntil(self, separator: bytes) -> bytes:
            response, self.response = self.response, b""
            if response:
                # The real coordinator's registry turn claim and get_state
                # send already happened; pause ACK precedes raw prompt write.
                fixture = FixtureMaintenanceControl(comms.owners.maintenance)
                first = fixture.begin("disposable-operator")
                pause_receipts.append(fixture.advance(first, "paused"))
            return response

        async def read(self, *_args) -> bytes:
            return b""

    process = None

    def launch(package, *, worktree, session_dir, session_file=None, **_kwargs):
        nonlocal process
        session_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        file = session_dir / "fixture.jsonl"
        file.write_text('{"type":"session","id":"fixture"}\n')
        file.chmod(0o600)
        reply = {
            "type": "response",
            "id": "native-capability",
            "command": "get_state",
            "success": True,
            "data": {
                "nativeInputProofCapability": native_pi.CAPABILITY,
                "sessionId": "fixture",
                "sessionFile": str(file),
            },
        }

        class Process:
            pid = 999999999  # Never signaled: this stream fixture is already exited.
            stdin = Stdin()
            stdout = Reader((json.dumps(reply) + "\n").encode())
            stderr = Reader()
            returncode = 0

            async def wait(self):
                return 0

        process = Process()
        return native_pi.NativePiRpcLaunch(
            ("not-executed",), worktree, {"AGENT_COMMS_ROOT": str(root)}, session_dir, None
        )

    async def create(*_args, **_kwargs):
        return process

    async def fake_raw_send(stdin, payload, boundary, *, timeout):
        # The real raw writer runs in a thread; preserve its no-owner-loop
        # boundary requirement while recording whether bytes could be sent.
        def attempt() -> None:
            with boundary():
                stdin.write(payload)

        await asyncio.to_thread(attempt)

    with (
        patch.object(runtime, "_trusted_package", lambda _package: None),
        patch.object(native_pi, "prepare_native_pi_rpc_launch", launch),
        patch.object(asyncio, "create_subprocess_exec", create),
        patch.object(native_pi, "send_fenced_prompt", fake_raw_send),
        pytest.raises(RelationViolationError, match="Maintenance"),
    ):
        await runtime.SelectedExecution(
            root=root, wire_root_id=root_id, owner_name="beta", native_package=tmp_path, opt_in=True
        ).run()
    assert len(pause_receipts) == 1 and pause_receipts[0].phase == "paused"
    assert [item["type"] for item in writes] == ["get_state"]
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        assert (
            store._connection.execute("SELECT COUNT(*) FROM native_runtime_inputs").fetchone()[0]
            == 1
        )
