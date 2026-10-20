"""A real local RPC child pauses the owner before the fenced prompt write."""

from agent_comms.native_package import VerifiedNativePackage, pinned_native_package
from agent_comms.launch_runtime import LaunchRuntime, RestartEnvironment
from agent_comms.selected_session import SelectedSession
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import agent_comms.coordinated_runtime as runtime
import agent_comms.native_pi as native_pi
from agent_comms.maintenance_barrier import MaintenancePhase
from agent_comms.child_process import ProcessIdentity
from agent_comms.coordinator import Coordination
from agent_comms.errors import RelationViolationError
from agent_comms.native_runtime_input import NativeRuntimeInput
from test_coordinated_runtime import _root


@pytest.mark.asyncio
async def test_private_native_raw_prompt_refused_after_pause_ack(tmp_path: Path, tmp_path_factory, *, native_runtime_package, cursor_delivery) -> None:
    root, root_id, comms, _initial, _people = _root(tmp_path, direct=True)
    received = tmp_path / "received.jsonl"
    child_pid = tmp_path / "child-pid"
    program = tmp_path / "pause-rpc.py"
    program.write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "from agent_comms.field_codec import FieldCodec\n"
        "from agent_comms.maintenance_barrier import (\n"
        "    MaintenanceBarrier, MaintenanceOperator, PausedPhase)\n"
        f"Path({str(child_pid)!r}).write_text(str(os.getpid()))\n"
        "request = json.loads(sys.stdin.readline())\n"
        f"with open({str(received)!r}, 'a') as log: log.write(json.dumps(request) + '\\n')\n"
        f"control = MaintenanceBarrier(Path({str(root / 'registry.json')!r}))\n"
        "first = control.begin(MaintenanceOperator.current())\n"
        "assert control.advance(first, PausedPhase).phase is PausedPhase\n"
        "file = sys.argv[1]\n"
        f"reply = {{'type':'response','id':request['id'],'command':'get_state','success':True,"
        f"'data':{{'nativeInputProofCapability':{native_pi.CAPABILITY!r},"
        "'sessionId':'fixture','sessionFile':file}}\n"
        "print(json.dumps(reply), flush=True)\n"
        "for line in sys.stdin:\n"
        f"    with open({str(received)!r}, 'a') as log: log.write(line)\n"
    )

    def launch(package, *, worktree, session, **_kwargs):
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
            SelectedSession(session_dir),
            native_runtime_package,
            root=worktree, runtime=LaunchRuntime.running(), configuration=RestartEnvironment.inherit(env),
        )

    with patch.object(native_pi.NativePiRpcLaunch, 'tracked', side_effect=launch), pytest.raises(RelationViolationError, match='Maintenance'):
        await runtime.SelectedExecution(
            root=root,
            wire_root_id=root_id,
            owner_name="beta",
            native_package=native_runtime_package,
            opt_in=True,
            cursor_delivery=cursor_delivery,
        ).run()
    assert comms.owners.maintenance.read().phase == MaintenancePhase.member_named("paused")
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
