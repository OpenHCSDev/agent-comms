"""Default-OFF native Pi launcher path with disposable fake RPC descendants only."""

from __future__ import annotations

import asyncio
import json
import os
import select
import signal
import subprocess
import sys
import time
from contextlib import nullcontext
from pathlib import Path

import pytest

from agent_comms import native_pi
from agent_comms.native_pi_retirement import (
    RetirementIdentity,
    RetirementUnavailable,
    _pidfd_kill,
    _pidfd_open,
)
from agent_comms.native_prompt_binding import native_request_digest

_INPUT = "a" * 32
_PROMPT = "one exact private selected input"


def _identity(session_file: Path, *, prompt: str = _PROMPT) -> RetirementIdentity:
    return RetirementIdentity(
        wire_root_id="1" * 32,
        input_id=_INPUT,
        stage="full",
        claim_id="cohort-v1:exact-claim",
        execution_id="wirev1exact-execution",
        attempt_ordinal=1,
        owner_thread="beta",
        recipient_lookup="2" * 32,
        owner_created_at=17_000.0,
        owner_pid=os.getpid(),
        owner_generation=2,
        owner_admission_epoch=3,
        owner_turn_id="turn-instance-1",
        source_seq=7,
        source_message_id="message-7",
        native_request_digest=native_request_digest(prompt),
        session_file=session_file,
    )


def _fake_rpc(tmp_path: Path, *, mutate_inode: bool = False, hang: bool = False) -> Path:
    script = tmp_path / "fake-private-rpc.py"
    script.write_text("""import json, os, pathlib, signal, subprocess, sys
file = pathlib.Path(sys.argv[1]); digest = sys.argv[2]
mutate_inode, hang = sys.argv[3:] == ['1','0'], sys.argv[4] == '1'
def emit(value): print(json.dumps(value), flush=True)
state = json.loads(sys.stdin.readline())
emit({'type':'response','id':state['id'],'command':'get_state','success':True,
      'data':{'nativeInputProofCapability':'pi-native-input-v1-live-only',
              'sessionId':'sid','sessionFile':str(file)}})
command = json.loads(sys.stdin.readline()); input_id = command['inputId']
if hang:
    pathlib.Path(str(file)+'.prompt-seen').write_text('seen')
    signal.pause()
# Both fake helper groups outlive the RPC reply until namespace PID 1 dies.
rpc_source = ('import os,signal,subprocess,sys;'
 'tool=subprocess.Popen([sys.executable,\"-c\",'
 '\"import signal;signal.pause()\"],start_new_session=True);'
 'print(os.getpid(),tool.pid,flush=True); signal.pause()')
rpc = subprocess.Popen([sys.executable,'-c',rpc_source],
 stdout=subprocess.PIPE, start_new_session=True)
assert rpc.stdout is not None and len(rpc.stdout.readline().split()) == 2
entry = {'type':'message','id':'entry','message':{'role':'user',
    'inputId':input_id,'inputDigest':digest}}
if mutate_inode:
    replacement = file.with_suffix('.replacement')
    replacement.write_text(file.read_text()+json.dumps(entry)+'\\n')
    os.chmod(replacement,0o600); os.replace(replacement,file)
else:
    with file.open('a') as output:
        output.write(json.dumps(entry)+'\\n'); output.flush(); os.fsync(output.fileno())
proof = {'schema':1,'type':'context_committed','sessionId':'sid',
    'inputId':input_id,'sessionEntryId':'entry','requestGeneration':1,
    'llmContextDigest':'c'*64}
proof_file = pathlib.Path(str(file)+'.input-proof')
with proof_file.open('w') as output:
    output.write(json.dumps(proof)+'\\n'); output.flush(); os.fsync(output.fileno())
os.chmod(proof_file,0o600)
parent = os.open(file.parent, os.O_RDONLY|os.O_DIRECTORY); os.fsync(parent); os.close(parent)
emit({'type':'response','id':command['id'],'command':'prompt','success':True})
emit({'type':'input_committed','sessionId':'sid','inputId':input_id,'sessionEntryId':'entry'})
emit(proof)
emit({'type':'message_update','assistantMessageEvent':{'type':'text_delta','delta':'DONE'}})
emit({'type':'message_end','message':{'role':'assistant','stopReason':'stop',
 'content':[{'type':'text','text':'DONE'}]}})
emit({'type':'agent_settled'})
signal.pause()
""")
    return script


