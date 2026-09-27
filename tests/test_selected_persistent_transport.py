"""Provider-free transport controls; NOT private claim or generic-tool authority."""

from __future__ import annotations

import os
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from agent_comms import backend
from agent_comms.selected_persistent_prompt import SelectedPersistentPrompt

pytestmark = pytest.mark.skipif(os.name != "posix", reason="raw Pi pipe is POSIX-only")


def _fake_pi(root: Path) -> str:
    session = root / "session.jsonl"
    session.write_text("session\n")
    pid_log = root / "pid-log"
    prompt_log = root / "prompt-log"
    source = root / "pi-stub"
    source.write_text(
        f"#!{sys.executable}\n"
        + """
import json, os, sys
emit = lambda row: print(json.dumps(row), flush=True)
with open(@@LOG@@, 'a') as log: log.write(str(os.getpid()) + '\\n')
for line in sys.stdin:
    command = json.loads(line)
    kind = command['type']
    if kind == 'get_state':
        emit({{'type':'response','command':kind,'id':command['id'],'success':True,
              'data':{{'nativeInputProofCapability':'pi-native-input-v1-live-only',
                      'sessionId':'fixed-session','sessionFile':@@SESSION@@}}}})
    elif kind == 'prompt':
        with open(@@PROMPTS@@, 'a') as log: log.write(command['inputId'] + '\\n')
        emit({{'type':'response','command':kind,'id':command['id'],'success':True}})
        emit({{'type':'message_start','message':{{'role':'user',
              'content':command['message'],'inputId':command['inputId']}}}})
        if command['message'] == 'selected safe tool':
            emit({{'type':'tool_execution_start','toolCallId':'call-1',
                  'toolName':'read','args':{{'path':'safe.txt'}}}})
            emit({{'type':'tool_execution_end','toolCallId':'call-1',
                  'toolName':'read','result':{{'content':[{{'type':'text','text':'safe'}}]}}}})
        emit({{'type':'message_update','assistantMessageEvent':
              {{'type':'text_delta','delta':'ok'}}}})
        emit({{'type':'message_end','message':{{'role':'assistant','stopReason':'stop'}}}})
        emit({{'type':'agent_settled'}})
    elif kind == 'get_session_stats':
        emit({{'type':'response','command':kind,'id':command['id'],'success':True,
              'data':{{'contextUsage':{{'tokens':10}}}}}})
""".replace("{{", "{")
        .replace("}}", "}")
        .replace("@@LOG@@", repr(str(pid_log)))
        .replace("@@PROMPTS@@", repr(str(prompt_log)))
        .replace("@@SESSION@@", repr(str(session)))
    )
    source.chmod(0o755)
    return str(source)


async def _run(
    stub: str,
    root: Path,
    persistent: backend.PersistentPiSession,
    text: str,
    selected: SelectedPersistentPrompt | None = None,
    args: tuple[str, ...] = (),
    cwd: Path | None = None,
):
    return [
        event
        async for event in backend.stream_agent_events(
            stub,
            args,
            text,
            str(cwd or root),
            session_file=str(root / "session.jsonl"),
            persistent_session=persistent,
            selected_prompt=selected,
        )
    ]


@pytest.mark.asyncio
async def test_selected_exact_input_uses_same_live_child_and_one_raw_writer(tmp_path):
    stub = _fake_pi(tmp_path)
    persistent = backend.PersistentPiSession()
    once = threading.Lock()
    calls: list[int] = []

    @contextmanager
    def boundary():
        if not once.acquire(blocking=False):
            raise RuntimeError("selected reservation already consumed")
        calls.append(threading.get_ident())
        yield

    boundary._maintenance_wire_locked = True
    input_id = "a" * 32
    try:
        baseline = await _run(stub, tmp_path, persistent, "ordinary")
        assert baseline[-1]["ok"] is True
        child = persistent.proc
        assert child is not None and child.returncode is None
        selected = await _run(
            stub,
            tmp_path,
            persistent,
            "selected safe tool",
            SelectedPersistentPrompt(input_id, boundary),
        )
        assert selected[-1]["ok"] is True
        assert any(
            event.get("type") == "tool_start" and event["name"] == "read" for event in selected
        )
        assert persistent.proc is child
        assert (tmp_path / "pid-log").read_text().splitlines() == [str(child.pid)]
        assert len(calls) == 1 and calls[0] != threading.get_ident()
        assert (tmp_path / "prompt-log").read_text().splitlines()[-1] == input_id
        # A duplicate caller cannot reuse the one-use final send boundary,
        # even while it still has the same input ID and saved Pi session.
        duplicate = await _run(
            stub,
            tmp_path,
            persistent,
            "selected safe tool",
            SelectedPersistentPrompt(input_id, boundary),
        )
        assert duplicate[-1]["ok"] is False
        assert (tmp_path / "pid-log").read_text().splitlines() == [str(child.pid)]
        assert child.returncode is not None
        assert len((tmp_path / "prompt-log").read_text().splitlines()) == 2
    finally:
        await persistent.close_idle()


