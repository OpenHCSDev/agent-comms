"""ACP server tests against the official agent-client-protocol library.

Exercises the agent the way real clients (Toad, Zed) do: through
Core's ACP connection over real stdio pipes, plus direct handler-level tests.
"""

from agent_comms.bus import Bus

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from acp import RequestError

import agent_comms.agent_events as ae
import agent_comms.backend as backend
from agent_comms.channel_targets import CommandTarget
from agent_comms.acp import CommsAgent
from agent_comms.acp_extension import (
    CoordinationChangedUpdate,
    TranscriptSnapshotUpdate,
    decode_updates,
)
from agent_comms.activity import ActivityState
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import wire
from conftest import installed_comms
from agent_comms.context_reading import ContextUsage, UnmeasuredContext
from agent_comms.channels import AnyChannel
from agent_comms.errors import UnregisteredThreadError
from agent_comms.pi_counts import PiCount
from agent_comms.read_basis import NamedViewer
from agent_comms.native_arguments import GivenArguments
from delivery_owner_fixture import canonical_agent
from agent_comms.thread_model import DeclaredModel



def facts(metadata, kind):
    return tuple((f for f in decode_updates(metadata) if isinstance(f, kind)))


@pytest.fixture
def native_stdio_root():
    if not os.environ.get("AC_NATIVE_COPIED_PACKAGE"):
        pytest.skip("Requires existing prepared native package; no provider request")
    with tempfile.TemporaryDirectory(prefix="l0b-stdio-", dir="/var/tmp") as directory:
        yield Path(directory)