def _prepare(tmp_path: Path, monkeypatch, *, mutate_inode: bool = False, hang: bool = False):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir(mode=0o700)
    session_file = session_dir / "one.jsonl"
    session_file.write_text(json.dumps({"type": "session", "id": "sid"}) + "\n")
    session_file.chmod(0o600)
    fake = _fake_rpc(tmp_path, mutate_inode=mutate_inode, hang=hang)

    def launch(*_args, **_kwargs):
        return native_pi.NativePiRpcLaunch(
            (
                sys.executable,
                "-u",
                str(fake),
                str(session_file),
                native_request_digest(_PROMPT),
                "1" if mutate_inode else "0",
                "1" if hang else "0",
            ),
            tmp_path,
            {**os.environ, "PYTHONPATH": str(Path(native_pi.__file__).parents[1])},
            session_dir,
            session_file,
        )

    monkeypatch.setattr(native_pi, "prepare_native_pi_rpc_launch", launch)
    return session_dir, session_file


def _namespace_members(inode: int) -> list[int]:
    members = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if os.stat(entry / "ns/pid").st_ino == inode:
                members.append(int(entry.name))
        except (OSError, FileNotFoundError):
            continue
    return members


@pytest.mark.skipif(sys.platform != "linux", reason="Linux user+PID namespaces required")
async def test_actual_native_pi_runner_returns_one_use_receipt_after_fake_namespace_retires(
    tmp_path: Path, monkeypatch
) -> None:
    session_dir, session_file = _prepare(tmp_path, monkeypatch)
    result = await native_pi.run_native_pi_turn(
        tmp_path,
        input_id=_INPUT,
        prompt=_PROMPT,
        worktree=tmp_path,
        session_dir=session_dir,
        session_file=session_file,
        timeout=10,
        prompt_send_boundary=lambda: nullcontext(),
        retirement_identity=_identity(session_file),
    )
    assert result.text == "DONE" and result.context.input_id == _INPUT
    receipt = result.retirement_receipt
    assert receipt is not None
    record = receipt.take_once(_identity(session_file))
    assert record["session_dev_ino"] == [session_file.stat().st_dev, session_file.stat().st_ino]
    assert _namespace_members(record["namespace_inode"]) == []
    assert (session_dir / "native-retirement-terminal" / f"{_INPUT}.json").exists()
    assert record["session_id"] == "sid"
    with pytest.raises(RetirementUnavailable, match="already used"):
        receipt.take_once(_identity(session_file))


@pytest.mark.skipif(sys.platform != "linux", reason="Linux user+PID namespaces required")
async def test_inode_replacement_returns_no_retirement_receipt(tmp_path: Path, monkeypatch) -> None:
    session_dir, session_file = _prepare(tmp_path, monkeypatch, mutate_inode=True)
    result = await native_pi.run_native_pi_turn(
        tmp_path,
        input_id=_INPUT,
        prompt=_PROMPT,
        worktree=tmp_path,
        session_dir=session_dir,
        session_file=session_file,
        timeout=10,
        retirement_identity=_identity(session_file),
    )
    assert result.context.input_id == _INPUT and result.retirement_receipt is None
    assert not (session_dir / "native-retirement-terminal" / f"{_INPUT}.json").exists()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux user+PID namespaces required")
async def test_cancelled_fake_rpc_never_publishes_terminal_receipt(
    tmp_path: Path, monkeypatch
) -> None:
    session_dir, session_file = _prepare(tmp_path, monkeypatch, hang=True)
    spawned = []
    original = native_pi.spawn_retired_child

    async def capture(*args):
        child = await original(*args)
        spawned.append(child.namespace_inode)
        return child

    monkeypatch.setattr(native_pi, "spawn_retired_child", capture)
    started = asyncio.create_task(
        native_pi.run_native_pi_turn(
            tmp_path,
            input_id=_INPUT,
            prompt=_PROMPT,
            worktree=tmp_path,
            session_dir=session_dir,
            session_file=session_file,
            timeout=10,
            retirement_identity=_identity(session_file),
        )
    )

    # Cancel only after the fake has observed the actual prompt. The boundary
    # remains UNKNOWN, and all descendants must retire despite cancellation.
    async def sent() -> None:
        while not Path(str(session_file) + ".prompt-seen").exists():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(sent(), timeout=5)
    started.cancel()
    with pytest.raises(asyncio.CancelledError):
        await started
    assert len(spawned) == 1 and _namespace_members(spawned[0]) == []
    assert not (session_dir / "native-retirement-terminal" / f"{_INPUT}.json").exists()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux user+PID namespaces required")