@pytest.mark.asyncio
async def test_selected_refuses_existing_no_tools_child_without_closing_it(tmp_path):
    stub = _fake_pi(tmp_path)
    persistent = backend.PersistentPiSession()

    @contextmanager
    def boundary():
        raise AssertionError("no-tools child cannot receive a selected input")
        yield

    boundary._maintenance_wire_locked = True
    try:
        baseline = await _run(stub, tmp_path, persistent, "ordinary", args=("--no-tools",))
        assert baseline[-1]["ok"] is True
        child = persistent.proc
        assert child is not None
        denied = await _run(
            stub,
            tmp_path,
            persistent,
            "selected safe tool",
            SelectedPersistentPrompt("c" * 32, boundary),
            args=("--no-tools",),
        )
        assert denied[-1]["reason_code"] == "selected_persistent_owner_unavailable"
        assert persistent.proc is child and child.returncode is None
        assert (tmp_path / "prompt-log").read_text().splitlines() != ["c" * 32]
        assert len((tmp_path / "pid-log").read_text().splitlines()) == 1
    finally:
        await persistent.close_idle()


@pytest.mark.asyncio
async def test_selected_refuses_missing_owner_before_spawn(tmp_path):
    stub = _fake_pi(tmp_path)
    persistent = backend.PersistentPiSession()

    @contextmanager
    def boundary():
        raise AssertionError("must not send or call admission")
        yield

    boundary._maintenance_wire_locked = True
    try:
        refused = await _run(
            stub,
            tmp_path,
            persistent,
            "selected safe tool",
            SelectedPersistentPrompt("b" * 32, boundary),
        )
        assert refused[-1]["reason_code"] == "selected_persistent_owner_unavailable"
        assert not (tmp_path / "pid-log").exists()
        assert persistent.proc is None
    finally:
        await persistent.close_idle()


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["session_revision", "profile", "worktree"])
async def test_selected_refuses_stale_session_or_profile_without_replacement(tmp_path, drift):
    stub = _fake_pi(tmp_path)
    persistent = backend.PersistentPiSession()

    @contextmanager
    def boundary():
        raise AssertionError("stale selected owner must never receive bytes")
        yield

    boundary._maintenance_wire_locked = True
    try:
        baseline = await _run(stub, tmp_path, persistent, "ordinary")
        assert baseline[-1]["ok"] is True
        child = persistent.proc
        assert child is not None and child.returncode is None
        args = ()
        cwd = tmp_path
        if drift == "session_revision":
            (tmp_path / "session.jsonl").write_text("outside writer changed branch\n")
        elif drift == "profile":
            args = ("--model", "other")
        else:
            cwd = tmp_path / "other-worktree"
            cwd.mkdir()
        denied = await _run(
            stub,
            tmp_path,
            persistent,
            "selected safe tool",
            SelectedPersistentPrompt("e" * 32, boundary),
            args=args,
            cwd=cwd,
        )
        assert denied[-1]["reason_code"] == "selected_persistent_owner_unavailable"
        assert persistent.proc is child and child.returncode is None
        assert len((tmp_path / "prompt-log").read_text().splitlines()) == 1
        assert (tmp_path / "pid-log").read_text().splitlines() == [str(child.pid)]
    finally:
        await persistent.close_idle()


def test_selected_input_rejects_unreserved_shape():
    with pytest.raises(ValueError):
        SelectedPersistentPrompt("not-an-input-id", lambda: None)
    with pytest.raises(ValueError, match="canonical wire exclusion"):
        SelectedPersistentPrompt("d" * 32, lambda: None)
