"""No-provider negative gates for the saved-session Pi compact runner."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path

import pytest

from agent_comms import backend as coding_backend
from agent_comms import manual_compaction as compact


async def test_malformed_middle_session_row_refused_before_pi_preflight(tmp_path, monkeypatch):
    session = tmp_path / "session.jsonl"
    session.write_bytes(
        (json.dumps({"type": "session", "version": 3, "id": "session"}) + "\n").encode()
        + b'{"type":"message","id":\n'
        + (json.dumps({"type": "message", "id": "last"}) + "\n").encode()
    )
    backend = tmp_path / "pi-stub"
    backend.write_text("")
    monkeypatch.setattr(compact, "_pinned_package", lambda: tmp_path)
    profile = tmp_path / "profile"
    profile.mkdir()
    monkeypatch.setattr(compact, "_private_policy", lambda **_kwargs: profile)

    async def forbidden_preflight(*args, **kwargs):
        raise AssertionError("Malformed JSONL reached Pi preflight")

    monkeypatch.setattr(compact, "_preflight", forbidden_preflight)
    result = await compact.ManualCompaction(
        str(backend),
        ["--provider", "openrouter", "--model", "fake"],
        str(session),
        str(tmp_path),
    ).run()
    assert result["ok"] is False


@pytest.mark.skipif(os.name != "posix", reason="POSIX session writer lock")
async def test_compaction_waits_for_session_writer_before_preflight(tmp_path, monkeypatch):
    import fcntl

    session = tmp_path / "session.jsonl"
    session.write_text(json.dumps({"type": "session", "version": 3, "id": "session"}) + "\n")
    backend = tmp_path / "pi-stub"
    backend.write_text("")
    monkeypatch.setattr(compact, "_pinned_package", lambda: tmp_path)
    profile = tmp_path / "profile"
    profile.mkdir()
    monkeypatch.setattr(compact, "_private_policy", lambda **_kwargs: profile)
    preflight_started = asyncio.Event()

    async def preflight(*args, **kwargs):
        preflight_started.set()
        return None

    monkeypatch.setattr(compact, "_preflight", preflight)
    lock_file = session.with_name(f".{session.name}.agent-comms-writer.lock")
    with lock_file.open("a+b") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        task = asyncio.create_task(
            compact.ManualCompaction(
                str(backend),
                ["--provider", "openrouter", "--model", "fake"],
                str(session),
                str(tmp_path),
            ).run()
        )
        await asyncio.sleep(0.1)
        assert not preflight_started.is_set()
        fcntl.flock(held.fileno(), fcntl.LOCK_UN)
    result = await asyncio.wait_for(task, 2)
    assert preflight_started.is_set()
    assert result["ok"] is False


def test_concurrent_session_append_cannot_be_reported_as_compaction_success():
    with tempfile.TemporaryDirectory(dir="/var/tmp" if os.name == "posix" else None) as directory:
        session = Path(directory) / "session.jsonl"
        session.write_text(json.dumps({"type": "session", "version": 3, "id": "session"}) + "\n")
        before = compact._session_bytes(session)
        session.write_bytes(
            before
            + (json.dumps({"type": "message", "id": "concurrent"}) + "\n").encode()
            + (json.dumps({"type": "compaction", "id": "saved"}) + "\n").encode()
        )
        with pytest.raises(ValueError, match="concurrent|unexpected"):
            compact._durable_compaction_row(session, before)


@pytest.mark.skipif(os.name != "posix", reason="POSIX session writer lock")
async def test_backend_writer_waits_while_session_is_fenced(tmp_path):
    import fcntl
    import sys

    session = tmp_path / "session.jsonl"
    session.write_text(json.dumps({"type": "session", "version": 3, "id": "session"}) + "\n")
    marker = tmp_path / "backend-launched"
    child = tmp_path / "text-child"
    child.write_text(
        f"#!{sys.executable}\n"
        + f"import pathlib\npathlib.Path({str(marker)!r}).touch()\nprint('done')\n"
    )
    child.chmod(0o700)
    lock_file = session.with_name(f".{session.name}.agent-comms-writer.lock")
    with lock_file.open("a+b") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        task = asyncio.create_task(
            _collect_backend(coding_backend, str(child), str(tmp_path), str(session))
        )
        await asyncio.sleep(0.1)
        assert not marker.exists()
        fcntl.flock(held.fileno(), fcntl.LOCK_UN)
    events = await asyncio.wait_for(task, 2)
    assert marker.exists()
    assert events[-1].ok is True


async def _collect_backend(module, child: str, cwd: str, session: str):
    return [
        event
        async for event in module.stream_agent_events(
            child, [], "work", cwd, session_file=session, require_input_id=False
        )
    ]
