"""AntigravityAcpBackend: multiplexed single-process ACP execution, WAL-safe forking, and nominal MI."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from bus_record_fixture import prepare_native_comms

from agent_comms import agent_events as events
from agent_comms.agent_backend import AgentBackend, Forks, PlacedCompaction, ThreadServingBackend
from agent_comms.comms import wire
from agent_comms.execution.acp import CommsAgent, RetainedOwnerLaunch
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
from agent_comms.execution.child_process import ProcessIdentity
from agent_comms.execution.native_arguments import AntigravityProvider, NativeArguments, SelectedModel
from agent_comms.execution.pi_vocabulary import HighThinkingLevel
from agent_comms.execution.runtime_observations import AgentInfo, ContextInfo
from agent_comms.execution.send_outcome import NO_SEND_BOUNDARY
from agent_comms.execution.session_file import RegistrySessionLocation
from agent_comms.execution.turn_inbox import InputContent, TurnInbox
from agent_comms.input_attempt import NotSentInput, ReservedInput
from agent_comms.nominal.field_codec import AcpSessionId, FieldCodec
from agent_comms.projects.checkouts import Checkout, LaunchCheckout
from agent_comms.registry.thread_management import ForkSpec, NewThreadSpec
from agent_comms.registry.thread_model import DeclaredModel
from agent_comms.registry.threads import ChosenModel, NativeThread, NextTurnConfiguration, Thread


_FAKE_ACP_SERVER = """
import json, sys, threading, time, uuid

sessions = {}
write_lock = threading.Lock()

def write_msg(payload):
    with write_lock:
        sys.stdout.write(json.dumps(payload) + "\\n")
        sys.stdout.flush()

