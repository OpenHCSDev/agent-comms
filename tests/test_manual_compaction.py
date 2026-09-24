"""No-provider Pi-RPC-shaped tests for explicit idle manual compaction."""

import asyncio
import json
import os
import signal
import subprocess
import sys
from contextlib import suppress
from pathlib import Path

import pytest

from agent_comms.manual_compaction import MAX_SUMMARY_CHARS, compact_session

pytestmark = pytest.mark.skipif(os.name == "nt", reason="Executable stub and POSIX PID proof")


def _stub(tmp_path: Path, monkeypatch, mode: str) -> tuple[str, Path, Path]:
    script = tmp_path / "pi-compact-stub"
    pid_file = tmp_path / "pid"
    capture = tmp_path / "command.json"
    monkeypatch.setenv("MODE", mode)
    monkeypatch.setenv("PID_FILE", str(pid_file))
    monkeypatch.setenv("CAPTURE_FILE", str(capture))
    monkeypatch.setenv("GRANDCHILD_PID_FILE", str(tmp_path / "grandchild.pid"))
    script.write_text(
        f"#!{sys.executable}\n"
        + "import json, os, pathlib, signal, subprocess, sys, time\n"
        + "pathlib.Path(os.environ['PID_FILE']).write_text(str(os.getpid()))\n"
        + "mode = os.environ['MODE']\n"
        + "assert sys.argv[1:3] == ['--mode', 'rpc']\n"
        + "assert sys.argv[3] == '--session' and pathlib.Path(sys.argv[4]).is_file()\n"
        + "command = json.loads(sys.stdin.readline())\n"
        + "pathlib.Path(os.environ['CAPTURE_FILE']).write_text(json.dumps(command))\n"
        + "assert command['type'] == 'compact'\n"
        + "assert command['id'].startswith('agent-comms-compact-')\n"
        + "emit = lambda payload: print(json.dumps(payload), flush=True)\n"
        + "if mode == 'hang' or mode == 'reply_hang':\n"
        + "    signal.signal(signal.SIGTERM, lambda *_: None)\n"
        + "if mode == 'hang':\n"
        + "    while True: time.sleep(0.1)\n"
        + "if mode == 'orphan':\n"
        + "    child = (\n"
        + "        'import os,signal,time; signal.signal(signal.SIGTERM, lambda *_: None); '\n"
        + '        \'open(os.environ["GRANDCHILD_PID_FILE"],"w").write(str(os.getpid())); \'\n'
        + "        'time.sleep(100)'\n"
        + "    )\n"
        + "    subprocess.Popen([sys.executable, '-c', child], stdout=sys.stdout,\n"
        + "                     stderr=sys.stderr)\n"
        + "    for _ in range(100):\n"
        + "        if pathlib.Path(os.environ['GRANDCHILD_PID_FILE']).exists(): break\n"
        + "        time.sleep(0.01)\n"
        + "    sys.exit(0)\n"
        + "if mode == 'oversize': print('x' * 70000, flush=True)\n"
        + "if mode == 'malformed': print('{not json', flush=True)\n"
        + "if mode == 'foreign': emit({'type':'response','command':'compact',\n"
        + "                           'id':'foreign','success':True,'data':{'summary':'fake'}})\n"
        + "if mode in ('success', 'failure', 'reply_hang'):\n"
        + "    emit({'type':'response','command':'compact','id':command['id'],\n"
        + "          'success':mode != 'failure',\n"
        + "          'error':'PRIVATE-ERROR-TOKEN',\n"
        + "          'data':{'summary':'safe\\n\\x1b[31m' + 'z'*3000,\n"
        + "                  'tokensBefore':500,'estimatedTokensAfter':120,\n"
        + "                  'details':{'secret':'PRIVATE-DETAIL-TOKEN'}}})\n"
        + "if mode in ('failure','malformed','oversize'):\n"
        + "    print('PRIVATE-STDERR-TOKEN', file=sys.stderr, flush=True)\n"
        + "if mode == 'reply_hang':\n"
        + "    while True: time.sleep(0.1)\n"
    )
    script.chmod(0o700)
    return str(script), pid_file, capture


def _session(tmp_path: Path) -> str:
    session = tmp_path / "saved.jsonl"
    session.write_text("")
    return str(session)


def _reaped(pid_file: Path) -> bool:
    pid = int(pid_file.read_text())
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


async def test_manual_compaction_exact_id_bounded_result_and_no_raw_private_data(
    tmp_path, monkeypatch
):
    stub, pid_file, capture = _stub(tmp_path, monkeypatch, "success")
    result = await compact_session(
        stub, [], _session(tmp_path), str(tmp_path), "Focus on user changes", timeout_seconds=2
    )
    assert result["ok"] is True
    assert result["tokensBefore"] == 500
    assert result["estimatedTokensAfter"] == 120  # explicitly an estimate, never context_used
    assert len(result["summary"]) <= MAX_SUMMARY_CHARS
    assert "\x1b" not in result["summary"]
    assert "PRIVATE" not in repr(result)
    command = json.loads(capture.read_text())
    assert command["customInstructions"] == "Focus on user changes"
    assert len(command["id"].split("-")[-1]) == 32
    assert _reaped(pid_file)


