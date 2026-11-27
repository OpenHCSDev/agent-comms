"""AntigravityAcpBackend: multiplexed single-process ACP execution, WAL-safe forking, and nominal MI."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_comms import agent_events as events
from agent_comms.agent_backend import AgentBackend, Forks, PlacedCompaction, ThreadServingBackend
from agent_comms.execution.antigravity_acp import (
    AntigravityAcpPool,
    AntigravityAcpProcess,
    AntigravityConversationId,
    AntigravitySessionFiles,
    AntigravitySessionStorage,
)
from agent_comms.execution.antigravity_acp_events import (
    ANTIGRAVITY_ACP_EVENT_KINDS,
    AcpTurnStreamState,
    AntigravityAcpUpdate,
)
from agent_comms.execution.antigravity_backend import AntigravityAcpBackend, AntigravitySession
from agent_comms.execution.native_arguments import AntigravityProvider, SelectedModel
from agent_comms.execution.pi_vocabulary import HighThinkingLevel
from agent_comms.execution.runtime_observations import AgentInfo, ContextInfo
from agent_comms.execution.send_outcome import NO_SEND_BOUNDARY
from agent_comms.execution.session_file import RegistrySessionLocation
from agent_comms.execution.turn_inbox import InputContent
from agent_comms.nominal.field_codec import AcpSessionId, FieldCodec
from agent_comms.projects.checkouts import Checkout, LaunchCheckout
from agent_comms.registry.thread_management import ForkSpec, NewThreadSpec
from agent_comms.registry.thread_model import DeclaredModel
from agent_comms.registry.threads import ChosenModel, NativeThread, Thread


_FAKE_ACP_SERVER = """
import json, sys, time, uuid