for raw in sys.stdin:
    if not raw.strip():
        continue
    msg = json.loads(raw)
    method = msg.get("method")
    req_id = msg.get("id")
    params = msg.get("params") or {}

    def reply(result, rid=req_id):
        write_msg({"jsonrpc": "2.0", "id": rid, "result": result})

    def update(sid, payload):
        write_msg({
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {"sessionId": sid, "update": payload},
        })

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
            write_msg({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32602, "message": f"Session not found: {sid}"}})
            continue
        sessions[sid]["turns"] = sessions[sid].get("turns", 0) + 1
        text = " ".join(b.get("text", "") for b in params.get("prompt", []) if b.get("type") == "text")
        if "SLOW_HOLD" in text:
            def finish_slow(rid=req_id, s=sid, t=text):
                time.sleep(0.5)
                update(s, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": f"slow:{t}"}})
                reply({"stopReason": "end_turn"}, rid=rid)
            threading.Thread(target=finish_slow, daemon=True).start()
            continue
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
            inbox=TurnInbox(),
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
    from agent_comms.execution.turn_phase import (
        NativePhaseChanged,
        ReplyingPhase,
        SettlingStatsPhase,
        ThinkingPhase,
        ToolRunningPhase,
    )

    assert any(isinstance(e, AgentInfo) for e in events_a)
    assert any(isinstance(e, events.Thinking) for e in events_a)
    assert any(isinstance(e, events.ToolStart) for e in events_a)
    assert any(isinstance(e, events.ToolEnd) and e.ok for e in events_a)
    assert any(isinstance(e, ContextInfo) for e in events_a)
    phases_a = [e.phase for e in events_a if isinstance(e, NativePhaseChanged)]
    assert any(isinstance(p, ThinkingPhase) for p in phases_a)
    assert any(isinstance(p, ToolRunningPhase) and p.tools for p in phases_a)
    assert any(isinstance(p, ReplyingPhase) for p in phases_a)
    assert any(isinstance(p, SettlingStatsPhase) for p in phases_a)
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
    from agent_comms.nominal.field_codec import ToolCallId
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

        sent1 = comms.messaging.send("tristan", thread.name, "first-ping")
        for _ in range(80):
            await asyncio.sleep(0.05)
            if len(comms.bus.inbox("tristan").page.messages) >= 1 and not agent.inputs.turn_live(sid):
                break
        backend = agent.turns.persistent_backends.get(sid)
        assert isinstance(backend, AntigravityAcpBackend)
        first_session = str(backend._acp_session)
        first_pid = backend.runtime_process.pid
        assert first_session and first_pid > 0

        from agent_comms.messaging.message_notification import MessageNotification
        from agent_comms.messaging.notification_evidence import AllOutcomes

        ref1 = sent1.message.reference
        win1 = comms.views.message_notifications_for_references(AllOutcomes(), (ref1,))
        summary1 = MessageNotification.summary(win1.outcomes.get((ref1.seq, ref1.message_id), ()))
        assert "1 read" in summary1, f"Expected read receipt in summary, got {summary1!r}"

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

        # Alert/Notice messages must NOT wake the thread (no alert ping-pong storms).
        from agent_comms.messaging.messages import AlertKind, NoticeRole

        comms.messaging.send("tristan", thread.name, "non-waking alert", AlertKind, role=NoticeRole())
        await asyncio.sleep(0.3)
        assert len(comms.bus.inbox("tristan").page.messages) == 2

        # Mid-turn steer: send a slow turn, then inject a second bus message WHILE the first turn is live!
        comms.messaging.send("tristan", thread.name, "SLOW_HOLD initial task")
        for _ in range(40):
            await asyncio.sleep(0.02)
            if agent.inputs.turn_live(sid):
                break
        assert agent.inputs.turn_live(sid), "Expected SLOW_HOLD turn to be actively running"
        steered_sent = comms.messaging.send("tristan", thread.name, "MID_TURN_STEER_99")
        for _ in range(100):
            await asyncio.sleep(0.05)
            if len(comms.bus.inbox("tristan").page.messages) >= 3 and not agent.inputs.turn_live(sid):
                break
        replies_after_steer = [m.body for m in comms.bus.inbox("tristan").page.messages]
        assert len(replies_after_steer) == 3, f"Expected 3 combined replies, got {replies_after_steer}"
        assert "MID_TURN_STEER_99" in replies_after_steer[2] and "SLOW_HOLD" in replies_after_steer[2]
        steer_ref = steered_sent.message.reference
        steer_win = comms.views.message_notifications_for_references(AllOutcomes(), (steer_ref,))
        steer_summary = MessageNotification.summary(
            steer_win.outcomes.get((steer_ref.seq, steer_ref.message_id), ())
        )
        assert "1 read" in steer_summary, f"Expected read receipt on steered msg, got {steer_summary!r}"

        diag_dir = root / "diagnostics"
        assert not (list(diag_dir.glob("*.json")) if diag_dir.exists() else [])
    finally:
        await agent.shutdown()


@pytest.mark.asyncio
async def test_five_concurrent_antigravity_threads_wake_without_lock_starvation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os
    from bus_record_fixture import prepare_native_comms
    from agent_comms.comms import wire
    from agent_comms.execution.acp import CommsAgent, RetainedOwnerLaunch
    from agent_comms.execution.child_process import ProcessIdentity
    from agent_comms.execution.native_arguments import NativeArguments
    from agent_comms.messaging.channel_targets import ChannelTarget
    from agent_comms.messaging.message_notification import MessageNotification
    from agent_comms.messaging.notification_evidence import AllOutcomes
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

    comms.messaging.user_identity(Checkout(worktree))
    names = tuple(f"pm-{idx}" for idx in range(5))
    for name in names:
        declared = comms.registry.declare(NativeThread(name, frozenset({"model"}), Checkout(worktree)))
        comms.threads.set_thread_configuration(
            declared, NextTurnConfiguration(model=ChosenModel("antigravity/gemini-4-neon")),
        )
        comms.owners.acquire_thread(name)

    proc = ProcessIdentity.capture(os.getpid())
    agent = CommsAgent(comms, auto_wake=True)
    sids = tuple(AcpSessionId(name) for name in names)
    try:
        for name in names:
            owner = comms.registry.entry(name).entry.owner_identity
            env = {
                **os.environ,
                "AGENT_COMMS_THREAD": name,
                "PI_WORKTREE": str(worktree),
                NativeArguments.binary_key: NativeArguments.default_binary,
            }
            launch = RetainedOwnerLaunch(process=proc, interpreter=sys.executable, environment=env)
            await agent.sessions.start_owner(launch, owner)

        sent = comms.messaging.send_user_message(
            ChannelTarget("#model"), "usage-is-back", worktree=Checkout(worktree),
        )
        for _ in range(120):
            await asyncio.sleep(0.05)
            msgs = [m for m in comms.bus.bus.since(sent.seq, 50) if m.sender in names]
            if len(msgs) >= 5 and not any(agent.inputs.turn_live(sid) for sid in sids):
                break
        msgs = [m for m in comms.bus.bus.since(sent.seq, 50) if m.sender in names]
        assert {m.sender for m in msgs} == set(names), f"Missing replies: {msgs}"
        pids = {
            agent.turns.persistent_backends[sid].runtime_process.pid
            for sid in sids
        }
        assert len(pids) == 1 and next(iter(pids)) > 0

        ref = sent.reference
        win = comms.views.message_notifications_for_references(AllOutcomes(), (ref,))
        summary = MessageNotification.summary(win.outcomes.get((ref.seq, ref.message_id), ()))
        assert "5 read" in summary, f"Expected 5 read receipts, got {summary!r}"

        diag_dir = root / "diagnostics"
        assert not (list(diag_dir.glob("*.json")) if diag_dir.exists() else [])
    finally:
        await agent.shutdown()


def test_committed_progress_flushes_intermediate_and_open_background_replies() -> None:
    # 1. Intermediate reply before a tool call flushes CommittedProgress once, leaving final reply intact.
    state = AcpTurnStreamState()
    e1 = AntigravityAcpUpdate.decode(
        {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "checking logs..."}},
        state,
    )
    assert any(isinstance(ev, events.Chunk) for ev in e1)
    assert not any(isinstance(ev, events.CommittedProgress) for ev in e1)

    e2 = AntigravityAcpUpdate.decode(
        {"sessionUpdate": "tool_call", "toolCallId": "bg-1", "title": "run_command", "name": "run_command"},
        state,
    )
    committed = [ev for ev in e2 if isinstance(ev, events.CommittedProgress)]
    assert committed == [events.CommittedProgress(text="checking logs...")]
    assert any(isinstance(ev, events.ToolStart) for ev in e2)

    # 2. Reply written while a background tool is STILL open flushes on usage_update (no silent trap!).
    AntigravityAcpUpdate.decode(
        {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "numbers look clean"}},
        state,
    )
    e3 = AntigravityAcpUpdate.decode(
        {"sessionUpdate": "usage_update", "used": 2000, "size": 1048576},
        state,
    )
    assert [ev for ev in e3 if isinstance(ev, events.CommittedProgress)] == [
        events.CommittedProgress(text="numbers look clean")
    ]
    assert any(isinstance(ev, ContextInfo) for ev in e3)

    # 3. Once the tool finishes, the final reply before end_turn is NOT prematurely flushed on usage_update.
    AntigravityAcpUpdate.decode(
        {"sessionUpdate": "tool_call_update", "toolCallId": "bg-1", "name": "run_command", "status": "completed", "rawOutput": "ok"},
        state,
    )
    AntigravityAcpUpdate.decode(
        {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "all done"}},
        state,
    )
    e4 = AntigravityAcpUpdate.decode(
        {"sessionUpdate": "usage_update", "used": 2500, "size": 1048576},
        state,
    )
    assert not any(isinstance(ev, events.CommittedProgress) for ev in e4)
    assert "".join(state.pending_progress_chunks) == "all done"