class TestHandlers:
    def _agent(self, tmp_path: Path) -> CommsAgent:
        return canonical_agent(wire(tmp_path / "wire"))

    async def test_initialize_echoes_protocol_version(self, tmp_path):
        agent = self._agent(tmp_path)
        response = await agent.initialize(protocol_version=1)
        assert response.protocol_version == 1
        assert response.agent_info is not None
        assert response.agent_info.name == "agent-comms"

    async def test_new_session_registers_thread_from_cwd(self, tmp_path):
        agent = self._agent(tmp_path)
        response = await agent.new_session(cwd="/home/me/my-project", mcp_servers=[])
        assert response.session_id == "my-project"
        assert facts(response.field_meta, CoordinationChangedUpdate)[0].thread.name == "my-project"
        assert "my-project" in agent._comms.registry
        thread = agent._comms.registry.require("my-project")
        assert thread.worktree == "/home/me/my-project"
        assert thread.tags == frozenset({"acp"})


    async def test_attach_metadata_restores_saved_context_without_reading_transcript(
        self, tmp_path
    ):
        agent = self._agent(tmp_path)
        await agent.new_session(cwd="/wt/proj", mcp_servers=[])
        agent._comms.agents.observe_runtime_info(
            "proj", ae.ContextUsageObservation(context_used=PiCount.from_wire(38723))
        )
        agent._comms.agents.observe_runtime_info(
            "proj", ae.AgentInfo(model=DeclaredModel('test/model'), context_size=PiCount.from_wire(272000))
        )
        reopened = canonical_agent(wire(agent._comms.root))
        assert facts((await reopened.sessions.metadata("proj")), CoordinationChangedUpdate)[
            0
        ].context_usage == ContextUsage(38723, 272000)
        agent._comms.agents.observe_runtime_info(
            "proj", ae.ContextInfo(context_used=PiCount.from_wire(None), context_size=PiCount.from_wire(272000))
        )
        assert (
            facts((await reopened.sessions.metadata("proj")), CoordinationChangedUpdate)[0].context_usage
            == UnmeasuredContext()
        )

    async def test_unknown_model_is_rejected(self, tmp_path, monkeypatch):

        monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "openrouter/one")
        agent = canonical_agent(
            wire(tmp_path / "wire"), agent_args=GivenArguments(("--provider", "openrouter", "--model", "one",))
        )
        await agent.new_session(cwd="/wt/proj", mcp_servers=[])
        with pytest.raises(RequestError):
            await agent.set_config_option(
                session_id="proj", config_id="model", value="openrouter/missing"
            )

    async def test_compaction_details_preserve_markdown(self, tmp_path):
        summary = "## Decisions\n\n" + "- Keep this decision.\n" * 80 + "\n## Next\nContinue."
        assert backend.compaction_summary(summary) == summary
        assert "\x1b" not in backend.compaction_summary("\x1b[2J\nSafe")


    @pytest.mark.parametrize("renamed", [False, True])
    async def test_snapshot_capability_replays_one_bounded_update(self, tmp_path, renamed):
        agent = self._agent(tmp_path)
        await agent.initialize(1)
        await agent.new_session("/wt/proj")
        path = tmp_path / "history.jsonl"
        records = [
            {"type": "session", "version": 3, "id": "snapshot", "timestamp": "2026-10-19T00:00:00.000Z"},
            *(
                {
                    "type": "message",
                    "id": f"message-{index}",
                    "timestamp": f"2026-10-19T00:{index // 60:02d}:{index % 60:02d}.000Z",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": str(index)}],
                    },
                }
                for index in range(100)
            ),
        ]
        path.write_text("".join(json.dumps(record) + "\n" for record in records))
        agent._comms.threads.attach_session((attachment_owner := agent._comms.registry.require("proj")), str(path), process=attachment_owner.process_identity)
        if renamed:
            agent._comms.threads.rename_managed_thread("proj", "renamed", owner=ProcessIdentity.capture(os.getpid()))
        sent = []

        class Client:
            async def session_update(self, **kwargs):
                sent.append(kwargs["update"])

        try:
            await agent.sessions.transcript.replay("proj", client=Client())
            assert len(sent) == 1
            update = facts(sent[0].field_meta, TranscriptSnapshotUpdate)[0]
            assert update.identity.requested_name == "proj"
            assert update.identity.thread.incarnation == agent._comms.registry.require("proj").incarnation
            page = update.page
            events = page.events
            assert len(events) == 20
            assert events[-1].text == "99"
            assert page.has_older
        finally:
            await agent.shutdown()

    async def test_new_session_starts_after_existing_wire_history(self, tmp_path):
        agent = canonical_agent(wire(tmp_path / "wire"))
        agent._comms.registry.declare(agent._comms.registry.new_thread(name='peer', tags=frozenset(), worktree='/wt'))
        agent._comms.messaging.send("peer", "#all", "old message").message.message_id
        response = await agent.new_session(cwd="/wt/proj", mcp_servers=[])
        assert agent._comms.bus.inbox(response.session_id) == []
        agent._comms.messaging.send("peer", "#all", "new message").message.message_id
        assert [message.body for message in agent._comms.bus.inbox(response.session_id)] == [
            "new message"
        ]

    async def test_same_cwd_allocates_distinct_threads(self, tmp_path):
        agent = self._agent(tmp_path)
        await agent.new_session(cwd="/wt/proj", mcp_servers=[])
        agent._comms.owners.stop("proj")
        response = await agent.new_session(cwd="/wt/proj", mcp_servers=[])
        names = list(agent._comms.registry.all_threads())
        assert names == ["proj", "proj-2"]
        assert response.session_id == "proj-2"
        assert agent._comms.registry.status("proj").declared_name == "stopped"

    async def test_same_leaf_different_cwd_disambiguates(self, tmp_path):
        agent = self._agent(tmp_path)
        await agent.new_session(cwd="/a/proj", mcp_servers=[])
        await agent.new_session(cwd="/b/proj", mcp_servers=[])
        names = sorted(agent._comms.registry.all_threads())
        assert names == ["proj", "proj-2"]
        assert agent._comms.registry.require("proj").worktree == "/a/proj"
        assert agent._comms.registry.require("proj-2").worktree == "/b/proj"

    async def test_separate_servers_in_same_cwd_get_distinct_threads(self, tmp_path):
        first = self._agent(tmp_path)
        second = self._agent(tmp_path)
        first_response = await first.new_session(cwd="/wt/proj", mcp_servers=[])
        second_response = await second.new_session(cwd="/wt/proj", mcp_servers=[])
        assert first_response.session_id == "proj"
        assert second_response.session_id == "proj-2"

    async def test_load_session_reconnects_persistent_thread(self, tmp_path):
        first = self._agent(tmp_path)
        response = await first.new_session(cwd="/wt/proj", mcp_servers=[])
        session_file = tmp_path / "session.jsonl"
        session_file.write_text(
            "".join(
                json.dumps(record) + "\n"
                for record in (
                    {"type": "session", "version": 3, "id": "saved", "timestamp": "2026-10-19T00:00:00.000Z"},
                    {
                        "type": "message",
                        "id": "user-message",
                        "timestamp": "2026-10-19T00:00:01.000Z",
                        "message": {
                            "role": "user",
                            "content": [{"type": "text", "text": "replayed user"}],
                        },
                    },
                    {
                        "type": "message",
                        "id": "assistant-message",
                        "timestamp": "2026-10-19T00:00:02.000Z",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "replayed response"}],
                        },
                    },
                )
            )
        )
        first._comms.threads.attach_session(
            first._comms.registry.require("proj"), str(session_file), process=ProcessIdentity.capture(os.getpid())
        )
        await first.shutdown()
        assert first._comms.registry.status("proj").declared_name == "stopped"
        second = self._agent(tmp_path)

        class FakeClient:
            def __init__(self):
                self.updates = []

            async def session_update(self, session_id=None, update=None, **kwargs):
                self.updates.append(update)

        client = FakeClient()
        second.sessions.client = client
        loaded = await second.load_session(
            cwd="/wt/proj", session_id=response.session_id, mcp_servers=[]
        )
        assert second._comms.registry.status("proj").declared_name == "running"
        assert second._comms.registry.require("proj").process_identity.runs_here()
        assert facts(loaded.field_meta, CoordinationChangedUpdate)[0].thread.name == "proj"
        snapshots = [
            fact
            for update in client.updates
            for fact in facts(update.field_meta, TranscriptSnapshotUpdate)
        ]
        assert len(snapshots) == 1
        assert [event.text for event in snapshots[0].page.events] == [
            "replayed user",
            "replayed response",
        ]

    async def test_prompt_broadcasts_to_global_channel(self, tmp_path):
        agent = canonical_agent(wire(tmp_path / "wire"))
        await agent.new_session(cwd="/wt/proj", mcp_servers=[])
        response = await agent.prompt(
            session_id="proj", prompt=[{"type": "text", "text": "!relay hello all"}]
        )
        assert response.stop_reason == "end_turn"
        assert not agent.turns.turn_state("proj").active.phase.busy
        history = list(agent._comms.views.history_page(CommandTarget.named("#all")).messages)
        assert [m.body for m in history] == ["hello all"]

    async def test_unknown_session_fails_closed(self, tmp_path):

        agent = self._agent(tmp_path)
        with pytest.raises(RequestError):
            await agent.prompt(session_id="nope", prompt=[{"type": "text", "text": "x"}])

    async def test_cwd_leaf_sanitized_for_thread_name(self, tmp_path):
        agent = self._agent(tmp_path)
        await agent.new_session(cwd="/wt/My Project!", mcp_servers=[])
        assert "My-Project" in agent._comms.registry

    async def test_cancel_preserves_unbound_inbox(self, tmp_path):
        agent = self._agent(tmp_path)
        await agent.new_session(cwd="/wt/proj", mcp_servers=[])
        agent._comms.registry.declare(agent._comms.registry.new_thread(name='peer', tags=frozenset(), worktree='/wt'))
        agent._comms.messaging.send("peer", "proj", "hello").message.message_id
        await agent.cancel(session_id="proj")
        assert agent._comms.bus.pending_count("proj") == 1


