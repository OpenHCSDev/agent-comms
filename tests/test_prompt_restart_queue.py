"""The external prompt queue must not restart an active or changed owner."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "prompt_restart_queue.py"
spec = importlib.util.spec_from_file_location("prompt_restart_queue", SCRIPT)
queue = importlib.util.module_from_spec(spec)
spec.loader.exec_module(queue)


def _fixture(tmp_path, monkeypatch):
    root = tmp_path / "project"
    prompt = root / ".pi" / "APPEND_SYSTEM.md"
    prompt.parent.mkdir(parents=True)
    prompt.write_text("reviewed project policy")
    import hashlib

    digest = hashlib.sha256(prompt.read_bytes()).hexdigest()
    entry = {
        "name": "owner",
        "pid": 401,
        "created_at": 100.5,
        "epoch": 7,
        "worktree": str(root),
        "policy_sha256": digest,
        "state": "pending",
    }
    path = tmp_path / "queue.json"
    path.write_text(
        json.dumps({"version": 1, "root": str(tmp_path), "entries": [entry], "excluded": []})
    )
    owner = SimpleNamespace(
        name="owner", pid=401, created_at=100.5, worktree=str(root), active_turn=object()
    )
    state = SimpleNamespace(active=True)

    def snapshot():
        return SimpleNamespace(
            threads={"owner": owner}, statuses={"owner": state}, admission_generations={"owner": 7}
        )

    calls = []

    def restart(names, **kwargs):
        calls.append((names, kwargs, os.environ.copy()))
        return (SimpleNamespace(previous_pid=401, pid=501),)

    comms = SimpleNamespace(
        registry=SimpleNamespace(snapshot=snapshot),
        owners=SimpleNamespace(
            restart_owners=restart,
            _is_local_participant=lambda thread, wait=False: True,
        ),
    )
    monkeypatch.setattr(queue, "wire", lambda _: comms)
    monkeypatch.setattr(
        queue,
        "_owner_environment",
        lambda pid: (
            {
                "AGENT_COMMS_THREAD": "owner",
                "AGENT_COMMS_AGENT_BIN": "/native/pi",
                "PYTHONPATH": "/original/site",
                "CUSTOM_OWNER": "preserved",
            },
            "/stack/python",
        ),
    )
    return path, prompt, owner, calls


def test_active_turn_is_deferred_then_exact_incarnation_restarted(tmp_path, monkeypatch):
    path, _prompt, owner, calls = _fixture(tmp_path, monkeypatch)
    assert queue.step(path)["entries"][0]["state"] == "pending"
    assert not calls
    owner.active_turn = None
    result = queue.step(path)["entries"][0]
    assert result["state"] == "restarted"
    assert result["old_pid"] == 401 and result["new_pid"] == 501
    assert len(calls) == 1
    names, kwargs, child_env = calls[0]
    assert names == ["owner"]
    assert kwargs == {
        "agent_bin": "/native/pi",
        "agent_args": None,
        "expected_incarnations": {"owner": (401, 100.5, 7)},
    }
    assert child_env["PYTHONPATH"] == "/original/site"
    assert child_env["AGENT_COMMS_AGENT_BIN"] == "/native/pi"
    assert "AGENT_COMMS_AGENT_ARGS" not in child_env
    assert child_env["CUSTOM_OWNER"] == "preserved"
    assert os.environ.get("PYTHONPATH") != "/original/site"
    assert queue.step(path)["entries"][0]["state"] == "restarted"
    assert len(calls) == 1


def test_changed_owner_or_prompt_never_restarted(tmp_path, monkeypatch):
    path, prompt, owner, calls = _fixture(tmp_path, monkeypatch)
    owner.active_turn = None
    owner.pid = 402
    assert queue.step(path)["entries"][0]["state"] == "stale"
    assert not calls
    other = tmp_path / "other"
    other.mkdir()
    path, prompt, owner, calls = _fixture(other, monkeypatch)
    owner.active_turn = None
    prompt.write_text("new policy")
    assert queue.step(path)["entries"][0]["state"] == "blocked"
    assert not calls


def test_exact_owner_environment_does_not_leak_between_restarts(tmp_path, monkeypatch):
    path, _prompt, owner, calls = _fixture(tmp_path, monkeypatch)
    monkeypatch.setenv("AGENT_COMMS_AGENT_ARGS", "--provider wrong --model wrong")
    monkeypatch.setenv("PYTHONPATH", "/isolated/branch/src")
    queue._restart_exact_owner(queue.wire(str(tmp_path)), owner, (401, 100.5, 7))
    assert "AGENT_COMMS_AGENT_ARGS" not in calls[0][2]
    assert os.environ["AGENT_COMMS_AGENT_ARGS"] == "--provider wrong --model wrong"
    original = {
        "AGENT_COMMS_THREAD": "owner",
        "AGENT_COMMS_AGENT_BIN": "/native/pi",
        "AGENT_COMMS_AGENT_ARGS": "--provider original --model configured",
        "PYTHONPATH": "/different/original",
    }
    monkeypatch.setattr(queue, "_owner_environment", lambda pid: (original, "/stack/python"))
    queue._restart_exact_owner(queue.wire(str(tmp_path)), owner, (401, 100.5, 7))
    assert calls[1][1]["agent_args"] == ["--provider", "original", "--model", "configured"]
    assert calls[1][2]["PYTHONPATH"] == "/different/original"
    assert os.environ["PYTHONPATH"] == "/isolated/branch/src"
    monkeypatch.setattr(
        queue,
        "_owner_environment",
        lambda pid: ({**original, "AGENT_COMMS_THREAD": "reused-pid"}, "/stack/python"),
    )
    with pytest.raises(ValueError, match="different thread"):
        queue._restart_exact_owner(queue.wire(str(tmp_path)), owner, (401, 100.5, 7))
    assert len(calls) == 2


def test_owner_interpreter_must_match_runner(monkeypatch):
    original_read = Path.read_bytes

    def fake_read(path):
        if str(path).endswith("/cmdline"):
            return b"/other/python\0-m\0agent_comms.worker\0"
        if str(path).endswith("/environ"):
            return b"AGENT_COMMS_AGENT_BIN=/native/pi\0AGENT_COMMS_THREAD=owner\0"
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", fake_read)
    monkeypatch.setattr(os.path, "samefile", lambda a, b: False)
    with pytest.raises(ValueError, match="Runner interpreter differs"):
        queue._owner_environment(401)


def test_uncertain_restart_never_retried(tmp_path, monkeypatch):
    path, _prompt, owner, _calls = _fixture(tmp_path, monkeypatch)
    owner.active_turn = None

    def uncertain(names, **kwargs):
        raise RuntimeError("owner stop outcome unknown")

    monkeypatch.setattr(queue.wire(str(tmp_path)).owners, 'restart_owners', uncertain)
    # The wire factory is a lambda, returning the same fake comms object.
    with pytest.raises(RuntimeError, match="outcome UNKNOWN"):
        queue.step(path)
    assert json.loads(path.read_text())["entries"][0]["state"] == "uncertain"
    assert queue.step(path)["entries"][0]["state"] == "uncertain"