@pytest.mark.asyncio
async def test_antigravity_acp_turn_publishes_external_transcript_items_and_drafts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os
    from bus_record_fixture import prepare_native_comms
    from agent_comms.comms import wire
    from agent_comms.execution.acp import CommsAgent, RetainedOwnerLaunch
    from agent_comms.execution.child_process import ProcessIdentity
    from agent_comms.execution.native_arguments import NativeArguments
    from agent_comms.nominal.field_codec import ToolCallId
    from agent_comms.registry.threads import NextTurnConfiguration
    from agent_comms.transcript.transcript_drafts import NoDraft
    from agent_comms.transcript.transcript_events import (
        AssistantTranscript,
        SentTranscript,
        ThinkingTranscript,
        ToolEndTranscript,
        ToolStartTranscript,
    )

    fake_script = tmp_path / "fake_agy_transcript.py"
    fake_script.write_text(
        '''import json, sys

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")
    sys.stdout.flush()

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    method = msg.get("method")
    rid = msg.get("id")
    params = msg.get("params") or {}
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": 1}})
    elif method == "session/new":
        send({"jsonrpc": "2.0", "id": rid, "result": {
            "sessionId": "11111111-2222-3333-4444-555555555555",
            "configOptions": [
                {"id": "model", "currentValue": "gemini-4-neon"},
                {"id": "thought_level", "currentValue": "high"},
            ],
        }})
    elif method == "session/prompt":
        sid = params["sessionId"]
        for update in (
            {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "inspecting "}},
            {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "android logs"}},
            {"sessionUpdate": "tool_call", "toolCallId": "call-1", "name": "run_command", "title": "run_command", "rawInput": {"CommandLine": "adb logcat -d"}},
            {"sessionUpdate": "tool_call_update", "toolCallId": "call-1", "name": "run_command", "status": "completed", "rawOutput": "clean"},
            {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "logs checked, patching now"}},
            {"sessionUpdate": "tool_call", "toolCallId": "call-2", "name": "read_file", "title": "read_file", "rawInput": {"Path": "/tmp/app.py"}},
            {"sessionUpdate": "tool_call_update", "toolCallId": "call-2", "name": "read_file", "status": "completed", "rawOutput": "print(1)"},
            {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "all fixed"}},
            {"sessionUpdate": "usage_update", "used": 1200, "size": 1048576},
        ):
            send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": update}})
        send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})
    elif rid is not None:
        send({"jsonrpc": "2.0", "id": rid, "result": {}})
''',
        encoding="utf-8",
    )
    fake_bin = tmp_path / "fake_agy_transcript"
    fake_bin.write_text(f"#!/bin/sh\nexec {sys.executable} {fake_script} \"$@\"\n", encoding="utf-8")
    fake_bin.chmod(0o755)
    monkeypatch.setenv("AGENT_COMMS_AGY_BIN", str(fake_bin))

    root = tmp_path / "bus"
    worktree = tmp_path / "wt"
    worktree.mkdir()
    comms = prepare_native_comms(wire(root))
    comms.registry.declare(NativeThread("tristan", frozenset(), Checkout(worktree)))

    declared = comms.registry.declare(NativeThread("toad-sim", frozenset({"toad"}), Checkout(worktree)))
    comms.threads.set_thread_configuration(
        declared, NextTurnConfiguration(model=ChosenModel("antigravity/gemini-4-neon")),
    )
    comms.owners.acquire_thread("toad-sim")
    owner = comms.registry.entry("toad-sim").entry.owner_identity

    proc = ProcessIdentity.capture(os.getpid())
    agent = CommsAgent(comms, auto_wake=True)
    sid = AcpSessionId("toad-sim")
    try:
        env = {
            **os.environ,
            "AGENT_COMMS_THREAD": "toad-sim",
            "PI_WORKTREE": str(worktree),
            NativeArguments.binary_key: NativeArguments.default_binary,
        }
        launch = RetainedOwnerLaunch(process=proc, interpreter=sys.executable, environment=env)
        await agent.sessions.start_owner(launch, owner)

        comms.messaging.send("tristan", "toad-sim", "check android message view")
        for _ in range(100):
            await asyncio.sleep(0.05)
            replies = [m.body for m in comms.bus.inbox("tristan").page.messages]
            if len(replies) >= 2 and not agent.inputs.turn_live(sid):
                break

        replies = [m.body for m in comms.bus.inbox("tristan").page.messages]
        assert replies == ["logs checked, patching now", "all fixed"]

        page = comms.transcripts.capture_page_read("toad-sim").read()
        thoughts = [ev.text for ev in page.events if isinstance(ev, ThinkingTranscript)]
        starts = [ev for ev in page.events if isinstance(ev, ToolStartTranscript)]
        ends = [ev for ev in page.events if isinstance(ev, ToolEndTranscript)]
        sents = [ev.text for ev in page.events if isinstance(ev, SentTranscript)]
        dup_assistants = [ev for ev in page.events if isinstance(ev, AssistantTranscript)]

        assert thoughts == ["inspecting android logs"]
        assert [(ev.tool_call_id, ev.tool_name) for ev in starts] == [
            (ToolCallId("call-1"), "bash"),
            (ToolCallId("call-2"), "read"),
        ]
        assert [(ev.tool_call_id, ev.text, ev.ok) for ev in ends] == [
            (ToolCallId("call-1"), "clean", True),
            (ToolCallId("call-2"), "print(1)", True),
        ]
        assert sents == ["logs checked, patching now", "all fixed"]
        assert dup_assistants == []

        ext = comms.transcripts.external("toad-sim")
        with ext.reading() as reader:
            assert reader.current_draft(ext.root, ext.incarnation) == NoDraft()
    finally:
        await agent.shutdown()