async def test_terminal_parent_fsync_failure_returns_no_receipt(
    tmp_path: Path, monkeypatch
) -> None:
    from agent_comms import native_pi_retirement as retirement

    session_dir, session_file = _prepare(tmp_path, monkeypatch)
    original = retirement._sync_directory

    def fail_terminal(path: Path) -> None:
        if path.name == "native-retirement-terminal":
            raise OSError("simulated failed parent fsync")
        original(path)

    monkeypatch.setattr(retirement, "_sync_directory", fail_terminal)
    result = await native_pi.run_native_pi_turn(
        tmp_path,
        input_id=_INPUT,
        prompt=_PROMPT,
        worktree=tmp_path,
        session_dir=session_dir,
        session_file=session_file,
        timeout=10,
        retirement_identity=_identity(session_file),
    )
    assert result.text == "DONE" and result.retirement_receipt is None
    # A visible row after lost fsync/return is not terminal authority.
    assert (session_dir / "native-retirement-terminal" / f"{_INPUT}.json").exists()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux user+PID namespaces required")
async def test_wrong_source_digest_refuses_before_child_launch(tmp_path: Path, monkeypatch) -> None:
    session_dir, session_file = _prepare(tmp_path, monkeypatch)
    identity = _identity(session_file, prompt="different prompt")
    with pytest.raises(native_pi.NativePiUnavailable, match="child isolation is unavailable"):
        await native_pi.run_native_pi_turn(
            tmp_path,
            input_id=_INPUT,
            prompt=_PROMPT,
            worktree=tmp_path,
            session_dir=session_dir,
            session_file=session_file,
            timeout=10,
            retirement_identity=identity,
        )
    assert not (session_dir / "native-retirement-terminal").exists()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux user+PID namespaces required")
def test_controller_death_kills_fake_rpc_and_setsid_tool(tmp_path: Path) -> None:
    session_dir = tmp_path / "sessions"
    session_dir.mkdir(mode=0o700)
    session_file = session_dir / "one.jsonl"
    session_file.write_text(json.dumps({"type": "session", "id": "sid"}) + "\n")
    session_file.chmod(0o600)
    marker = tmp_path / "fake-started"
    controller = tmp_path / "controller.py"
    controller.write_text("""import asyncio,json,os,sys,time
from pathlib import Path
from agent_comms.native_pi import NativePiRpcLaunch
from agent_comms.native_pi_retirement import spawn_retired_child
from test_native_pi_retirement import _identity,_INPUT,_PROMPT
async def run():
    session_file = Path(sys.argv[1]); marker = Path(sys.argv[2])
    fake = ('import os,signal,subprocess,pathlib;'
      'subprocess.Popen([__import__(\"sys\").executable,\"-c\",'
      '\"import signal;signal.pause()\"],start_new_session=True);'
      'pathlib.Path('+repr(str(marker))+').write_text(\"started\");signal.pause()')
    launch = NativePiRpcLaunch((sys.executable,'-c',fake),marker.parent,
        os.environ.copy(),session_file.parent,session_file)
    child = await spawn_retired_child(launch,_identity(session_file),_INPUT,_PROMPT)
    deadline = time.monotonic()+3
    while not marker.exists() and time.monotonic()<deadline: time.sleep(0.01)
    if not marker.exists(): raise RuntimeError('fake child did not start')
    print(json.dumps({'init_pid':child.init_pid,'namespace':child.namespace_inode,
      'wrapper_pid':child.process.pid}),flush=True)
    await asyncio.Event().wait()
asyncio.run(run())
""")
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        (str(Path(native_pi.__file__).parents[1]), str(Path(__file__).parent))
    )
    process = subprocess.Popen(
        [sys.executable, "-u", str(controller), str(session_file), str(marker)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        env=env,
    )
    assert process.stdout is not None
    ready = select.select([process.stdout], [], [], 5)[0]
    assert ready, "controller never armed a namespace init"
    record = json.loads(process.stdout.readline())
    init_pid = record["init_pid"]
    init_pidfd = _pidfd_open(init_pid)
    try:
        assert marker.read_text() == "started"
        assert _namespace_members(record["namespace"])
        os.kill(process.pid, signal.SIGKILL)  # only this disposable controller
        process.wait(timeout=3)
        watcher = select.poll()
        watcher.register(init_pidfd, select.POLLIN)
        assert watcher.poll(3000)
        # pidfd readability precedes host init reaping under scheduler load;
        # parent death never returns a receipt even during this zombie window.
        deadline = time.monotonic() + 3
        while Path(f"/proc/{init_pid}").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not Path(f"/proc/{init_pid}").exists()
        assert _namespace_members(record["namespace"]) == []
        assert not (session_dir / "native-retirement-terminal").exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
        if Path(f"/proc/{init_pid}").exists():
            _pidfd_kill(init_pidfd)
        os.close(init_pidfd)