class TestWireProtocol:
    @pytest.mark.skipif(
        sys.platform == "win32", reason="selectors cannot poll Windows pipe handles"
    )
    def test_real_stdio_roundtrip(self, native_stdio_root):
        """initialize -> session/new -> prompt over real stdio pipes."""
        import selectors

        tmp_path = native_stdio_root
        (tmp_path / "proj").mkdir()
        root = tmp_path / "wire"
        comms = wire(root)
        Bus.install(Bus.path_in(comms.root))
        env = dict(
            __import__("os").environ,
            AGENT_COMMS_ROOT=str(root),
            PYTHONPATH=str(Path(ae.__file__).parent.parent),
            AGENT_COMMS_AGENT_BIN="pi",
            AGENT_COMMS_AGENT_ARGS="",
        )
        requests = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": 1, "clientCapabilities": {}},
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/new",
                "params": {"cwd": str(tmp_path / "proj"), "mcpServers": []},
            },
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "session/prompt",
                "params": {
                    "sessionId": "proj",
                    "prompt": [{"type": "text", "text": "!relay anyone alive?"}],
                },
            },
        ]
        proc = subprocess.Popen(
            [sys.executable, "-m", "agent_comms.acp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            bufsize=0,
        )
        out_lines: list[str] = []
        comms = wire(root)
        try:
            assert proc.stdin is not None and proc.stdout is not None
            import time as _time

            pending = b""
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ)
                for request in requests:
                    proc.stdin.write((json.dumps(request) + "\n").encode())
                    proc.stdin.flush()
                    deadline = _time.monotonic() + 30
                    while True:
                        if b"\n" not in pending:
                            assert selector.select(
                                max(0, deadline - _time.monotonic())
                            ), "ACP timeout"
                            chunk = os.read(proc.stdout.fileno(), 65536)
                            assert chunk, "ACP closed before response"
                            pending += chunk
                            continue
                        raw, pending = pending.split(b"\n", 1)
                        line = raw.decode()
                        out_lines.append(line)
                        message = json.loads(line)
                        if message.get("id") == request["id"]:
                            assert "result" in message, message
                            break
        finally:
            if proc.stdin is not None:
                proc.stdin.close()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            if "proj" in comms.registry:
                owner = comms.registry.require("proj").require_process().pid
                assert owner != proc.pid
                assert comms.registry.require("proj").process_alive
                comms.owners.stop("proj")
        responses = {
            m.get("id"): m for m in (json.loads(line) for line in out_lines if line.strip())
        }
        assert responses[1]["result"]["protocolVersion"] == 1
        assert responses[2]["result"]["sessionId"] == "proj"
        assert responses[3]["result"]["stopReason"] == "end_turn"
        assert "proj" in comms.registry
        assert [m.body for m in list(comms.views.history_page(CommandTarget.named("#all")).messages)] == ["anyone alive?"]