@pytest.mark.asyncio
async def test_antigravity_acp_steers_dm_during_active_persistent_goal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An incoming DM while a persistent goal is active must steer into the running
    Antigravity ACP turn and record its read receipt instead of sitting stuck on Sent."""
    import os
    from bus_record_fixture import prepare_native_comms
    from agent_comms.comms import wire
    from agent_comms.execution.acp import CommsAgent, RetainedOwnerLaunch
    from agent_comms.execution.child_process import ProcessIdentity
    from agent_comms.execution.native_arguments import NativeArguments
    from agent_comms.messaging.message_notification import MessageNotification
    from agent_comms.messaging.notification_evidence import AllOutcomes
    from agent_comms.registry.threads import NextTurnConfiguration

    fake_script = tmp_path / "fake_agy.py"
    fake_script.write_text(_FAKE_ACP_SERVER, encoding="utf-8")
    fake_bin = tmp_path / "fake_agy"
    fake_bin.write_text(f"#!/bin/sh\nexec {sys.executable} {fake_script} \"$@\"\n", encoding="utf-8")
    fake_bin.chmod(0o755)
    monkeypatch.setenv("AGENT_COMMS_AGY_BIN", str(fake_bin))

    root = tmp_path / "comms-root"
    worktree = tmp_path / "wt"
    worktree.mkdir()
    comms = prepare_native_comms(wire(root))
    comms.registry.declare(NativeThread("tristan", frozenset(), Checkout(worktree)))
    thread = comms.registry.declare(NativeThread("agy-goal-steer", frozenset(), Checkout(worktree)))
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
        # Active persistent goal whose prompt triggers SLOW_HOLD so the turn stays live
        # while the human DM arrives.
        await agent.turns.goals.set_goal(sid, "SLOW_HOLD persistent goal work")
        for _ in range(80):
            await asyncio.sleep(0.02)
            if agent.inputs.turn_live(sid):
                break
        assert agent.inputs.turn_live(sid), "Expected active goal turn to be running"

        dm_sent = comms.messaging.send("tristan", thread.name, "GOAL_DM_STEER_42")
        ref = dm_sent.message.reference
        summary = ""
        for _ in range(100):
            await asyncio.sleep(0.05)
            win = comms.views.message_notifications_for_references(AllOutcomes(), (ref,))
            summary = MessageNotification.summary(
                win.outcomes.get((ref.seq, ref.message_id), ())
            )
            if "1 read" in summary:
                break
        assert "1 read" in summary, (
            f"DM during active persistent goal stayed stuck instead of steering: {summary!r}"
        )
        diag_dir = root / "diagnostics"
        assert not (list(diag_dir.glob("*.json")) if diag_dir.exists() else [])
    finally:
        await agent.shutdown()


@pytest.mark.asyncio
async def test_antigravity_acp_ignores_unmentioned_channel_post_and_settles_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unmentioned MentionedPolicy channel post must not wake an Antigravity ACP
    member and must settle its BoundedTriageWake claim to Not answering (never Sent)."""
    import os
    from bus_record_fixture import prepare_native_comms
    from agent_comms.comms import wire
    from agent_comms.execution.acp import CommsAgent, RetainedOwnerLaunch
    from agent_comms.execution.child_process import ProcessIdentity
    from agent_comms.execution.native_arguments import NativeArguments
    from agent_comms.messaging.notification_evidence import AllOutcomes
    from agent_comms.messaging.notification_progress import IgnoredProgress
    from agent_comms.registry.threads import NextTurnConfiguration

    fake_script = tmp_path / "fake_agy.py"
    fake_script.write_text(_FAKE_ACP_SERVER, encoding="utf-8")
    fake_bin = tmp_path / "fake_agy"
    fake_bin.write_text(f"#!/bin/sh\nexec {sys.executable} {fake_script} \"$@\"\n", encoding="utf-8")
    fake_bin.chmod(0o755)
    monkeypatch.setenv("AGENT_COMMS_AGY_BIN", str(fake_bin))

    root = tmp_path / "comms-root"
    worktree = tmp_path / "wt"
    worktree.mkdir()
    comms = prepare_native_comms(wire(root))
    comms.registry.declare(NativeThread("sender", frozenset({"model"}), Checkout(worktree)))
    comms.registry.declare(NativeThread("other-agent", frozenset({"model"}), Checkout(worktree)))
    thread = comms.registry.declare(NativeThread("agy-idle", frozenset({"model"}), Checkout(worktree)))
    thread = comms.threads.set_thread_configuration(
        thread, NextTurnConfiguration(model=ChosenModel("antigravity/gemini-4-neon")),
    )
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
        sent = comms.messaging.send("sender", "#model", "@other-agent please check run 8")
        ref = sent.message.reference
        ignored_state = None
        by_recipient: dict[str, object] = {}
        for _ in range(80):
            await asyncio.sleep(0.05)
            win = comms.views.message_notifications_for_references(AllOutcomes(), (ref,))
            by_recipient = {
                item.recipient: item.state
                for item in win.outcomes.get((ref.seq, ref.message_id), ())
            }
            if isinstance(by_recipient.get(thread.name), IgnoredProgress):
                ignored_state = by_recipient[thread.name]
                break
        assert isinstance(ignored_state, IgnoredProgress), (
            f"Unmentioned BoundedTriageWake post stayed stuck instead of IgnoredProgress: {by_recipient!r}"
        )
        assert not agent.inputs.turn_live(sid)
        assert comms.bus.inbox(thread.name).page.messages == ()
        channel_msgs = [m.body for m in comms.bus.inbox("other-agent").page.messages]
        assert channel_msgs == ["@other-agent please check run 8"]
    finally:
        await agent.shutdown()