sessions = {}
for raw in sys.stdin:
    if not raw.strip():
        continue
    msg = json.loads(raw)
    method = msg.get("method")
    req_id = msg.get("id")
    params = msg.get("params") or {}

    def reply(result):
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result}) + "\\n")
        sys.stdout.flush()

    def update(sid, payload):
        sys.stdout.write(json.dumps({
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {"sessionId": sid, "update": payload},
        }) + "\\n")
        sys.stdout.flush()

    if method == "initialize":
        reply({"protocolVersion": 1, "agentCapabilities": {"loadSession": True}})
    elif method == "session/new":
        sid = str(uuid.uuid4())
        sessions[sid] = {"model": "gemini-4-neon", "thought_level": "high", "turns": 0}
        reply({
            "sessionId": sid,
            "configOptions": [
                {"id": "model", "currentValue": "gemini-4-neon", "options": [{"value": "gemini-4-neon", "name": "Gemini 4 Neon"}]},
                {"id": "thought_level", "currentValue": "high", "options": [{"value": "high", "name": "High"}]},
            ],
        })
    elif method == "session/resume":
        sid = params["sessionId"]
        sessions.setdefault(sid, {"model": "gemini-4-neon", "thought_level": "high", "turns": 0})
        reply({
            "sessionId": sid,
            "configOptions": [
                {"id": "model", "currentValue": sessions[sid]["model"], "options": []},
                {"id": "thought_level", "currentValue": sessions[sid]["thought_level"], "options": []},
            ],
        })
    elif method == "session/set_config_option":
        sid = params["sessionId"]
        sessions.setdefault(sid, {})[params["configId"]] = params["value"]
        reply({})
    elif method == "session/prompt":
        sid = params["sessionId"]
        if sid not in sessions:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32602, "message": f"Session not found: {sid}"}}) + "\\n")
            sys.stdout.flush()
            continue
        sessions[sid]["turns"] = sessions[sid].get("turns", 0) + 1
        text = " ".join(b.get("text", "") for b in params.get("prompt", []) if b.get("type") == "text")
        update(sid, {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": f"thinking:{sid[:6]}"}})
        update(sid, {"sessionUpdate": "tool_call", "toolCallId": f"tc-{sid[:6]}", "title": "run_command", "name": "run_command", "rawInput": {"CommandLine": "echo ok"}})
        update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": f"tc-{sid[:6]}", "name": "run_command", "status": "completed", "rawOutput": {"output": "ok", "exit_code": 0}, "content": [{"type": "content", "content": {"type": "text", "text": "ok"}}]})
        update(sid, {"sessionUpdate": "usage_update", "used": 1200, "size": 1048576})
        update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": f"reply:{text}:{sessions[sid]['model']}"}})
        reply({"stopReason": "end_turn"})
    elif method == "session/cancel":
        pass
    elif method == "session/close":
        sessions.pop(params.get("sessionId"), None)
        reply({})
"""


def _make_fake_antigravity_db(storage: AntigravitySessionStorage, *, title: str = "Parent") -> None:
    storage.db_path.parent.mkdir(parents=True, exist_ok=True)
    storage.annotations_path.parent.mkdir(parents=True, exist_ok=True)
    (storage.brain_dir / ".system_generated/logs").mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(storage.db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE trajectory_meta (trajectory_id text PRIMARY KEY, cascade_id text, trajectory_type integer, source integer)"
    )
    conn.execute("CREATE TABLE steps (idx integer PRIMARY KEY, payload text)")
    conn.execute(
        "INSERT INTO trajectory_meta VALUES (?, ?, 4, 17)",
        ("orig-traj-id", storage.conversation_id),
    )
    conn.execute("INSERT INTO steps VALUES (0, 'step-zero'), (1, 'step-one')")
    conn.commit()
    conn.close()
    storage.annotations_path.write_text(f'title:"{title}"\n', encoding="utf-8")
    (storage.brain_dir / ".system_generated/logs/transcript.jsonl").write_text(
        json.dumps({"step_index": 0, "type": "USER_INPUT", "content": "hello"}) + "\n",
        encoding="utf-8",
    )
    (storage.brain_dir / ".agy-thread.json").write_text('{"thread":"old"}\n', encoding="utf-8")


def test_antigravity_backend_nominal_registration_and_provider_dispatch() -> None:
    assert issubclass(AntigravityAcpBackend, Forks)
    assert issubclass(AntigravityAcpBackend, ThreadServingBackend)
    assert AgentBackend.member_named("antigravity_acp") is AntigravityAcpBackend
    assert AgentBackend.for_provider(AntigravityProvider) is AntigravityAcpBackend
    assert SelectedModel.decode("antigravity/gemini-4-neon").provider_type is AntigravityProvider
    assert AntigravityProvider.thinking_levels() == ("low", "medium", "high", "xhigh")
    assert AntigravityAcpBackend.event_kinds == ANTIGRAVITY_ACP_EVENT_KINDS
    AntigravityAcpBackend.require_event_family(AntigravityAcpUpdate)


def test_antigravity_session_storage_wal_safe_clone_and_deletion(tmp_path: Path) -> None:
    parent = AntigravitySessionStorage(
        root=tmp_path / ".gemini/antigravity-cli",
        conversation_id=AntigravityConversationId("parent-uuid"),
    )
    _make_fake_antigravity_db(parent, title="Original Thread")

    child = parent.clone_to(AntigravityConversationId("child-uuid"))
    assert child.exists
    assert child.annotations_path.read_text(encoding="utf-8") == 'title:"Original Thread"\n'
    assert child.transcript_path.is_file()
    assert not (child.brain_dir / ".agy-thread.json").exists()

    conn = sqlite3.connect(child.db_path)
    traj_id, cascade_id = conn.execute("SELECT trajectory_id, cascade_id FROM trajectory_meta").fetchone()
    steps = conn.execute("SELECT count(*) FROM steps").fetchone()[0]
    conn.close()
    assert cascade_id == "child-uuid"
    assert traj_id != "orig-traj-id"
    assert steps == 2

    freed = AntigravitySessionFiles.for_storage(child).delete()
    assert freed > 0
    assert not child.exists
    assert not child.annotations_path.exists()
    assert not child.brain_dir.exists()


def test_new_thread_and_fork_specs_select_antigravity_backend(tmp_path: Path, comms, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    spec = NewThreadSpec.from_task(
        name="agy-one",
        project=LaunchCheckout(tmp_path),
        model=DeclaredModel("antigravity/gemini-4-neon"),
        thinking_level=HighThinkingLevel,
    )
    declared = spec.declare(comms.threads, "pi", None)  # type: ignore[arg-type]
    assert declared.backend is AntigravityAcpBackend
    assert isinstance(declared.runtime, AntigravitySession)

    # Populate parent SQLite DB as if a turn ran, then fork polymorphically through ForkSpec.
    parent_storage = AntigravitySessionStorage.from_db_path(Path(declared.runtime.path))
    _make_fake_antigravity_db(parent_storage, title="Agy One")
    fork_spec = ForkSpec.from_task(
        name="agy-two",
        parent="agy-one",
        project=LaunchCheckout(tmp_path),
        model=DeclaredModel("antigravity/gemini-4-neon"),
        thinking_level=HighThinkingLevel,
    )
    forked = fork_spec.declare(comms.threads, "pi", None)  # type: ignore[arg-type]
    assert forked.backend is AntigravityAcpBackend
    assert isinstance(forked.runtime, AntigravitySession)
    assert forked.runtime.path != declared.runtime.path
    assert Path(forked.runtime.path).is_file()

    # Switching model away from antigravity via ChosenModel resets incompatible session location.
    switched = ChosenModel("openai-codex/gpt-5.6-sol").configured(forked)
    assert switched.backend is not AntigravityAcpBackend
    encoded = FieldCodec.encode(forked)
    assert encoded["backend"] == "antigravity_acp"
    assert FieldCodec.decode(Thread, encoded).backend is AntigravityAcpBackend


@pytest.mark.asyncio
async def test_shared_acp_process_multiplexes_two_backends_and_streams_events(tmp_path: Path) -> None:
    class _FakeRunner:
        pass

    runner = _FakeRunner()
    process = AntigravityAcpPool.for_runner(
        runner, command=(sys.executable, "-u", "-c", _FAKE_ACP_SERVER),
    )
    backend_a = AntigravityAcpBackend.for_worker(runner, AcpSessionId("a"))
    backend_b = AntigravityAcpBackend.for_worker(runner, AcpSessionId("b"))
    assert backend_a._process is process is backend_b._process

    class _StartListener:
        started_events = (events.OriginalInputStarted(),)

        def original(self, native_id, text):
            del native_id, text
            return True

    def _delivery():
        return SimpleNamespace(
            worktree=str(tmp_path),
            send_boundary=NO_SEND_BOUNDARY,
            input_start=_StartListener(),
        )

    loc_a = AntigravitySession.fresh_in(tmp_path)
    loc_b = AntigravitySession.fresh_in(tmp_path)
    thread_a = NativeThread(
        "agy-a", frozenset(), Checkout(tmp_path),
        backend=AntigravityAcpBackend,
        model=DeclaredModel("antigravity/gemini-4-neon"),
        thinking_level=HighThinkingLevel,
        session_file=RegistrySessionLocation.from_path(loc_a.path, tmp_path),
    )
    thread_b = NativeThread(
        "agy-b", frozenset(), Checkout(tmp_path),
        backend=AntigravityAcpBackend,
        model=DeclaredModel("antigravity/gemini-4-carbon"),
        thinking_level=HighThinkingLevel,
        session_file=RegistrySessionLocation.from_path(loc_b.path, tmp_path),
    )

    async def collect(backend: AntigravityAcpBackend, thread: NativeThread, text: str):
        return [
            ev async for ev in backend.run_turn(thread, InputContent(text=text), _delivery())
        ]

    # Run thread A FIRST so `process.alive` is already True when thread B starts with NoAcpSessionId()!
    events_a = await collect(backend_a, thread_a, "alpha")
    assert process.alive
    events_b = await collect(backend_b, thread_b, "beta")
    assert process.alive
    # Also run both concurrently on the same shared ACP process.
    events_a2, events_b2 = await asyncio.gather(
        collect(backend_a, thread_a, "alpha-2"),
        collect(backend_b, thread_b, "beta-2"),
    )
    assert any(isinstance(e, events.Done) and e.ok and "alpha-2" in e.text for e in events_a2)
    assert any(isinstance(e, events.Done) and e.ok and "beta-2" in e.text for e in events_b2)
    assert any(isinstance(e, AgentInfo) for e in events_a)
    assert any(isinstance(e, events.Thinking) for e in events_a)
    assert any(isinstance(e, events.ToolStart) for e in events_a)
    assert any(isinstance(e, events.ToolEnd) and e.ok for e in events_a)
    assert any(isinstance(e, ContextInfo) for e in events_a)
    done_a = [e for e in events_a if isinstance(e, events.Done)][-1]
    done_b = [e for e in events_b if isinstance(e, events.Done)][-1]
    assert done_a.ok and "reply:alpha:gemini-4-neon" in done_a.text
    assert done_b.ok and "reply:beta:gemini-4-carbon" in done_b.text

    compacted = await backend_a.compact(thread_a, "focus on tests")
    assert isinstance(compacted, PlacedCompaction)

    # Closing session A keeps the shared ACP process alive for session B; closing B shuts it down.
    await backend_a.close_idle()
    assert process.alive
    await backend_b.close_idle()
    assert not process.alive


def test_switch_to_native_and_attach_session_for_antigravity_thread(tmp_path: Path, comms) -> None:
    from agent_comms.execution.child_process import NoProcess
    from agent_comms.registry.thread_status import NoBridgeThreadStatus
    from agent_comms.registry.threads import ExternalThread

    storage = AntigravitySessionStorage(
        root=tmp_path / ".gemini/antigravity-cli",
        conversation_id=AntigravityConversationId("existing-agy-uuid"),
    )
    _make_fake_antigravity_db(storage, title="Existing Agy")
    (storage.brain_dir / ".system_generated/logs/transcript.jsonl").write_text(
        json.dumps({"step_index": 1, "type": "PLANNER_RESPONSE", "model": "gemini-4-neon"}) + "\n",
        encoding="utf-8",
    )
    external = ExternalThread.stored(
        comms.registry.declare(
            ExternalThread(
                "agy-legacy",
                frozenset({"model"}),
                Checkout(tmp_path),
                model=DeclaredModel("antigravity/gemini-4-neon"),
            ),
            NoBridgeThreadStatus(),
        )
    )
    switched = comms.threads.switch_to_native(external.name)
    assert switched.backend is AntigravityAcpBackend
    assert isinstance(switched.runtime, AntigravitySession)

    attached = comms.threads.attach_session(
        switched, str(storage.db_path), process=NoProcess(),
    )
    assert attached.backend is AntigravityAcpBackend
    assert Path(attached.runtime.path).resolve() == storage.db_path.resolve()
    assert attached.model == DeclaredModel("antigravity/gemini-4-neon")
    assert attached.runtime.read_keys() == ()

    # Sidebar unread indexing must never try to parse Antigravity's binary SQLite `.db` as Pi JSONL.
    from agent_comms.registry.roster_visibility import RosterVisibility
    from agent_comms.ui_model.sidebar import SidebarModel

    for visibility in (
        RosterVisibility(stopped=False, archived=False),
        RosterVisibility(stopped=True, archived=False),
        RosterVisibility(stopped=True, archived=True),
    ):
        snapshot = comms.views.viewer_snapshot(Checkout(tmp_path), visibility)
        SidebarModel.derive(snapshot)

    # Persisting via set_thread_configuration switches backend both into and out of AntigravityAcpBackend.
    from agent_comms.registry.threads import NextTurnConfiguration

    pi_thread = comms.registry.declare(NativeThread("pi-to-agy", frozenset(), Checkout(tmp_path)))
    migrated = comms.threads.set_thread_configuration(
        pi_thread, NextTurnConfiguration(model=ChosenModel("antigravity/gemini-4-neon")),
    )
    assert migrated.backend is AntigravityAcpBackend
    assert isinstance(migrated.runtime, AntigravitySession)
    reattached = comms.threads.attach_session(
        migrated, str(storage.db_path), process=NoProcess(),
    )
    assert reattached.backend is AntigravityAcpBackend
    assert Path(reattached.runtime.path).resolve() == storage.db_path.resolve()
    back_to_pi = comms.threads.set_thread_configuration(
        reattached, NextTurnConfiguration(model=ChosenModel("openai-codex/gpt-5.6-sol")),
    )
    assert back_to_pi.backend is not AntigravityAcpBackend


@pytest.mark.asyncio
async def test_comms_agent_drains_and_runs_two_turns_on_antigravity_acp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os
    from bus_record_fixture import prepare_native_comms
    from agent_comms.comms import wire
    from agent_comms.execution.acp import CommsAgent, RetainedOwnerLaunch
    from agent_comms.execution.child_process import ProcessIdentity
    from agent_comms.execution.native_arguments import NativeArguments
    from agent_comms.registry.threads import NextTurnConfiguration

    fake_script = tmp_path / "fake_agy.py"
    fake_script.write_text(_FAKE_ACP_SERVER, encoding="utf-8")
    fake_bin = tmp_path / "fake_agy"
    fake_bin.write_text(f"#!/bin/sh\nexec {sys.executable} {fake_script} \"$@\"\n", encoding="utf-8")
    fake_bin.chmod(0o755)
    monkeypatch.setenv("AGENT_COMMS_AGY_BIN", str(fake_bin))

    root = tmp_path / "bus"
    worktree = tmp_path / "wt"
    worktree.mkdir()
    comms = prepare_native_comms(wire(root))

    comms.registry.declare(NativeThread("tristan", frozenset(), Checkout(worktree)))
    thread = comms.registry.declare(NativeThread("agy-e2e", frozenset(), Checkout(worktree)))
    thread = comms.threads.set_thread_configuration(
        thread, NextTurnConfiguration(model=ChosenModel("antigravity/gemini-4-neon")),
    )
    assert thread.backend is AntigravityAcpBackend

    comms.owners.acquire_thread(thread.name)
    owner = comms.registry.entry(thread.name).entry.owner_identity
    proc = ProcessIdentity.capture(os.getpid())
    env = {
        **os.environ,
        "AGENT_COMMS_THREAD": thread.name,
        "PI_WORKTREE": str(worktree),
        NativeArguments.binary_key: NativeArguments.default_binary,
    }
    launch = RetainedOwnerLaunch(process=proc, interpreter=sys.executable, environment=env)

    agent = CommsAgent(comms, auto_wake=True)
    sid = AcpSessionId(thread.name)
    try:
        await agent.sessions.start_owner(launch, owner)

        comms.messaging.send("tristan", thread.name, "first-ping")
        for _ in range(80):
            await asyncio.sleep(0.05)
            if len(comms.bus.inbox("tristan").page.messages) >= 1 and not agent.inputs.turn_live(sid):
                break
        backend = agent.turns.persistent_backends.get(sid)
        assert isinstance(backend, AntigravityAcpBackend)
        first_session = str(backend._acp_session)
        first_pid = backend.runtime_process.pid
        assert first_session and first_pid > 0

        comms.messaging.send("tristan", thread.name, "second-ping")
        for _ in range(80):
            await asyncio.sleep(0.05)
            if len(comms.bus.inbox("tristan").page.messages) >= 2 and not agent.inputs.turn_live(sid):
                break
        replies = [m.body for m in comms.bus.inbox("tristan").page.messages]
        assert len(replies) == 2
        assert "first-ping" in replies[0]
        assert "second-ping" in replies[1]
        assert str(backend._acp_session) == first_session
        assert backend.runtime_process.pid == first_pid
        diag_dir = root / "diagnostics"
        assert not (list(diag_dir.glob("*.json")) if diag_dir.exists() else [])
    finally:
        await agent.shutdown()