class TestLiveDrain:
    async def test_cancel_keeps_background_drain_live(self, tmp_path):
        agent = canonical_agent(wire(tmp_path / "wire"))
        await agent.new_session(cwd=str(tmp_path / "proj"), mcp_servers=[])
        await agent.prompt(session_id="proj", prompt=[{"type": "text", "text": "!relay hi"}])
        assert agent.inputs.state("proj").drain_task is not None
        await agent.cancel(session_id="proj")
        await asyncio.sleep(0.05)
        assert agent.inputs.state("proj").drain_task is not None
        assert not agent.inputs.state("proj").drain_task.done()
        await agent.shutdown()


class TestFullHistory:
    def test_full_history_is_everything_in_order(self, tmp_path):
        comms = installed_comms(tmp_path / "wire")
        comms.registry.declare(comms.registry.new_thread(name='a', tags=frozenset({'x'}), worktree='/wt'))
        comms.registry.declare(comms.registry.new_thread(name='b', tags=frozenset(), worktree='/wt'))
        comms.messaging.send("a", "#all", "one").message.message_id
        comms.messaging.send("a", "b", "dm").message.message_id
        comms.messaging.send("b", "#x", "tagged").message.message_id
        bodies = [(m.sender, m.target, m.body) for m in list(comms.views.history_page(AnyChannel.target()).messages)]
        assert bodies == [("a", "#all", "one"), ("a", "b", "dm"), ("b", "#x", "tagged")]