@pytest.mark.asyncio
async def test_antigravity_acp_suppresses_ignore_channel_reply_without_posting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a woken Antigravity ACP agent answers IGNORE on a channel turn (either in
    CommittedProgress before a tool call or as final text), TurnProgress must suppress
    posting IGNORE to the channel while still acknowledging the incoming message."""
    import os
    from bus_record_fixture import prepare_native_comms
    from agent_comms.comms import wire
    from agent_comms.execution.acp import CommsAgent, RetainedOwnerLaunch
    from agent_comms.execution.child_process import ProcessIdentity
    from agent_comms.execution.native_arguments import NativeArguments
    from agent_comms.messaging.channel_targets import ChannelTarget
    from agent_comms.messaging.message_notification import MessageNotification
    from agent_comms.messaging.notification_evidence import AllOutcomes
    from agent_comms.registry.threads import NextTurnConfiguration

    ignore_server = """
import json, sys, uuid

for raw in sys.stdin:
    if not raw.strip():
        continue
    msg = json.loads(raw)
    method = msg.get("method")
    rid = msg.get("id")
    params = msg.get("params") or {}
    def send(payload):
        sys.stdout.write(json.dumps(payload) + "\\n")
        sys.stdout.flush()
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": 1}})
    elif method in ("session/new", "session/resume"):
        sid = params.get("sessionId") or str(uuid.uuid4())
        send({"jsonrpc": "2.0", "id": rid, "result": {"sessionId": sid, "configOptions": []}})
    elif method == "session/prompt":
        sid = params["sessionId"]
        for update in (
            {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "IGNORE"}},
            {"sessionUpdate": "tool_call", "toolCallId": "call-1", "name": "run_command", "title": "run_command", "rawInput": {"CommandLine": "true"}},
            {"sessionUpdate": "tool_call_update", "toolCallId": "call-1", "name": "run_command", "status": "completed", "rawOutput": ""},
            {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "IGNORE"}},
        ):
            send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": update}})
        send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})
    elif rid is not None:
        send({"jsonrpc": "2.0", "id": rid, "result": {}})