@pytest.mark.parametrize(
    ("mode", "error"),
    [
        ("failure", "Compaction failed; inspect local diagnostics."),
        ("eof", "Compaction ended without a matching response."),
        ("foreign", "Compaction ended without a matching response."),
        ("malformed", "Compaction returned an invalid response."),
        ("oversize", "Compaction transport failed."),
    ],
)
async def test_compaction_never_borrows_other_responses_or_leaks_stderr(
    tmp_path, monkeypatch, mode, error
):
    stub, pid_file, _ = _stub(tmp_path, monkeypatch, mode)
    result = await compact_session(stub, [], _session(tmp_path), str(tmp_path), timeout_seconds=2)
    assert result == {"ok": False, "error": error}
    assert "PRIVATE" not in repr(result)
    assert _reaped(pid_file)


async def test_timeout_stops_process_group_and_reaps_child(tmp_path, monkeypatch):
    stub, pid_file, _ = _stub(tmp_path, monkeypatch, "hang")
    result = await compact_session(stub, [], _session(tmp_path), str(tmp_path), timeout_seconds=0.2)
    assert result == {"ok": False, "error": "Compaction timed out."}
    assert _reaped(pid_file)


async def test_exited_pi_leader_does_not_leave_stdout_inheriting_child_alive(tmp_path, monkeypatch):
    stub, pid_file, _ = _stub(tmp_path, monkeypatch, "orphan")
    child_file = tmp_path / "grandchild.pid"
    result = await compact_session(stub, [], _session(tmp_path), str(tmp_path), timeout_seconds=0.2)
    assert result == {"ok": False, "error": "Compaction timed out."}
    assert _reaped(pid_file)
    assert child_file.exists()
    child_pid = int(child_file.read_text())
    try:
        status = subprocess.run(
            ["/bin/ps", "-p", str(child_pid), "-o", "state="],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        assert status.returncode != 0 or status.stdout.strip().startswith("Z")
    finally:
        # A failing test must never strand its deliberately stubborn child.
        with suppress(ProcessLookupError):
            os.kill(child_pid, signal.SIGKILL)


async def test_success_response_without_clean_process_exit_is_not_success(tmp_path, monkeypatch):
    stub, pid_file, _ = _stub(tmp_path, monkeypatch, "reply_hang")
    result = await compact_session(stub, [], _session(tmp_path), str(tmp_path), timeout_seconds=2)
    assert result == {"ok": False, "error": "Compaction did not finish cleanly."}
    assert _reaped(pid_file)


async def test_cancellation_reaps_child_without_replaying(tmp_path, monkeypatch):
    stub, pid_file, capture = _stub(tmp_path, monkeypatch, "hang")
    task = asyncio.create_task(
        compact_session(stub, [], _session(tmp_path), str(tmp_path), timeout_seconds=5)
    )
    async with asyncio.timeout(2):
        while not capture.exists():
            await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=4)
    assert _reaped(pid_file)
    assert json.loads(capture.read_text())["type"] == "compact"  # exactly one command


@pytest.mark.parametrize(
    "args",
    [
        ["--"],  # Pi stops option parsing before the runner's saved-session flags.
        ["--no-session"],
        ["--session", "other.jsonl"],
        ["--session-id", "other"],
        ["--session-dir", "/tmp/other"],
        ["--fork", "other.jsonl"],
        ["--continue"],
        ["-c"],
        ["--resume"],
        ["-r"],
        ["--no-session=true"],
    ],
)
async def test_session_overrides_are_rejected_before_pi_can_claim_ephemeral_success(
    tmp_path, monkeypatch, args
):
    stub, pid_file, _ = _stub(tmp_path, monkeypatch, "success")
    result = await compact_session(stub, args, _session(tmp_path), str(tmp_path))
    assert result == {
        "ok": False,
        "error": "Compaction arguments may not change the saved session.",
    }
    assert not pid_file.exists()


async def test_preflight_refuses_unsupported_backend_missing_session_and_bad_instructions(
    tmp_path, monkeypatch
):
    stub, pid_file, _ = _stub(tmp_path, monkeypatch, "success")
    missing = str(tmp_path / "no-session.jsonl")
    assert (await compact_session(stub, [], missing, str(tmp_path)))["ok"] is False
    assert (await compact_session("/bin/echo", [], _session(tmp_path), str(tmp_path)))[
        "ok"
    ] is False
    bad = await compact_session(
        stub, [], _session(tmp_path), str(tmp_path), "x" * 4097, timeout_seconds=2
    )
    assert bad == {"ok": False, "error": "Compaction instructions are invalid or too long."}
    missing_worktree = await compact_session(
        stub, [], _session(tmp_path), str(tmp_path / "moved"), timeout_seconds=2
    )
    assert missing_worktree == {
        "ok": False,
        "error": "This thread's project directory is unavailable.",
    }
    assert not pid_file.exists()