class TestActivityLayer:
    def test_emit_and_read_current(self, wired):

        from agent_comms.presentation import WireRevision
        from agent_comms.turn_phase import ToolRunningPhase

        wired.registry.declare(wired.registry.new_thread(
            "activity-owner", frozenset(), str(wired.root),
            process_identity=ProcessIdentity.capture(os.getpid()),
        ))
        owner = wired.agents.begin_turn("activity-owner", "activity-test")
        try:
            before = WireRevision.capture(wired.views, expiry_tick=0)
            wired.agents.transition_turn(owner.thread.turn_lease, ToolRunningPhase(detail="bash: echo hi"))
            activity = wired.agents.activity_of(owner.thread.name, snapshot=wired.registry.snapshot())
            assert activity.state is ActivityState.WORKING
            assert activity.detail == "bash: echo hi"
            working = WireRevision.capture(wired.views, expiry_tick=0)
            assert working.activity != before.activity
        finally:
            wired.agents.finish_turn(owner.thread.turn_lease)
        assert WireRevision.capture(wired.views, expiry_tick=0).activity != working.activity

    def test_idle_cannot_carry_detail(self, wired):

        with pytest.raises(Exception):
            wired.agents.set_activity("PR111", ActivityState.IDLE, "junk")

    def test_unknown_thread_fail_closed(self, wired):

        with pytest.raises(UnregisteredThreadError):
            wired.agents.set_activity("ghost", ActivityState.THINKING)

    def test_stale_activity_reads_idle(self, wired):
        import time as _time

        from agent_comms.activity import Activity

        wired.agents.activity.emit(
            Activity(thread="PR111", state=ActivityState.WORKING, detail="old",
                     timestamp=_time.time() - 1000)
        )
        assert wired.agents.activity_of("PR111", snapshot=wired.registry.snapshot()).state is ActivityState.IDLE

    def test_activity_persistence(self, tmp_path):
        from agent_comms.activity import Activity, ActivityStore

        store = ActivityStore.at(tmp_path)
        store.emit(Activity(thread="a", state=ActivityState.WORKING, detail="bash"))
        assert ActivityStore.at(tmp_path).current("a").detail == "bash"

    def test_all_current_latest_per_thread(self, tmp_path):
        from agent_comms.activity import Activity, ActivityStore

        store = ActivityStore.at(tmp_path)
        store.emit(Activity(thread="a", state=ActivityState.THINKING))
        store.emit(Activity(thread="a", state=ActivityState.WORKING, detail="bash"))
        store.emit(Activity(thread="b", state=ActivityState.THINKING))
        current = store.all_current()
        assert current["a"].state is ActivityState.WORKING
        assert current["b"].state is ActivityState.THINKING


class TestTargetPrefix:
    def test_dm_prefix(self):
        from agent_comms.turn_runner import parse_target

        assert parse_target("@fixer hello there") == ("fixer", "hello there")

    def test_channel_prefix(self):
        from agent_comms.turn_runner import parse_target

        assert parse_target("#ci flake again") == ("#ci", "flake again")

    def test_plain_goes_global(self):
        from agent_comms.turn_runner import parse_target

        assert parse_target("plain text") == ("#all", "plain text")

    def test_bare_target_not_sent(self):
        from agent_comms.turn_runner import parse_target

        assert parse_target("@fixer")[0] == "#all"

    async def test_dm_prompt_reaches_only_target(self, wired):
        agent = canonical_agent(wired)
        wired.registry.declare(wired.registry.new_thread(name='peer', tags=frozenset(), worktree='/wt'))
        await agent.new_session(cwd="/wt/proj", mcp_servers=[])
        await agent.prompt(session_id="proj", prompt=[{"type": "text", "text": "@peer hi peer"}])
        assert wired.bus.pending_count("peer") == 1
        assert wired.bus.pending_count("PR111") == 0
        assert [m.body for m in list(wired.views.history_page(CommandTarget.named("peer"), viewer=NamedViewer("proj")).messages)] == ["hi peer"]