"""
    fake_script = tmp_path / "fake_agy_ignore.py"
    fake_script.write_text(ignore_server, encoding="utf-8")
    fake_bin = tmp_path / "fake_agy_ignore"
    fake_bin.write_text(f"#!/bin/sh\nexec {sys.executable} {fake_script} \"$@\"\n", encoding="utf-8")
    fake_bin.chmod(0o755)
    monkeypatch.setenv("AGENT_COMMS_AGY_BIN", str(fake_bin))

    root = tmp_path / "comms-root"
    worktree = tmp_path / "wt"
    worktree.mkdir()
    comms = prepare_native_comms(wire(root))
    comms.registry.declare(NativeThread("tristan", frozenset({"model"}), Checkout(worktree)))
    thread = comms.registry.declare(NativeThread("agy-ch", frozenset({"model"}), Checkout(worktree)))
    thread = comms.threads.set_thread_configuration(
        thread, NextTurnConfiguration(model=ChosenModel("antigravity/gemini-4-neon")),
    )
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
        sent = comms.messaging.send_user_message(
            ChannelTarget("#model"),
            "Guys, collective update with no mentions",
            worktree=Checkout(worktree),
        )
        ref = sent.reference
        summary = ""
        for _ in range(100):
            await asyncio.sleep(0.05)
            win = comms.views.message_notifications_for_references(AllOutcomes(), (ref,))
            summary = MessageNotification.summary(
                win.outcomes.get((ref.seq, ref.message_id), ())
            )
            if "1 read" in summary and not agent.inputs.turn_live(sid):
                break
        assert summary == "1 read · 1 not answering", (
            f"Expected suppressed IGNORE reply to settle as Not answering, got {summary!r}"
        )
        posted_by_agent = [
            m.body for m in comms.bus.inbox("tristan").page.messages if m.sender == thread.name
        ]
        assert posted_by_agent == [], f"IGNORE must never be posted to #model: {posted_by_agent!r}"
    finally:
        await agent.shutdown()


def test_progress_reply_role_preserves_channel_mentions_and_keeps_dm_notice() -> None:
    """Channel committed progress stays ConversationRole (so @mentions wake peers and receipts
    resolve); DM committed progress stays NoticeRole so mid-turn lines never wake DM peers."""
    from agent_comms.messaging.channel_targets import CommandTarget
    from agent_comms.messaging.messages import ConversationRole, NoticeRole

    assert isinstance(
        CommandTarget.named("#model").progress_reply_role(
            conversation=ConversationRole(), notice=NoticeRole()
        ),
        ConversationRole,
    )
    assert isinstance(
        CommandTarget.named("peer").progress_reply_role(
            conversation=ConversationRole(), notice=NoticeRole()
        ),
        NoticeRole,
    )


def test_explicit_receipt_choices_separate_read_answering_answered_and_not_answering(
    tmp_path: Path,
) -> None:
    """Read never auto-defaults to Answering; explicit Answering flips to Answered automatically
    when the reply lands, and Not answering settles without waiting for a reply."""
    from bus_record_fixture import prepare_native_comms
    from agent_comms.cli_bridges.harness_delivery_states import (
        AnsweringReceiptChoice,
        NotAnsweringReceiptChoice,
        ReadReceiptChoice,
    )
    from agent_comms.cli_bridges.history import RemoteWatchHistory
    from agent_comms.comms import wire
    from agent_comms.messaging.message_notification import MessageNotification
    from agent_comms.messaging.notification_evidence import AllOutcomes

    root = tmp_path / "comms-root"
    wt = tmp_path / "wt"
    wt.mkdir()
    comms = prepare_native_comms(wire(root))
    comms.registry.declare(NativeThread("tristan", frozenset({"team"}), Checkout(wt)))
    comms.registry.declare(NativeThread("peer-a", frozenset({"team"}), Checkout(wt)))
    comms.registry.declare(NativeThread("peer-b", frozenset({"team"}), Checkout(wt)))

    sent = comms.messaging.send("tristan", "#team", "check receipts")
    ref = sent.message.reference

    def summary() -> str:
        win = comms.views.message_notifications_for_references(AllOutcomes(), (ref,))
        return MessageNotification.summary(win.outcomes.get((ref.seq, ref.message_id), ()))

    # 1. Plain Read does not auto-default to Answering.
    comms.messaging.acknowledge_deliveries(
        "peer-a", (ref,), bridge=RemoteWatchHistory, status=ReadReceiptChoice,
    )
    comms.messaging.acknowledge_deliveries(
        "peer-b", (ref,), bridge=RemoteWatchHistory, status=ReadReceiptChoice,
    )
    assert summary() == "2 read"

    # 2. Explicit Answering and Not answering choices show in the summary.
    comms.messaging.acknowledge_deliveries(
        "peer-a", (ref,), bridge=RemoteWatchHistory, status=AnsweringReceiptChoice,
    )
    comms.messaging.acknowledge_deliveries(
        "peer-b", (ref,), bridge=RemoteWatchHistory, status=NotAnsweringReceiptChoice,
    )
    assert summary() == "2 read · 1 answering · 1 not answering"

    # 3. Sending a reply automatically flips Answering -> Answered with no second ack.
    comms.messaging.send("peer-a", "#team", "on it", answers=(ref,))
    assert summary() == "2 read · 1 answered · 1 not answering"


@pytest.mark.asyncio
async def test_direct_delivered_input_records_wire_origin_and_antigravity_allows_mcp_fork(
    tmp_path: Path,
) -> None:
    """Steered direct-inbox messages carry WireInputOrigin (for automatic answers= linking),
    and AntigravityAcpBackend allows MCP fork calls under its own thread name."""
    from bus_record_fixture import prepare_native_comms
    from agent_comms.comms import wire
    from agent_comms.execution.acp import CommsAgent
    from agent_comms.execution.input_origin import WireInputOrigin
    from agent_comms.execution.queued_input import DirectDeliveredInput
    from agent_comms.pi_native_backend import PiNativeBackend
    from agent_comms.registry.threads import NextTurnConfiguration
    from agent_comms_hooks import ToolRefusedError

    root = tmp_path / "comms-root"
    wt = tmp_path / "wt"
    wt.mkdir()
    comms = prepare_native_comms(wire(root))
    comms.registry.declare(NativeThread("tristan", frozenset({"team"}), Checkout(wt)))
    acp_thread = comms.registry.declare(NativeThread("acp-peer", frozenset({"team"}), Checkout(wt)))
    acp_thread = comms.threads.set_thread_configuration(
        acp_thread, NextTurnConfiguration(model=ChosenModel("antigravity/gemini-4-neon")),
    )
    pi_thread = comms.registry.declare(NativeThread("pi-peer", frozenset({"team"}), Checkout(wt)))

    # Polymorphic join_forking_caller: Antigravity ACP allows, Native Pi refuses.
    acp_thread.join_forking_caller(lambda: None)
    assert pi_thread.backend is PiNativeBackend
    with pytest.raises(ToolRefusedError, match="Native Pi"):
        pi_thread.join_forking_caller(lambda: None)

    # DirectDeliveredInput.reserve_direct must record WireInputOrigin, not UnattributedInputOrigin.
    msg = comms.messaging.send("tristan", "#team", "steered ping").message
    agent = CommsAgent(comms, auto_wake=False)
    try:
        reserved: list = []
        DirectDeliveredInput.reserve_direct(agent.inputs, acp_thread.name, (msg,), (), reserved)
        assert len(reserved) == 1
        stored = agent.inputs.dispositions.read((msg.bus_key(acp_thread),)).lookup(msg.bus_key(acp_thread))
        assert isinstance(stored.origin, WireInputOrigin)
        assert stored.origin.reference == msg.reference
    finally:
        await agent.shutdown()


@pytest.mark.asyncio
async def test_antigravity_acp_waking_dm_survives_restart_before_turn_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """drain_direct_inbox must not mark waking messages displayed before TurnProgress.input_started
    records durable HarnessDelivery, so a restart while queued redelivers the DM."""
    import os
    from bus_record_fixture import prepare_native_comms
    from agent_comms.comms import wire
    from agent_comms.execution.acp import CommsAgent, RetainedOwnerLaunch
    from agent_comms.execution.child_process import ProcessIdentity
    from agent_comms.execution.native_arguments import NativeArguments
    from agent_comms.messaging.message_notification import MessageNotification
    from agent_comms.messaging.notification_evidence import AllOutcomes
    from agent_comms.registry.threads import NextTurnConfiguration

    fake_script = tmp_path / "fake_agy.py"
    fake_script.write_text(_FAKE_ACP_SERVER, encoding="utf-8")
    fake_bin = tmp_path / "fake_agy"
    fake_bin.write_text(f"#!/bin/sh\nexec {sys.executable} {fake_script} \"$@\"\n", encoding="utf-8")
    fake_bin.chmod(0o755)
    monkeypatch.setenv("AGENT_COMMS_AGY_BIN", str(fake_bin))

    root = tmp_path / "comms-root"
    worktree = tmp_path / "wt"
    worktree.mkdir()
    comms = prepare_native_comms(wire(root))
    comms.registry.declare(NativeThread("tristan", frozenset(), Checkout(worktree)))
    thread = comms.registry.declare(NativeThread("agy-restart", frozenset(), Checkout(worktree)))
    thread = comms.threads.set_thread_configuration(
        thread, NextTurnConfiguration(model=ChosenModel("antigravity/gemini-4-neon")),
    )
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

    # First worker captures the waking DM into pending_turns, then dies before running it.
    first_agent = CommsAgent(comms, auto_wake=True)
    sid = AcpSessionId(thread.name)
    monkeypatch.setattr(
        first_agent.inputs,
        "ensure_live_drain",
        lambda *_a, **_kw: None,
    )
    dm = comms.messaging.send("tristan", thread.name, "SURVIVE_RESTART_9852")
    ref = dm.message.reference
    first_agent.sessions.published[sid] = thread.name
    first_agent.inputs.state(sid).owned_launch = launch
    await first_agent.inputs.drain_direct_inbox(sid, thread)
    assert len(first_agent.inputs.state(sid).pending_turns) == 1
    assert [m.seq for m in comms.bus.inbox(thread.name).page.messages] == [ref.seq]
    await first_agent.shutdown()

    # Second worker starts fresh and redelivers the DM cleanly.
    second_agent = CommsAgent(comms, auto_wake=True)
    try:
        await second_agent.sessions.start_owner(launch, owner)
        summary = ""
        for _ in range(100):
            await asyncio.sleep(0.05)
            win = comms.views.message_notifications_for_references(AllOutcomes(), (ref,))
            summary = MessageNotification.summary(
                win.outcomes.get((ref.seq, ref.message_id), ())
            )
            if "1 read" in summary and not second_agent.inputs.turn_live(sid):
                break
        assert "1 read" in summary, f"Restarted worker failed to redeliver DM: {summary!r}"
        assert comms.bus.inbox(thread.name).page.messages == ()
    finally:
        await second_agent.shutdown()


@pytest.mark.asyncio
async def test_drain_direct_inbox_skips_stored_not_sent_without_replaying_or_dismissing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A direct-inbox waking message already settled as NotSentInput must neither
    loop forever through bind_turn_input/unbound_ordinary_input nor be silently
    dismissed as non-waking; a fresh subsequent waking message still drains."""
    root = tmp_path / "comms-root"
    worktree = tmp_path / "wt"
    worktree.mkdir()
    comms = prepare_native_comms(wire(root))
    comms.registry.declare(NativeThread("tristan", frozenset(), Checkout(worktree)))
    thread = comms.registry.declare(NativeThread("agy-unsent", frozenset(), Checkout(worktree)))
    thread = comms.threads.set_thread_configuration(
        thread, NextTurnConfiguration(model=ChosenModel("antigravity/gemini-4-neon")),
    )
    comms.owners.acquire_thread(thread.name)
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
    monkeypatch.setattr(agent.inputs, "ensure_live_drain", lambda *_a, **_kw: None)
    try:
        agent.sessions.published[sid] = thread.name
        agent.inputs.state(sid).owned_launch = launch

        first = comms.messaging.send("tristan", thread.name, "PRIOR_UNSENT_11150").message
        key = first.bus_key(thread)
        agent.inputs.dispositions.record_originals(
            ReservedInput(key, first.seq, thread.name, 1, first.target, first.body),
        )
        settled = agent.inputs.dispositions.settle_unbound((key,))
        assert isinstance(settled.lookup(key), NotSentInput)

        await agent.inputs.drain_direct_inbox(sid, thread)
        assert agent.inputs.state(sid).pending_turns == []
        # Still unread (never silently dismissed as ignored) and still NotSentInput.
        assert [m.seq for m in comms.bus.inbox(thread.name).page.messages] == [first.seq]
        assert isinstance(agent.inputs.dispositions.read((key,)).lookup(key), NotSentInput)

        # A fresh subsequent DM still wakes cleanly without re-scheduling the NotSentInput.
        second = comms.messaging.send("tristan", thread.name, "FRESH_DM_11151").message
        await agent.inputs.drain_direct_inbox(sid, thread)
        pending = agent.inputs.state(sid).pending_turns
        assert [m.seq for turn in pending for m in turn.origin.messages()] == [second.seq]
    finally:
        await agent.shutdown()




