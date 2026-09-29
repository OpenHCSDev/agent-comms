"""A real local RPC child pauses the owner before the fenced prompt write."""

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_comms import coordinated_runtime as runtime
from agent_comms import native_pi
from agent_comms.child_process import ProcessIdentity
from agent_comms.coordinator import Coordination
from agent_comms.errors import RelationViolationError
from agent_comms.native_runtime_input import NativeRuntimeInput
from test_coordinated_runtime import _root


@pytest.mark.asyncio
async def test_private_native_raw_prompt_refused_after_pause_ack(
    tmp_path: Path, tmp_path_factory
) -> None:
    root, root_id, comms, _initial, _people = _root(tmp_path, direct=True)
    received = tmp_path / "received.jsonl"
    child_pid = tmp_path / "child-pid"
    program = tmp_path / "pause-rpc.py"
    program.write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "from agent_comms.maintenance_barrier import MaintenanceBarrier\n"
        "from maintenance_control_fixture import FixtureMaintenanceControl\n"
        f"Path({str(child_pid)!r}).write_text(str(os.getpid()))\n"
        "request = json.loads(sys.stdin.readline())\n"
        f"with open({str(received)!r}, 'a') as log: log.write(json.dumps(request) + '\\n')\n"
        f"control = FixtureMaintenanceControl(MaintenanceBarrier(Path({str(root / 'registry.json')!r})))\n"
        "first = control.begin('disposable-operator')\n"
        "assert control.advance(first, 'paused').phase == 'paused'\n"
        "file = sys.argv[1]\n"
        f"reply = {{'type':'response','id':request['id'],'command':'get_state','success':True,"
        f"'data':{{'nativeInputProofCapability':{native_pi.CAPABILITY!r},"
        "'sessionId':'fixture','sessionFile':file}}\n"
        "print(json.dumps(reply), flush=True)\n"
        "for line in sys.stdin:\n"
        f"    with open({str(received)!r}, 'a') as log: log.write(line)\n"
    )

    def launch(package, *, worktree, session_dir, session_file=None, **_kwargs):
        # Keep the real selected tool socket inside the owned basetemp and
        # below the operating system Unix socket path limit.
        session_dir = tmp_path_factory.getbasetemp() / "native-pause"
        session_dir.mkdir(mode=0o700)
        file = session_dir / "fixture.jsonl"
        file.write_text('{"type":"session","id":"fixture"}\n')
        file.chmod(0o600)
        env = dict(os.environ, AGENT_COMMS_ROOT=str(root))
        repo = Path(__file__).resolve().parents[1]
        env["PYTHONPATH"] = os.pathsep.join((str(repo / "src"), str(repo / "tests")))
        return native_pi.NativePiRpcLaunch(
            (sys.executable, str(program), str(file)),
            worktree,
            env,
            session_dir,
            None,
            tmp_path,
        )

    with (
        patch.object(runtime, "_trusted_package", lambda _package: None),
        patch.object(native_pi.NativePiRpcLaunch, "tracked", launch),
        pytest.raises(RelationViolationError, match="Maintenance"),
    ):
        await runtime.SelectedExecution(
            root=root, wire_root_id=root_id, owner_name="beta", native_package=tmp_path, opt_in=True
        ).run()
    assert comms.owners.maintenance.read().phase == "paused"
    writes = [json.loads(line) for line in received.read_text().splitlines()]
    assert [item["type"] for item in writes] == ["get_state"]
    with pytest.raises(ProcessLookupError):
        ProcessIdentity.capture(int(child_pid.read_text()))
    with Coordination(str(root / "coordination.sqlite3")) as store:
        assert (
            store.session._connection.execute(
                f"SELECT COUNT(*) FROM {NativeRuntimeInput.declared_name}"
            ).fetchone()[0]
            == 1
        )
