"""ACP server tests against the official agent-client-protocol library.

Exercises the agent the way real clients (Toad, Zed) do: through
``acp.run_agent`` over real stdio pipes, plus direct handler-level tests.
"""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

import pytest
from acp.schema import ConfigOptionUpdate, SessionInfoUpdate

from agent_comms.acp import CommsAgent
from agent_comms.backend import NATIVE_INPUT_CAPABILITY
from agent_comms.declarations import ActivityState, UnregisteredThreadError
from agent_comms.operations import wire
from agent_comms.runtime import RuntimeProxy, socket_path


@pytest.fixture(autouse=True)
def _model_catalog_without_a_local_pi_process(monkeypatch):
    # These ACP handler tests use fake backends. Keep model discovery local so
    # an installed Pi process cannot make an unrelated handler test stall.
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "openrouter/z-ai/glm-5.3-flash")


def _update_text(update) -> str:
    """Update text for chunk-like updates; config updates carry none."""
    content = getattr(update, "content", None)
    return content.text or "" if content is not None else ""


class TestHandlers:
    def _agent(self, tmp_path: Path) -> CommsAgent:
        return CommsAgent(
            wire(tmp_path / "wire"), reply_window=0.1, no_reply_window=0.1, reply_quiet=0.05
        )

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
        assert response.field_meta["agentComms"]["thread"] == "my-project"
        assert "my-project" in agent._comms.registry
        thread = agent._comms.registry.require("my-project")
        assert thread.worktree == "/home/me/my-project"
        assert thread.tags == frozenset({"acp"})

    async def test_session_model_config_is_persisted_and_selectable(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "openrouter/one,anthropic/two")
        agent = CommsAgent(
            wire(tmp_path / "wire"),
            agent_args=["--print", "--provider", "openrouter", "--model", "one"],
        )

        response = await agent.new_session(cwd="/wt/proj", mcp_servers=[])
        model_config = response.config_options[0]
        assert model_config.current_value == "openrouter/one"
        assert [option.value for option in model_config.options] == [
            "openrouter/one",
            "anthropic/two",
        ]
        thinking_config = response.config_options[1]
        assert thinking_config.current_value == "medium"
        assert [option.value for option in thinking_config.options] == [
            "off",
            "minimal",
            "low",
            "medium",
            "high",
        ]

        changed = await agent.set_config_option(
            session_id="proj", config_id="model", value="anthropic/two"
        )

        assert changed.config_options[0].current_value == "anthropic/two"
        assert agent._comms.registry.require("proj").model == "anthropic/two"
        changed = await agent.set_config_option(
            session_id="proj", config_id="thinking_level", value="high"
        )
        assert changed.config_options[1].current_value == "high"
        assert agent._comms.registry.require("proj").thinking_level == "high"

    async def test_attach_metadata_restores_saved_context_without_reading_transcript(
        self, tmp_path
    ):
        agent = self._agent(tmp_path)
        await agent.new_session(cwd="/wt/proj", mcp_servers=[])
        agent._comms.set_agent_info(
            "proj", model="test/model", context_used=38723, context_size=272000
        )
        reopened = CommsAgent(wire(agent._comms.root))
        assert reopened._session_metadata("proj")["agentComms"]["contextUsage"] == {
            "used": 38723,
            "size": 272000,
            "source": "last_response",
        }
        agent._comms.set_agent_info(
            "proj", model="test/model", context_used=None, context_size=272000
        )
        assert reopened._session_metadata("proj")["agentComms"]["contextUsage"] is None

    async def test_unknown_model_is_rejected(self, tmp_path, monkeypatch):
        from acp import RequestError

        monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "openrouter/one")
        agent = CommsAgent(
            wire(tmp_path / "wire"),
            agent_args=["--provider", "openrouter", "--model", "one"],
        )
        await agent.new_session(cwd="/wt/proj", mcp_servers=[])

        with pytest.raises(RequestError):
            await agent.set_config_option(
                session_id="proj", config_id="model", value="openrouter/missing"
            )

    async def test_compaction_details_preserve_markdown(self, tmp_path):
        summary = "## Decisions\n\n" + "- Keep this decision.\n" * 80 + "\n## Next\nContinue."
        assert CommsAgent._sanitized_compaction_summary(summary) == summary
        assert "\x1b" not in CommsAgent._sanitized_compaction_summary("\x1b[2J\nSafe")

    async def test_compaction_is_an_owner_operation_with_result_metadata(
        self, tmp_path, monkeypatch
    ):
        agent = self._agent(tmp_path)
        await agent.new_session(cwd=str(tmp_path / "proj"), mcp_servers=[])
        session_file = tmp_path / "session.jsonl"
        session_file.touch()
        agent._comms.attach_session("proj", str(session_file))
        received: dict[str, object] = {}

        async def compact_session(*args, **kwargs):
            received["args"] = args
            received["kwargs"] = kwargs
            return {
                "ok": True,
                "summary": "Preserved decisions.",
                "tokensBefore": 8000,
                "estimatedTokensAfter": 1000,
            }

        monkeypatch.setattr(
            "agent_comms.manual_compaction_bridge.manual_compaction.compact_session",
            compact_session,
        )

        response = await agent.prompt(
            "proj",
            [{"type": "text", "text": " "}],
            field_meta={"agentComms": {"compact": "keep test findings"}},
        )

        result = response.field_meta["agentComms"]["compaction"]
        assert result["ok"] is True
        assert result["summary"] == "Preserved decisions."
        assert received["args"][-1] == "keep test findings"
        # The bridge does not promote estimated post-compaction usage to a
        # measured context receipt; it remains unknown until a fresh sample.
        assert agent._comms.agent_info_of("proj").context_used is None
        assert agent._comms.registry.require("proj").active_turn is None

    async def test_compaction_has_owner_lifecycle_and_rejects_concurrent_work(
        self, tmp_path, monkeypatch
    ):
        agent = self._agent(tmp_path)
        await agent.new_session(cwd=str(tmp_path / "proj"), mcp_servers=[])
        session_file = tmp_path / "session.jsonl"
        session_file.touch()
        agent._comms.attach_session("proj", str(session_file))
        entered = asyncio.Event()
        release = asyncio.Event()

        async def compact_session(*args, **kwargs):
            entered.set()
            await release.wait()
            return {"ok": True, "summary": "done"}

        updates: list = []

        class FakeClient:
            async def session_update(self, session_id=None, update=None, **kwargs):
                updates.append(update)

        agent._client = FakeClient()
        monkeypatch.setattr(
            "agent_comms.manual_compaction_bridge.manual_compaction.compact_session",
            compact_session,
        )
        compaction = asyncio.create_task(agent.compact_context("proj"))
        await asyncio.wait_for(entered.wait(), timeout=1)

        active = agent._comms.registry.require("proj").active_turn
        assert active is not None and active.id.startswith("compaction-")
        assert active.started_at > 0
        assert agent._active_turns["proj"] == active.id
        assert agent._turn_tasks["proj"] is compaction
        activity = agent._comms.activity_of("proj")
        assert activity.state.value == "working"
        assert activity.detail == "Compacting context"
        started = next(
            update.field_meta["agentComms"]
            for update in updates
            if update.field_meta.get("agentComms", {}).get("turnStarted")
        )
        assert started == {
            "turnStarted": True,
            "turnId": active.id,
            "startedAt": active.started_at,
            "activity": "working",
            "activityDetail": "Compacting context",
        }
        rejected = await agent.compact_context("proj")
        assert rejected["ok"] is False and "current response" in rejected["error"]

        release.set()
        assert (await compaction)["ok"] is True
        assert agent._comms.registry.require("proj").active_turn is None
        assert "proj" not in agent._active_turns
        assert "proj" not in agent._turn_tasks
        assert agent._comms.activity_of("proj").state.value == "idle"

    async def test_replay_uses_owner_turn_timestamp_and_activity(self, tmp_path):
        agent = self._agent(tmp_path)
        await agent.new_session(cwd=str(tmp_path / "proj"), mcp_servers=[])
        turn_id = "compaction-existing"
        agent._comms.begin_turn("proj", turn_id, "Compacting context")
        agent._comms.set_activity("proj", ActivityState.WORKING, "Compacting context")
        active = agent._comms.registry.require("proj").active_turn
        updates: list = []

        class FakeClient:
            async def session_update(self, session_id=None, update=None, **kwargs):
                updates.append(update)

        await agent.replay_turn_state("proj", client=FakeClient())

        assert active is not None
        assert updates[0].field_meta == {
            "agentComms": {
                "turnStarted": True,
                "turnId": turn_id,
                "startedAt": active.started_at,
                "activity": "working",
                "activityDetail": "Compacting context",
            }
        }
        agent._comms.finish_turn("proj", turn_id)

    async def test_cancel_cleans_up_compaction_lifecycle(self, tmp_path, monkeypatch):
        agent = self._agent(tmp_path)
        await agent.new_session(cwd=str(tmp_path / "proj"), mcp_servers=[])
        session_file = tmp_path / "session.jsonl"
        session_file.touch()
        agent._comms.attach_session("proj", str(session_file))
        entered = asyncio.Event()

        async def compact_session(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
            return {"ok": True}

        monkeypatch.setattr(
            "agent_comms.manual_compaction_bridge.manual_compaction.compact_session",
            compact_session,
        )
        compaction = asyncio.create_task(agent.compact_context("proj"))
        await asyncio.wait_for(entered.wait(), timeout=1)

        await agent.cancel("proj")

        assert compaction.cancelled()
        assert agent._comms.registry.require("proj").active_turn is None
        assert "proj" not in agent._active_turns
        assert "proj" not in agent._turn_tasks
        assert agent._comms.activity_of("proj").state.value == "idle"

    async def test_compaction_refuses_to_interrupt_an_active_turn(self, tmp_path):
        agent = self._agent(tmp_path)
        await agent.new_session(cwd=str(tmp_path / "proj"), mcp_servers=[])
        agent._active_turns["proj"] = "running"

        result = await agent.compact_context("proj")

        assert result["ok"] is False
        assert "current response" in result["error"]

    async def test_live_model_change_waits_for_backend_confirmation(self, tmp_path, monkeypatch):
        from acp import RequestError

        monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/one,test/two")
        agent = CommsAgent(wire(tmp_path), agent_args=["--provider", "test", "--model", "one"])
        await agent.new_session("/wt/proj")
        agent._active_turns["proj"] = "turn"
        inbox = agent._backend_inboxes["proj"] = asyncio.Queue()
        try:
            for accepted in (False, True):
                request = asyncio.create_task(agent.set_config_option("model", "proj", "test/two"))
                command = await asyncio.wait_for(inbox.get(), timeout=1)
                assert command["type"] == "set_model"
                assert command["provider"] == "test" and command["modelId"] == "two"
                assert agent._comms.registry.require("proj").model == "test/one"
                future = agent._model_requests[command["id"]]
                if accepted:
                    future.set_result(None)
                    await request
                else:
                    future.set_exception(RuntimeError("model unavailable"))
                    with pytest.raises(RequestError):
                        await request
            assert agent._comms.registry.require("proj").model == "test/two"

            request = asyncio.create_task(agent.set_config_option("thinking_level", "proj", "high"))
            command = await asyncio.wait_for(inbox.get(), timeout=1)
            assert command["type"] == "set_thinking_level" and command["level"] == "high"
            agent._thinking_requests[command["id"]].set_result(None)
            await request
            assert agent._comms.registry.require("proj").thinking_level == "high"
        finally:
            await agent.shutdown()

    async def test_snapshot_capability_replays_one_bounded_update(self, tmp_path):
        agent = self._agent(tmp_path)
        await agent.initialize(1, {"_meta": {"agentComms": {"transcriptSnapshots": True}}})
        await agent.new_session("/wt/proj")
        path = tmp_path / "history.jsonl"
        path.write_text(
            "\n".join(
                json.dumps(
                    {"type": "message", "message": {"role": "assistant", "content": str(index)}}
                )
                for index in range(100)
            )
        )
        agent._comms.attach_session("proj", str(path))
        sent = []

        class Client:
            transcript_snapshots = True

            async def session_update(self, **kwargs):
                sent.append(kwargs["update"])

        try:
            await agent._replay_transcript("proj", "proj", client=Client())
            assert len(sent) == 1
            events = sent[0].field_meta["agentComms"]["transcript"]
            assert len(events) == 20
            assert events[-1]["text"] == "99"
            assert sent[0].field_meta["agentComms"]["transcriptPage"]["has_older"]
        finally:
            await agent.shutdown()

    async def test_new_session_starts_after_existing_wire_history(self, tmp_path):
        agent = self._agent(tmp_path)
        agent._comms.register(Thread(name="peer", tags=frozenset(), worktree="/wt"))
        agent._comms.send("peer", "#all", "old message")

        response = await agent.new_session(cwd="/wt/proj", mcp_servers=[])
        assert agent._comms.inbox(response.session_id) == []

        agent._comms.send("peer", "#all", "new message")
        assert [message.body for message in agent._comms.inbox(response.session_id)] == [
            "new message"
        ]

    async def test_same_cwd_allocates_distinct_threads(self, tmp_path):
        agent = self._agent(tmp_path)
        await agent.new_session(cwd="/wt/proj", mcp_servers=[])
        agent._comms.stop("proj")
        response = await agent.new_session(cwd="/wt/proj", mcp_servers=[])
        names = list(agent._comms.registry.all_threads())
        assert names == ["proj", "proj-2"]
        assert response.session_id == "proj-2"
        assert agent._comms.registry.status("proj").value == "stopped"

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
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "message",
                            "message": {
                                "role": "user",
                                "content": [{"type": "text", "text": "replayed user"}],
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "message",
                            "message": {
                                "role": "assistant",
                                "content": [{"type": "text", "text": "replayed response"}],
                            },
                        }
                    ),
                ]
            )
        )
        first._comms.attach_session("proj", str(session_file), pid=os.getpid())
        await first.shutdown()
        assert first._comms.registry.status("proj").value == "stopped"

        second = self._agent(tmp_path)

        class FakeClient:
            def __init__(self):
                self.updates = []

            async def session_update(self, session_id=None, update=None, **kwargs):
                self.updates.append(update)

        client = FakeClient()
        second._client = client
        loaded = await second.load_session(
            cwd="/wt/proj", session_id=response.session_id, mcp_servers=[]
        )
        assert second._comms.registry.status("proj").value == "running"
        assert second._comms.registry.require("proj").pid == os.getpid()
        assert loaded.field_meta["agentComms"]["thread"] == "proj"
        assert [update.session_update for update in client.updates] == [
            "user_message_chunk",
            "agent_message_chunk",
        ]

    async def test_prompt_broadcasts_to_global_channel(self, tmp_path):
        agent = self._agent(tmp_path)
        await agent.new_session(cwd="/wt/proj", mcp_servers=[])
        response = await agent.prompt(
            session_id="proj", prompt=[{"type": "text", "text": "!relay hello all"}]
        )
        assert response.stop_reason == "end_turn"
        history = agent._comms.channel_history("#all")
        assert [m.body for m in history] == ["hello all"]

    async def test_unknown_session_fails_closed(self, tmp_path):
        from acp import RequestError

        agent = self._agent(tmp_path)
        with pytest.raises(RequestError):
            await agent.prompt(session_id="nope", prompt=[{"type": "text", "text": "x"}])

    async def test_cwd_leaf_sanitized_for_thread_name(self, tmp_path):
        agent = self._agent(tmp_path)
        await agent.new_session(cwd="/wt/My Project!", mcp_servers=[])
        assert "My-Project" in agent._comms.registry

    async def test_cancel_drains_inbox(self, tmp_path):
        agent = self._agent(tmp_path)
        await agent.new_session(cwd="/wt/proj", mcp_servers=[])
        agent._comms.register(Thread(name="peer", tags=frozenset(), worktree="/wt"))
        agent._comms.send("peer", "proj", "hello")
        await agent.cancel(session_id="proj")
        assert agent._comms.pending_count("proj") == 0

    async def test_cancel_ends_active_relay_turn(self, tmp_path):
        agent = CommsAgent(
            wire(tmp_path / "wire"),
            reply_window=30,
            no_reply_window=30,
            reply_quiet=30,
        )
        await agent.new_session(cwd="/wt/proj", mcp_servers=[])
        prompt = asyncio.create_task(
            agent.prompt(
                session_id="proj",
                prompt=[{"type": "text", "text": "!relay wait for a reply"}],
            )
        )
        for _ in range(20):
            if "proj" in agent._turn_tasks:
                break
            await asyncio.sleep(0)

        await agent.cancel(session_id="proj")

        assert (await asyncio.wait_for(prompt, timeout=1)).stop_reason == "cancelled"
        assert "proj" not in agent._turn_tasks
        assert not agent._drain_tasks["proj"].done()
        await agent.shutdown()


from agent_comms import Thread  # noqa: E402


class TestAgentTurn:
    """``!agent`` prompts forward to a real agent binary."""

    pytestmark = pytest.mark.skipif(
        sys.platform == "win32",
        reason="agent-turn tests exec shell-script stubs; POSIX only",
    )

    def _agent_with_stub(self, tmp_path: Path, wired) -> CommsAgent:
        stub = tmp_path / "fake-agent"
        stub.write_text("#!/bin/sh\nprintf '%s' \"$1\"\n")
        stub.chmod(0o755)
        return CommsAgent(wired, agent_bin=str(stub), agent_args=[])

    def _authorize_test_goal(self, agent: CommsAgent, wired, goal) -> None:
        """Provide the private owner grant used by goal-turn tests with fake Pi events."""
        from agent_comms.goal_attempts import GoalAttemptStore

        agent._agent_bin = "pi"
        private = wired.root / "goal-private"
        private.mkdir(mode=0o700, exist_ok=True)
        store = GoalAttemptStore.initialize(private)
        store.create_goal(goal.id)
        agent._goal_store = store

    async def test_agent_turn_streams_reply_without_broadcasting(self, wired, tmp_path):
        agent = self._agent_with_stub(tmp_path, wired)
        wired.register(Thread(name="peer", tags=frozenset(), worktree="/wt"))

        sent: list = []

        class FakeClient:
            async def session_update(self, session_id=None, update=None, **kw):
                sent.append(update)

        agent._client = FakeClient()
        await agent.new_session(cwd=str(tmp_path / "proj"), mcp_servers=[])
        await agent.prompt(
            session_id="proj",
            prompt=[{"type": "text", "text": "!agent fix the flake"}],
        )
        response = "".join(getattr(getattr(update, "content", None), "text", "") for update in sent)
        assert response.endswith("fix the flake")
        assert "you are thread 'proj'" in response
        assert agent._comms.full_history() == []

    async def test_agent_turn_runs_in_thread_worktree(self, wired, tmp_path):
        worktree = tmp_path / "somewhere"
        worktree.mkdir()
        stub = tmp_path / "cwd-capture"
        stub.write_text("#!/bin/sh\npwd\n")
        stub.chmod(0o755)
        agent = CommsAgent(
            wired,
            agent_bin=str(stub),
            agent_args=[],
            reply_window=0.2,
            no_reply_window=0.1,
            reply_quiet=0.05,
        )
        sent: list = []

        class FakeClient:
            async def session_update(self, session_id=None, update=None, **kw):
                sent.append(update)

        agent._client = FakeClient()
        await agent.new_session(cwd=str(worktree), mcp_servers=[])
        await agent.prompt(
            session_id="somewhere", prompt=[{"type": "text", "text": "!agent where am i"}]
        )
        history = [getattr(getattr(update, "content", None), "text", "") for update in sent]
        assert any(str(worktree) in body for body in history)

    async def test_missing_agent_bin_is_reported_not_raised(self, wired, tmp_path):
        agent = CommsAgent(
            wired,
            agent_bin="definitely-not-a-real-binary-xyz",
            agent_args=[],
            reply_window=0.2,
            no_reply_window=0.1,
            reply_quiet=0.05,
        )
        sent: list = []

        class FakeClient:
            async def session_update(self, session_id=None, update=None, **kw):
                sent.append(update)

        agent._client = FakeClient()
        await agent.new_session(cwd=str(tmp_path / "proj"), mcp_servers=[])
        response = await agent.prompt(
            session_id="proj", prompt=[{"type": "text", "text": "!agent hello"}]
        )
        assert response.stop_reason == "end_turn"
        assert any("not found" in _update_text(u) for u in sent)

    async def test_plain_prompt_launches_agent(self, wired, tmp_path):
        agent = self._agent_with_stub(tmp_path, wired)
        await agent.new_session(cwd=str(tmp_path / "proj"), mcp_servers=[])
        response = await agent.prompt(
            session_id="proj", prompt=[{"type": "text", "text": "just chat"}]
        )
        assert response.stop_reason == "end_turn"
        assert wired.channel_history("#all") == []

    async def test_cancel_terminates_active_backend_process(self, wired, tmp_path):
        pid_path = tmp_path / "backend.pid"
        stub = tmp_path / "slow-agent"
        stub.write_text(f"#!/bin/sh\necho $$ > {pid_path}\nexec sleep 30\n")
        stub.chmod(0o755)
        agent = CommsAgent(wired, agent_bin=str(stub), agent_args=[])
        await agent.new_session(cwd=str(tmp_path / "proj"), mcp_servers=[])
        prompt = asyncio.create_task(
            agent.prompt(
                session_id="proj",
                prompt=[{"type": "text", "text": "run until cancelled"}],
            )
        )
        for _ in range(100):
            if pid_path.exists():
                break
            await asyncio.sleep(0.01)
        assert pid_path.exists()
        pid = int(pid_path.read_text())

        await agent.cancel(session_id="proj")

        assert (await asyncio.wait_for(prompt, timeout=2)).stop_reason == "cancelled"
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        await agent.shutdown()

    async def test_pi_session_name_stays_runtime_metadata(self, wired, tmp_path, monkeypatch):
        agent = self._agent_with_stub(tmp_path, wired)
        sent: list = []

        class FakeClient:
            async def session_update(self, session_id=None, update=None, **kw):
                sent.append(update)

        async def events(*args, **kwargs):
            yield {
                "type": "agent_info",
                "session_name": "Agent-chosen title",
                "model": "test/model",
            }
            yield {
                "type": "agent_info",
                "session_name": "Agent-chosen title",
                "model": "test/model",
            }

        monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", events)
        agent._client = FakeClient()
        await agent.new_session(cwd=str(tmp_path / "proj"), mcp_servers=[])

        await agent._run_agent_turn("proj", "proj", "name this session")

        title_updates = [
            update
            for update in sent
            if isinstance(update, SessionInfoUpdate) and "title" in update.model_fields_set
        ]
        assert title_updates == []
        assert wired.agent_info_of("proj").session_name == "Agent-chosen title"

    async def test_backend_session_is_persisted_and_live_inbox_is_steered(
        self, wired, tmp_path, monkeypatch
    ):
        agent = self._agent_with_stub(tmp_path, wired)
        agent._agent_bin = "pi"  # Event source below is a mocked native Pi RPC stream.
        session_file = tmp_path / "pi-session.jsonl"
        calls: list[dict] = []
        steered: list[str] = []

        class FakeClient:
            async def session_update(self, **kwargs):
                pass

        async def events(*args, **kwargs):
            calls.append(kwargs)
            queue = kwargs["steering_queue"]
            while not queue.empty():
                steered.append(queue.get_nowait())
            yield {
                "type": "agent_info",
                "session_file": str(session_file),
                "model": "test/model",
            }
            yield {"type": "settled"}

        monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", events)
        agent._client = FakeClient()
        await agent.new_session(cwd=str(tmp_path / "proj"), mcp_servers=[])
        wired.register(Thread(name="peer", tags=frozenset(), worktree=str(tmp_path / "proj")))
        wired.send("peer", "proj", "ping parent")

        await agent._run_agent_turn("proj", "proj", "coordinate with the child")
        await agent._run_agent_turn("proj", "proj", "continue")

        assert "ping parent" in (
            steered[0]["message"] if isinstance(steered[0], dict) else steered[0]
        )
        assert wired.registry.require("proj").session_file == str(session_file)
        assert calls[0]["session_file"] is None
        assert calls[1]["session_file"] == str(session_file)
        assert calls[0]["finish_event"].is_set()

    async def test_thread_rename_updates_acp_title(self, wired, tmp_path, monkeypatch):
        agent = self._agent_with_stub(tmp_path, wired)
        sent: list = []

        class FakeClient:
            async def session_update(self, session_id=None, update=None, **kw):
                sent.append(update)

        async def events(*args, **kwargs):
            monkeypatch.setenv("PI_AGENT_ID", "proj")
            wired.rename_self("renamed-proj")
            yield {"type": "tool_end", "id": "rename", "ok": True, "output": "renamed"}

        monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", events)
        agent._client = FakeClient()
        await agent.new_session(cwd=str(tmp_path / "proj"), mcp_servers=[])
        await agent._run_agent_turn("proj", "proj", "rename yourself")

        title_updates = [
            update
            for update in sent
            if isinstance(update, SessionInfoUpdate) and "title" in update.model_fields_set
        ]
        assert [update.title for update in title_updates] == ["renamed proj"]
        assert title_updates[0].field_meta == {"agentComms": {"thread": "renamed-proj"}}
        assert agent._sessions["proj"] == "renamed-proj"

    async def test_provider_failure_is_visible_once_and_blocks_the_goal(
        self, wired, tmp_path, monkeypatch
    ):
        agent = self._agent_with_stub(tmp_path, wired)
        sent: list = []

        class FakeClient:
            async def session_update(self, session_id=None, update=None, **kw):
                sent.append(update)

        message = "Codex error: The usage limit has been reached"

        async def events(*args, **kwargs):
            yield {"type": "error", "text": message}
            yield {"type": "settled"}
            yield {"type": "done", "ok": False, "text": message}

        monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", events)
        agent._client = FakeClient()
        await agent.new_session(cwd=str(tmp_path / "proj"), mcp_servers=[])
        goal = wired.update_goal("proj", "set", text="Ship the release")
        self._authorize_test_goal(agent, wired, goal)
        await agent._run_agent_turn("proj", "proj", "work")

        texts = [
            update.content.text
            for update in sent
            if getattr(update, "content", None) is not None and update.content.text
        ]
        assert texts.count(f"[agent error] {message}") == 1
        goal = wired.registry.require("proj").goal
        assert goal is not None and goal.status == "blocked"
        assert goal.progress == "Backend turn failed; inspect local diagnostics before resuming."
        assert message not in goal.progress
        assert not wired.registry.require("proj").executing

    @pytest.mark.parametrize("completed_in_turn", [False, True])
    async def test_missing_terminal_blocks_only_still_active_goal(
        self, wired, tmp_path, monkeypatch, completed_in_turn
    ):
        agent = self._agent_with_stub(tmp_path, wired)
        await agent.new_session(cwd=str(tmp_path / "proj"), mcp_servers=[])
        goal = wired.update_goal("proj", "set", text="Ship the release")
        self._authorize_test_goal(agent, wired, goal)
        secret = "provider stderr SECRET_PRIVATE_937"

        async def events(*args, **kwargs):
            yield {"type": "error", "text": secret}
            if completed_in_turn:
                wired.update_goal(
                    "proj",
                    "completed",
                    goal_id=goal.id,
                    progress="Verified complete",
                    model_report=True,
                )
            yield {"type": "settled"}
            # EOF without done: never let the live drain retry an active goal.

        monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", events)
        await agent._run_agent_turn("proj", "proj", "work")
        result = wired.registry.require("proj").goal
        assert result is not None
        assert result.status == "blocked"
        if completed_in_turn:
            assert result.progress == (
                "Verified complete\n\nGoal turn ended without verified terminal progress."
            )
        else:
            assert (
                result.progress == "Backend turn ended without a result; inspect local diagnostics."
            )
        assert secret not in result.progress
        assert "proj" not in agent._emitted_errors
        agent._schedule_goal("proj")
        assert not agent._pending_turns.get("proj")

    async def test_error_dedup_is_scoped_to_one_turn_and_cancel_clears_it(
        self, wired, tmp_path, monkeypatch
    ):
        agent = self._agent_with_stub(tmp_path, wired)
        sent: list = []

        class FakeClient:
            async def session_update(self, session_id=None, update=None, **kw):
                sent.append(update)
                if _update_text(update) == "[agent error] busy":
                    seen_error.set()

        seen_error = asyncio.Event()
        agent._client = FakeClient()
        await agent.new_session(cwd=str(tmp_path / "proj"), mcp_servers=[])
        turns = 0

        async def events(*args, **kwargs):
            nonlocal turns
            turns += 1
            if turns == 1:
                yield {"type": "error", "text": "busy"}
                yield {"type": "done", "ok": True, "text": "A succeeded"}
            elif turns == 2:
                yield {"type": "done", "ok": False, "text": "busy"}
            else:
                yield {"type": "error", "text": "busy"}
                await asyncio.Event().wait()

        monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", events)
        await agent._run_agent_turn("proj", "proj", "A")
        assert "proj" not in agent._emitted_errors
        seen_error.clear()
        await agent._run_agent_turn("proj", "proj", "B")
        assert [_update_text(item) for item in sent].count("[agent error] busy") == 2
        assert "proj" not in agent._emitted_errors
        seen_error.clear()
        cancelled = asyncio.create_task(agent._run_agent_turn("proj", "proj", "C"))
        await asyncio.wait_for(seen_error.wait(), 2)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        assert "proj" not in agent._emitted_errors

    async def test_successful_turn_without_a_goal_report_authorizes_next_attempt(
        self, wired, tmp_path, monkeypatch
    ):
        from agent_comms.goal_attempts import GoalAttemptStore

        agent = self._agent_with_stub(tmp_path, wired)

        class FakeClient:
            async def session_update(self, **kwargs):
                pass

        async def events(*args, **kwargs):
            yield {"type": "settled"}
            yield {"type": "done", "ok": True, "text": "did the work"}

        monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", events)
        agent._client = FakeClient()
        await agent.new_session(cwd=str(tmp_path / "proj"), mcp_servers=[])
        agent._agent_bin = "pi"
        monkeypatch.setattr(agent, "_schedule_goal", lambda _session: None)
        goal = await agent.set_goal("proj", "Ship the release")
        await agent._run_agent_turn("proj", "proj", "work")

        current = wired.registry.require("proj").goal
        assert current is not None and current.id == goal.id
        assert current.status == "active"
        assert current.progress == ""
        generation = GoalAttemptStore(wired.root / "goal-private").snapshot(goal.id)
        assert generation is not None and generation.state == "ready"
        assert generation.number == 2

    @pytest.mark.parametrize("empty_kind", ["none", "whitespace", "thinking", "unfinished_tool"])
    async def test_empty_successful_continuation_blocks_instead_of_false_no_progress_pause(
        self, wired, tmp_path, monkeypatch, empty_kind
    ):
        agent = self._agent_with_stub(tmp_path, wired)

        class FakeClient:
            async def session_update(self, **kwargs):
                pass

        async def events(*args, **kwargs):
            # Real RPC may accept the user prompt, emit agent_settled and
            # stats, then exit 0 without any assistant/provider work.
            if empty_kind == "whitespace":
                yield {"type": "chunk", "text": " \n\t"}
            elif empty_kind == "thinking":
                yield {"type": "thinking", "text": "Consider the task"}
            elif empty_kind == "unfinished_tool":
                yield {"type": "tool_start", "id": "unfinished", "name": "read"}
            yield {"type": "settled"}
            yield {"type": "agent_info", "context_used": None, "context_size": 1000}
            yield {"type": "done", "ok": True, "text": ""}

        monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", events)
        agent._client = FakeClient()
        await agent.new_session(cwd=str(tmp_path / "proj"), mcp_servers=[])
        original = wired.update_goal("proj", "set", text="Ship the release")
        self._authorize_test_goal(agent, wired, original)
        await agent._run_agent_turn("proj", "proj", "Continue working toward the active goal.")

        goal = wired.registry.require("proj").goal
        assert goal is not None and goal.id == original.id
        assert goal.status == "blocked"
        assert "without assistant output or tool activity" in goal.progress
        assert "paused to avoid a continuation loop" not in goal.progress
        agent._schedule_goal("proj")
        assert not agent._pending_turns.get("proj")

    @pytest.mark.parametrize("outcome", ["failed", "missing_done"])
    async def test_goal_auto_transition_cannot_overwrite_concurrent_progress(
        self, wired, tmp_path, monkeypatch, outcome
    ):
        agent = self._agent_with_stub(tmp_path, wired)

        class FakeClient:
            async def session_update(self, **kwargs):
                pass

        agent._client = FakeClient()
        await agent.new_session(cwd=str(tmp_path / "proj"), mcp_servers=[])
        initial = wired.update_goal("proj", "set", text="Ship the release")
        self._authorize_test_goal(agent, wired, initial)

        async def events(*args, **kwargs):
            yield {"type": "settled"}
            if outcome != "missing_done":
                yield {"type": "done", "ok": outcome == "success", "text": "work"}

        monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", events)
        original_update = wired.update_goal
        original_block = wired.block_goal_after_failed_turn
        raced = False

        def independently_advance(name):
            nonlocal raced
            raced = True
            # A separate tool process advances progress after ACP's precheck,
            # before the automated transition acquires the shared wire lock.
            wire(wired.root).update_goal(
                name,
                "active",
                goal_id=initial.id,
                expected_status="active",
                progress="independently verified newer progress",
            )

        def interpose_pause(name, action, **kwargs):
            if action == "paused" and not raced:
                independently_advance(name)
            return original_update(name, action, **kwargs)

        def interpose_block(name, **kwargs):
            if not raced:
                independently_advance(name)
            return original_block(name, **kwargs)

        monkeypatch.setattr(wired, "update_goal", interpose_pause)
        monkeypatch.setattr(wired, "block_goal_after_failed_turn", interpose_block)
        await agent._run_agent_turn("proj", "proj", "work")
        goal = wired.registry.require("proj").goal
        assert raced
        assert goal is not None and goal.id == initial.id
        if outcome == "success":
            assert goal.status == "blocked"
            assert "independently verified newer progress" in goal.progress
        else:
            assert goal.status == "blocked"
            assert goal.progress.startswith("independently verified newer progress\n\n")
            agent._schedule_goal("proj")
            assert not agent._pending_turns.get("proj")

    @pytest.mark.parametrize("transition", ["paused", "completed", "set"])
    async def test_failed_goal_turn_does_not_revive_superseded_goal(
        self, wired, tmp_path, monkeypatch, transition
    ):
        agent = self._agent_with_stub(tmp_path, wired)

        class FakeClient:
            async def session_update(self, **kwargs):
                pass

        agent._client = FakeClient()
        await agent.new_session(cwd=str(tmp_path / "proj"), mcp_servers=[])
        original = wired.update_goal("proj", "set", text="Ship the release")
        self._authorize_test_goal(agent, wired, original)
        assert original is not None

        async def events(*args, **kwargs):
            yield {"type": "settled"}
            yield {"type": "done", "ok": False, "text": "backend unavailable"}

        monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", events)
        original_block = wired.block_goal_after_failed_turn
        superseding = None

        def interpose(name, **kwargs):
            nonlocal superseding
            if superseding is None:
                superseding = wire(wired.root).update_goal(
                    name,
                    transition,
                    **(
                        {"text": "New objective"}
                        if transition == "set"
                        else {"goal_id": original.id, "progress": "explicit decision"}
                    ),
                )
            return original_block(name, **kwargs)

        monkeypatch.setattr(wired, "block_goal_after_failed_turn", interpose)
        await agent._run_agent_turn("proj", "proj", "work")
        current = wired.registry.require("proj").goal
        assert superseding is not None and current is not None
        if transition == "set":
            assert current == superseding and current.status == "active"
            assert "Backend turn failed" not in current.progress
        else:
            assert current.id == superseding.id and current.status == "blocked"
            assert current.toggle_action == "retry"
            assert current.progress.startswith("explicit decision\n\n")
            from agent_comms.goal_attempts import GoalAttemptStore

            assert (
                GoalAttemptStore(wired.root / "goal-private").snapshot(original.id).state
                == "blocked"
            )

    @pytest.mark.parametrize("outcome", ["failed", "missing_done"])
    async def test_failed_goal_turn_retains_verified_progress_without_auto_retry(
        self, wired, tmp_path, monkeypatch, outcome
    ):
        agent = self._agent_with_stub(tmp_path, wired)

        class FakeClient:
            async def session_update(self, **kwargs):
                pass

        agent._client = FakeClient()
        await agent.new_session(cwd=str(tmp_path / "proj"), mcp_servers=[])
        initial = wired.update_goal("proj", "set", text="Ship the release")
        self._authorize_test_goal(agent, wired, initial)

        async def events(*args, **kwargs):
            wire(wired.root).update_goal(
                "proj",
                "active",
                goal_id=initial.id,
                expected_status="active",
                progress="independently verified newer progress",
            )
            yield {"type": "settled"}
            if outcome == "failed":
                yield {"type": "done", "ok": False, "text": "backend unavailable"}

        monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", events)
        await agent._run_agent_turn("proj", "proj", "work")
        goal = wired.registry.require("proj").goal
        assert goal is not None and goal.id == initial.id
        assert goal.status == "blocked"
        assert goal.progress.startswith("independently verified newer progress\n\n")
        assert "inspect local diagnostics" in goal.progress
        agent._schedule_goal("proj")
        assert not agent._pending_turns.get("proj")

    async def test_successful_goal_update_during_turn_is_not_auto_paused(
        self, wired, tmp_path, monkeypatch
    ):
        agent = self._agent_with_stub(tmp_path, wired)

        class FakeClient:
            async def session_update(self, **kwargs):
                pass

        agent._client = FakeClient()
        await agent.new_session(cwd=str(tmp_path / "proj"), mcp_servers=[])
        initial = wired.update_goal("proj", "set", text="Ship the release")
        self._authorize_test_goal(agent, wired, initial)

        async def events(*args, **kwargs):
            yield {
                "type": "tool_start",
                "id": "goal-progress",
                "name": "comms_goal",
                "title": "Report progress",
            }
            wired.update_goal(
                "proj",
                "active",
                goal_id=initial.id,
                expected_status="active",
                progress="Completed a verified step",
                model_report=True,
            )
            yield {"type": "tool_end", "id": "goal-progress", "name": "comms_goal", "ok": True}
            yield {"type": "settled"}
            yield {"type": "done", "ok": True, "text": "reported"}

        monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", events)
        await agent._run_agent_turn("proj", "proj", "Work toward the active goal")
        goal = wired.registry.require("proj").goal
        assert goal is not None and goal.id == initial.id
        assert goal.status == "active" and goal.progress == "Completed a verified step"

    async def test_goal_completion_and_provider_usage_settle_one_attempt(
        self, wired, tmp_path, monkeypatch
    ):
        from agent_comms.goal_attempts import GoalAttemptStore

        agent = CommsAgent(wired, agent_bin="pi", runtime_enabled=True)
        monkeypatch.setattr(agent, "_ensure_live_drain", lambda _session: None)

        class FakeClient:
            async def session_update(self, **kwargs):
                pass

        agent._client = FakeClient()
        await agent.new_session(cwd=str(tmp_path / "proj"), mcp_servers=[])
        goal = wired.update_goal("proj", "set", text="Ship the release")
        private = wired.root / "goal-private"
        private.mkdir(mode=0o700)
        store = GoalAttemptStore.initialize(private)
        store.create_goal(goal.id)
        agent._goal_store = store

        async def events(*args, **kwargs):
            wired.update_goal(
                "proj", "completed", goal_id=goal.id, progress="Verified done", model_report=True
            )
            yield {"type": "tool_end", "id": "goal", "name": "comms_goal", "ok": True}
            yield {
                "type": "provider_usage",
                "response_id": "1",
                "usage": {"input": 5, "output": 2, "totalTokens": 7, "cost": {"total": 0.01}},
            }
            yield {"type": "settled"}
            yield {"type": "done", "ok": True, "text": "done"}

        monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", events)
        agent._schedule_goal("proj")
        await asyncio.wait_for(agent._wake_tasks["proj"], timeout=2)

        assert wired.registry.require("proj").goal.status == "completed"
        assert GoalAttemptStore(private).snapshot(goal.id).state == "completed"
        assert GoalAttemptStore(private).provider_usage_total(goal.id).responses == 1
        await agent.shutdown()

    @pytest.mark.parametrize("owner_paused", [False, True])
    async def test_goal_tool_hands_private_ready_grant_to_owner_after_final_stop(
        self, wired, tmp_path, monkeypatch, owner_paused
    ):
        from agent_comms.goal_attempts import GoalAttemptStore

        agent = CommsAgent(wired, agent_bin="pi")
        monkeypatch.setattr(agent, "_ensure_live_drain", lambda _session: None)
        updates = []

        class FakeClient:
            async def session_update(self, **kwargs):
                updates.append(kwargs["update"])

        agent._client = FakeClient()
        await agent.new_session(cwd=str(tmp_path / "proj"), mcp_servers=[])

        async def events(*args, **kwargs):
            yield {
                "type": "provider_usage",
                "response_id": "1",
                "usage": {"input": 3, "output": 1, "totalTokens": 4, "cost": {"total": 0.01}},
            }
            wired.update_goal("proj", "set", text="Finish the release")
            yield {"type": "tool_end", "id": "set-goal", "name": "comms_set_goal", "ok": True}
            yield {
                "type": "provider_usage",
                "response_id": "2",
                "usage": {"input": 2, "output": 2, "totalTokens": 4, "cost": {"total": 0.02}},
            }
            if owner_paused:
                wired.update_goal("proj", "paused", owner_action=True)
            yield {"type": "settled"}
            yield {"type": "done", "ok": True, "text": "Goal set"}

        monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", events)
        await agent._run_agent_turn("proj", "proj", "Set a goal")

        goal = wired.registry.require("proj").goal
        if owner_paused:
            assert goal.status == "paused"
            assert wired.goal_pause("proj").source == "owner"
            agent._schedule_goal("proj")
            assert not agent._pending_turns.get("proj")
        store = GoalAttemptStore(wired.root / "goal-private")
        assert store.snapshot(goal.id).state == "ready"
        assert store.snapshot(goal.id).number == 2
        assert store.provider_usage_total(goal.id).responses == 2
        assert str(store.provider_usage_total(goal.id).cost_total) == "0.03"
        grant = agent._goal_store.ready_grant(goal.id, 2)
        assert grant not in repr(updates)
        assert grant not in repr(goal)
        await agent.shutdown()

    async def test_failed_final_after_goal_completion_does_not_leave_false_completed_status(
        self, wired, tmp_path, monkeypatch
    ):
        from agent_comms.goal_attempts import GoalAttemptStore

        agent = CommsAgent(wired, agent_bin="pi", runtime_enabled=True)
        monkeypatch.setattr(agent, "_ensure_live_drain", lambda _session: None)

        class FakeClient:
            async def session_update(self, **kwargs):
                pass

        agent._client = FakeClient()
        await agent.new_session(cwd=str(tmp_path / "proj"), mcp_servers=[])
        goal = wired.update_goal("proj", "set", text="Finish safely")
        private = wired.root / "goal-private"
        private.mkdir(mode=0o700)
        store = GoalAttemptStore.initialize(private)
        store.create_goal(goal.id)
        agent._goal_store = store

        async def events(*args, **kwargs):
            wired.update_goal(
                "proj", "completed", goal_id=goal.id, progress="Premature", model_report=True
            )
            yield {"type": "tool_end", "id": "goal", "name": "comms_goal", "ok": True}
            yield {"type": "settled"}
            yield {"type": "done", "ok": False, "text": "failed"}

        monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", events)
        agent._schedule_goal("proj")
        await asyncio.wait_for(agent._wake_tasks["proj"], timeout=2)

        try:
            assert wired.registry.require("proj").goal.status == "blocked"
            assert GoalAttemptStore(private).snapshot(goal.id).state == "blocked"
        finally:
            await agent.shutdown()

    async def test_failed_goal_origin_never_launches_continuation(
        self, wired, tmp_path, monkeypatch
    ):
        from agent_comms.goal_attempts import GoalAttemptStore, UnresolvedAttempt

        agent = CommsAgent(wired, agent_bin="pi", runtime_enabled=True)
        monkeypatch.setattr(agent, "_ensure_live_drain", lambda _session: None)

        class FakeClient:
            async def session_update(self, **kwargs):
                pass

        agent._client = FakeClient()
        await agent.new_session(cwd=str(tmp_path / "proj"), mcp_servers=[])

        async def events(*args, **kwargs):
            wired.update_goal("proj", "set", text="Do not replay")
            yield {"type": "tool_end", "id": "set-goal", "name": "comms_set_goal", "ok": True}
            yield {"type": "settled"}
            yield {"type": "done", "ok": False, "text": "provider failed"}

        monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", events)
        await agent._run_agent_turn("proj", "proj", "Set a goal")

        goal = wired.registry.require("proj").goal
        store = GoalAttemptStore(wired.root / "goal-private")
        assert goal.status == "blocked"
        assert store.snapshot(goal.id).state == "blocked"
        with pytest.raises(UnresolvedAttempt):
            store.ready_grant(goal.id, 1)
        agent._schedule_goal("proj")
        assert not agent._pending_turns.get("proj")
        await agent.shutdown()

    async def test_blocked_goal_retry_is_explicit_and_restores_owner_grant(
        self, wired, tmp_path, monkeypatch
    ):
        from agent_comms.goal_attempts import GoalAttemptStore

        agent = CommsAgent(wired, agent_bin="pi", runtime_enabled=True)
        monkeypatch.setattr(agent, "_ensure_live_drain", lambda _session: None)
        monkeypatch.setattr(agent, "_schedule_wake", lambda _session: None)

        class FakeClient:
            async def session_update(self, **kwargs):
                pass

        agent.on_connect(FakeClient())
        await agent.new_session(cwd=str(tmp_path / "proj"), mcp_servers=[])
        goal = wired.update_goal("proj", "set", text="Keep working")
        store = agent._open_goal_store()
        store.create_goal(goal.id)
        reservation = store.reserve(goal.id, 1)
        store.claim_launch(reservation)
        store.record_failed(reservation, "The previous turn failed")
        blocked = wired.update_goal("proj", "blocked", goal_id=goal.id)

        try:
            with pytest.raises(ValueError, match="explicit retry"):
                wired.update_goal("proj", "active", goal_id=goal.id)
            proxy = RuntimeProxy(agent, "proj", socket_path(wired.root, os.getpid()))
            result = await proxy.request(
                "retry_goal", goal_id=goal.id, expected_revision=blocked.revision
            )
            resumed = wired.registry.require("proj").goal
            assert result["goal"]["id"] == resumed.id == goal.id
            assert resumed.status == "active"
            generation = GoalAttemptStore(wired.root / "goal-private").snapshot(goal.id)
            assert (generation.number, generation.state) == (2, "ready")
            assert store.ready_grant(goal.id, 2)
            assert agent._pending_turns["proj"][0].goal_id == goal.id
            with pytest.raises(RuntimeError, match="changed"):
                await proxy.request(
                    "retry_goal", goal_id=goal.id, expected_revision=blocked.revision
                )
        finally:
            await agent.shutdown()

    async def test_retry_recovers_ready_ledger_left_by_crash_before_registry_update(
        self, wired, tmp_path, monkeypatch
    ):
        from agent_comms.goal_attempts import GoalAttemptStore

        agent = CommsAgent(wired, agent_bin="pi", runtime_enabled=True)
        monkeypatch.setattr(agent, "_ensure_live_drain", lambda _session: None)
        monkeypatch.setattr(agent, "_schedule_wake", lambda _session: None)
        await agent.new_session(cwd=str(tmp_path / "proj"), mcp_servers=[])
        goal = wired.update_goal("proj", "set", text="Recover a retry")
        store = agent._open_goal_store()
        store.create_goal(goal.id)
        reservation = store.reserve(goal.id, 1)
        store.claim_launch(reservation)
        store.record_failed(reservation, "Previous turn failed")
        blocked = wired.update_goal("proj", "blocked", goal_id=goal.id)
        store.authorize_retry(
            goal.id,
            expected_generation=1,
            attempt_id=reservation.attempt_id,
            user_decision_id="first-ui-retry-before-crash",
        )
        # The owner lost its in-memory grant before making the registry active.
        agent._goal_store = GoalAttemptStore(wired.root / "goal-private")
        proxy = RuntimeProxy(agent, "proj", socket_path(wired.root, os.getpid()))
        try:
            result = await proxy.request(
                "retry_goal", goal_id=goal.id, expected_revision=blocked.revision
            )
            assert result["goal"]["status"] == "active"
            generation = agent._goal_store.snapshot(goal.id)
            assert (generation.number, generation.state) == (3, "ready")
            assert agent._goal_store.ready_grant(goal.id, 3)
        finally:
            await agent.shutdown()

    async def test_reopened_owner_recovers_unused_ready_grant_without_replaying(
        self, wired, tmp_path, monkeypatch
    ):
        from agent_comms.goal_attempts import GoalAttemptStore

        agent = CommsAgent(wired, agent_bin="pi", runtime_enabled=True)
        monkeypatch.setattr(agent, "_ensure_live_drain", lambda _session: None)
        monkeypatch.setattr(agent, "_schedule_wake", lambda _session: None)
        await agent.new_session(cwd=str(tmp_path / "proj"), mcp_servers=[])
        goal = wired.update_goal("proj", "set", text="No silent stalled goal")
        store = agent._open_goal_store()
        store.create_goal(goal.id)
        agent._goal_store = GoalAttemptStore(wired.root / "goal-private")
        try:
            agent._schedule_goal("proj")
            current = wired.registry.require("proj").goal
            assert current is not None and current.status == "active"
            assert len(agent._pending_turns["proj"]) == 1
            assert store.snapshot(goal.id).state == "ready"
            assert store.snapshot(goal.id).number == 1
            assert agent._goal_store.ready_grant(goal.id, 1)
        finally:
            await agent.shutdown()

    async def test_reopened_claimed_goal_attempt_has_no_wake_or_replay(self, monkeypatch):
        from agent_comms.goal_attempts import GoalAttemptStore

        with tempfile.TemporaryDirectory(prefix="ac-goal-reopen-", dir="/var/tmp") as base:
            root = Path(base) / "wire"
            wired = wire(root)
            agent = CommsAgent(wired, agent_bin="pi", runtime_enabled=True)
            monkeypatch.setattr(agent, "_ensure_live_drain", lambda _session: None)

            class FakeClient:
                async def session_update(self, **kwargs):
                    pass

            agent._client = FakeClient()
            await agent.new_session(cwd=base, mcp_servers=[])
            name = next(iter(agent._sessions))
            goal = wired.update_goal(name, "set", text="No replay after crash")
            private = root / "goal-private"
            private.mkdir(mode=0o700)
            store = GoalAttemptStore.initialize(private)
            store.create_goal(goal.id)
            reservation = store.reserve(goal.id, 1)
            store.claim_launch(reservation)
            agent._goal_store = GoalAttemptStore(private)

            async def forbidden_backend(*args, **kwargs):
                raise AssertionError("A claimed attempt must never launch again")
                yield  # pragma: no cover

            monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", forbidden_backend)
            agent._schedule_goal(name)
            assert not agent._pending_turns.get(name)
            await agent._run_agent_turn(name, name, "continue", autonomous_goal=True)
            assert GoalAttemptStore(private).snapshot(goal.id).attempt_id == reservation.attempt_id
            await agent.shutdown()

    async def test_active_goal_without_private_grant_refuses_direct_prompt(
        self, wired, tmp_path, monkeypatch
    ):
        from acp import RequestError

        agent = CommsAgent(wired, agent_bin="pi")
        monkeypatch.setattr(agent, "_ensure_live_drain", lambda _session: None)
        await agent.new_session(cwd=str(tmp_path / "proj"), mcp_servers=[])
        wired.update_goal("proj", "set", text="No orphan spend")

        async def forbidden_backend(*args, **kwargs):
            raise AssertionError("An ungranted goal must not start Pi")
            yield  # pragma: no cover

        monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", forbidden_backend)
        with pytest.raises(RequestError):
            await agent._run_agent_turn("proj", "proj", "continue")
        await agent.shutdown()

    async def test_goal_has_only_one_in_flight_attempt(self, wired, tmp_path, monkeypatch):
        from acp import RequestError

        agent = CommsAgent(wired, agent_bin="pi")
        monkeypatch.setattr(agent, "_ensure_live_drain", lambda _session: None)
        await agent.new_session(cwd=str(tmp_path / "proj"), mcp_servers=[])
        goal = wired.update_goal("proj", "set", text="One attempt")
        self._authorize_test_goal(agent, wired, goal)
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def events(*args, **kwargs):
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            yield {"type": "settled"}
            yield {"type": "done", "ok": False, "text": "failed"}

        monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", events)
        first = asyncio.create_task(agent._run_agent_turn("proj", "proj", "first"))
        await asyncio.wait_for(entered.wait(), timeout=2)
        with pytest.raises(RequestError):
            await agent._run_agent_turn("proj", "proj", "second")
        assert calls == 1
        release.set()
        await asyncio.wait_for(first, timeout=2)
        await agent.shutdown()

    async def test_uncertain_usage_write_terminates_goal_child_without_replay(
        self, wired, tmp_path, monkeypatch
    ):
        from agent_comms.goal_attempts import GoalAttemptStore, StorageUncertain

        agent = CommsAgent(wired, agent_bin="pi")
        monkeypatch.setattr(agent, "_ensure_live_drain", lambda _session: None)
        await agent.new_session(cwd=str(tmp_path / "proj"), mcp_servers=[])
        goal = wired.update_goal("proj", "set", text="Count responses")
        self._authorize_test_goal(agent, wired, goal)
        terminated = []

        async def events(*args, **kwargs):
            yield {"type": "provider_usage", "response_id": "1", "usage": {"totalTokens": 5}}
            await asyncio.Event().wait()

        async def terminate(task):
            terminated.append(task)

        def fail_usage(*args, **kwargs):
            raise StorageUncertain("fsync failed")

        monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", events)
        monkeypatch.setattr("agent_comms.acp.backend.terminate_task_process", terminate)
        monkeypatch.setattr(agent._goal_store, "record_provider_usage", fail_usage)
        with pytest.raises(StorageUncertain):
            await agent._run_agent_turn("proj", "proj", "continue")
        assert terminated
        assert GoalAttemptStore(wired.root / "goal-private").snapshot(goal.id).state == "blocked"
        await agent.shutdown()


class TestWireProtocol:
    @pytest.mark.skipif(
        sys.platform == "win32", reason="selectors cannot poll Windows pipe handles"
    )
    def test_real_stdio_roundtrip(self, tmp_path):
        """initialize -> session/new -> prompt over real stdio pipes."""
        import selectors

        (tmp_path / "proj").mkdir()
        root = tmp_path / "wire"
        env = dict(
            __import__("os").environ,
            AGENT_COMMS_ROOT=str(root),
            PYTHONPATH=str(Path(__file__).parents[1] / "src"),
            AGENT_COMMS_AGENT_BIN="/bin/echo",
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

            # Wait for each response before using its session, as an ACP client
            # does. Concurrent session/new + prompt relied on inline ownership.
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
                owner = comms.registry.require("proj").pid
                assert owner != proc.pid
                assert comms._process_alive(owner)
                comms.stop("proj")
        responses = {
            m.get("id"): m for m in (json.loads(line) for line in out_lines if line.strip())
        }
        assert responses[1]["result"]["protocolVersion"] == 1
        assert responses[2]["result"]["sessionId"] == "proj"
        assert responses[3]["result"]["stopReason"] == "end_turn"
        # The wire got the thread and the prompt.
        assert "proj" in comms.registry
        assert [m.body for m in comms.channel_history("#all")] == ["anyone alive?"]


class TestCrossClient:
    def test_acp_and_cli_share_one_wire(self, tmp_path):
        """CLI agents and ACP sessions see the same threads and messages."""
        from agent_comms.operations import wire as wire_fn

        root = tmp_path / "wire"
        comms = wire_fn(root)
        agent = CommsAgent(comms)

        import asyncio

        sent: list = []

        async def flow() -> None:
            await agent.new_session(cwd=str(tmp_path / "proj"), mcp_servers=[])
            comms.register(Thread(name="cli-agent", tags=frozenset(), worktree=str(tmp_path)))
            comms.send("cli-agent", "proj", "from the cli side")

            class FakeClient:
                async def session_update(self, session_id=None, update=None, **kw):
                    sent.append(update)

            agent._client = FakeClient()
            await agent.prompt(
                session_id="proj", prompt=[{"type": "text", "text": "!relay checking inbox"}]
            )

        asyncio.run(flow())
        incoming = [
            update
            for update in sent
            if "incoming" in (update.field_meta or {}).get("agentComms", {})
        ]
        assert len(incoming) == 1
        assert "from the cli side" in incoming[0].content.text
        # The ACP prompt itself is visible on the CLI side.
        assert [m.body for m in comms.inbox("cli-agent")] == ["checking inbox"]


class TestLiveDrain:
    async def test_messages_arrive_after_prompt_without_new_prompt(self, tmp_path):
        """The background drain pushes inbox messages live between prompts."""
        import asyncio

        agent = CommsAgent(
            wire(tmp_path / "wire"), reply_window=0.2, no_reply_window=0.1, reply_quiet=0.05
        )
        sent: list = []

        class FakeClient:
            async def session_update(self, session_id=None, update=None, **kw):
                sent.append(update)

        agent._client = FakeClient()
        await agent.new_session(cwd=str(tmp_path / "proj"), mcp_servers=[])
        await agent.prompt(session_id="proj", prompt=[{"type": "text", "text": "!relay hi room"}])
        assert not any(
            (
                "cli" in (getattr(u, "content", None).text or "")
                if getattr(u, "content", None)
                else False
            )
            for u in sent
        )

        # A peer DMs the session thread AFTER the prompt finished.
        agent._comms.register(Thread(name="cli", tags=frozenset(), worktree="/wt"))
        agent._comms.send("cli", "proj", "live push")

        # Wait for the background drain loop to fire.
        for _ in range(30):
            await asyncio.sleep(0.1)
            if any(
                "live push" in (getattr(u, "content", None).text or "")
                for u in sent
                if getattr(u, "content", None)
            ):
                break
        assert any(
            "live push" in (getattr(u, "content", None).text or "")
            for u in sent
            if getattr(u, "content", None)
        )

        # The drain acknowledged it; it won't be delivered twice.
        await asyncio.sleep(1.2)
        live_pushes = [u for u in sent if "live push" in _update_text(u)]
        assert len(live_pushes) == 1

    async def test_cancel_keeps_background_drain_live(self, tmp_path):
        agent = CommsAgent(
            wire(tmp_path / "wire"), reply_window=0.2, no_reply_window=0.1, reply_quiet=0.05
        )
        await agent.new_session(cwd=str(tmp_path / "proj"), mcp_servers=[])
        await agent.prompt(session_id="proj", prompt=[{"type": "text", "text": "!relay hi"}])
        assert "proj" in agent._drain_tasks
        await agent.cancel(session_id="proj")
        await asyncio.sleep(0.05)
        assert "proj" in agent._drain_tasks
        assert not agent._drain_tasks["proj"].done()
        await agent.shutdown()


class TestFullHistory:
    def test_full_history_is_everything_in_order(self, tmp_path):
        from agent_comms.operations import wire

        comms = wire(tmp_path / "wire")
        comms.register(Thread(name="a", tags=frozenset({"x"}), worktree="/wt"))
        comms.register(Thread(name="b", tags=frozenset(), worktree="/wt"))
        comms.send("a", "#all", "one")
        comms.send("a", "b", "dm")
        comms.send("b", "#x", "tagged")
        bodies = [(m.sender, m.target, m.body) for m in comms.full_history()]
        assert bodies == [
            ("a", "#all", "one"),
            ("a", "b", "dm"),
            ("b", "#x", "tagged"),
        ]


class TestAgentTurnForwarding:
    """!agent turns stream rpc events into ACP updates + wire activity."""

    def _rpc_stub(self, tmp_path: Path) -> str:
        rpc_lines = "\n".join(
            [
                '{"type":"message_update","assistantMessageEvent":'
                '{"type":"thinking_delta","delta":"Inspecting files"}}',
                '{"type":"message_update","assistantMessageEvent":{"type":"text_delta","delta":"running"}}',
                '{"type":"tool_execution_start","toolCallId":"t1","toolName":"bash","args":{"command":"pwd"}}',
                '{"type":"tool_execution_update","toolCallId":"t1","toolName":"bash",'
                '"partialResult":{"content":[{"type":"text","text":"working"}]}}',
                '{"type":"tool_execution_end","toolCallId":"t1","toolName":"bash",'
                '"result":{"content":[{"type":"text","text":"/wt"}]},"isError":false}',
                '{"type":"message_update",'
                '"assistantMessageEvent":{"type":"text_delta","delta":" finished"}}',
                '{"type":"message_end","message":{"role":"assistant","stopReason":"stop"}}',
                '{"type":"agent_settled"}',
            ]
        )
        stub = tmp_path / "pi-stub"
        stub.write_text(
            f"#!{sys.executable}\n"
            "import json, sys\n"
            + f"capability = {NATIVE_INPUT_CAPABILITY!r}\n"
            + "state = json.loads(sys.stdin.readline())  # get_state\n"
            + "print(json.dumps({'type':'response','command':'get_state','id':state['id'],\n"
            + "      'success':True,\n"
            + "      'data':{'nativeInputProofCapability':capability}}), flush=True)\n"
            + "prompt = json.loads(sys.stdin.readline())\n"
            + 'print(json.dumps({"type": "response", "command": "prompt", '
            '"id": prompt["id"], "success": True}), flush=True)\n'
            + 'print(json.dumps({"type": "message_start", "message": '
            '{"role": "user", "content": prompt["message"], '
            '"inputId": prompt["inputId"]}}), flush=True)\n'
            + f"for event in {rpc_lines.splitlines()!r}: print(event, flush=True)\n"
            + "sys.stdin.readline()  # postturn get_state\n"
            + "sys.stdin.readline()  # get_session_stats\n"
            + "print(json.dumps({'type': 'response', 'command': 'get_session_stats', "
            "'success': True, 'data': {'contextUsage': {}}}), flush=True)\n"
        )
        stub.chmod(0o755)
        return str(stub)

    async def test_turn_forwards_tool_calls_and_thinking(self, wired, tmp_path):
        import sys as _sys

        if _sys.platform == "win32":
            pytest.skip("shell-script stub; POSIX only")
        agent = CommsAgent(
            wired,
            agent_bin=self._rpc_stub(tmp_path),
            agent_args=[],
            reply_window=0.2,
            no_reply_window=0.1,
            reply_quiet=0.05,
        )
        sent: list = []

        class FakeClient:
            async def session_update(self, session_id=None, update=None, **kw):
                sent.append(update)

        agent._client = FakeClient()
        await agent.new_session(cwd=str(tmp_path / "proj"), mcp_servers=[])
        await agent.prompt(
            session_id="proj", prompt=[{"type": "text", "text": "!agent do a thing"}]
        )
        assert any(
            (getattr(update, "field_meta", None) or {})
            .get("agentComms", {})
            .get("inputDisposition", {})
            .get("status")
            == "unknown"
            for update in sent
        )
        assert any(
            "inputStarted" in (getattr(update, "field_meta", None) or {}).get("agentComms", {})
            for update in sent
        )
        sent = [
            update
            for update in sent
            if not any(
                key in (getattr(update, "field_meta", None) or {}).get("agentComms", {})
                for key in ("inputDisposition", "inputStarted", "queue")
            )
        ]
        goal_updates = [update for update in sent if isinstance(update, SessionInfoUpdate)]
        assert len(goal_updates) == 1
        assert goal_updates[0].field_meta == {"agentComms": {"goal": None, "goalExecution": None}}
        assert "title" not in goal_updates[0].model_fields_set
        sent = [update for update in sent if not isinstance(update, SessionInfoUpdate)]
        kinds = [type(u).__name__ for u in sent]
        assert kinds == [
            "AgentMessageChunk",  # turn-started metadata before any model output
            "AgentThoughtChunk",  # actual backend thinking
            "AgentMessageChunk",  # "running"
            "ToolCallStart",  # Run pwd
            "ToolCallProgress",  # live output
            "ToolCallProgress",  # completed
            "AgentMessageChunk",  # " finished"
            "AgentMessageChunk",  # turn-settled metadata
            "AgentMessageChunk",  # committed transcript invalidation
        ]
        turn_id = sent[0].field_meta["agentComms"]["turnId"]
        assert turn_id and sent[0].field_meta["agentComms"]["turnStarted"]
        assert sent[1].content.text == "Inspecting files"
        tool_call = sent[3]
        assert tool_call.tool_call_id == "t1"
        assert tool_call.title == "Run pwd"
        assert tool_call.kind == "execute"
        assert tool_call.raw_input == {"command": "pwd"}
        assert sent[4].status == "in_progress"
        assert sent[4].content[0].content.text == "working"
        progress = sent[5]
        assert progress.status == "completed"
        assert progress.content[0].content.text == "/wt"
        assert sent[-2].field_meta == {"agentComms": {"turnSettled": True, "turnId": turn_id}}
        assert sent[-1].field_meta == {
            "agentComms": {
                "transcriptChanged": True,
                "transcriptCursor": {"session_file": "", "offset": 0},
            }
        }
        assert not agent._active_turns

    async def test_turn_sets_wire_activity(self, wired, tmp_path):
        import sys as _sys

        if _sys.platform == "win32":
            pytest.skip("shell-script stub; POSIX only")
        agent = CommsAgent(
            wired,
            agent_bin=self._rpc_stub(tmp_path),
            agent_args=[],
            reply_window=0.2,
            no_reply_window=0.1,
            reply_quiet=0.05,
        )
        agent._client = None  # no client: activity still recorded
        await agent.new_session(cwd=str(tmp_path / "proj"), mcp_servers=[])
        await agent.prompt(
            session_id="proj", prompt=[{"type": "text", "text": "!agent do a thing"}]
        )
        # Turn finished -> idle again.
        assert wired.activity_of("proj").state.value == "idle"
        # The full trail was recorded: thinking -> working -> thinking -> idle.
        states = [e.state.value for e in wired.activity._load() if e.thread == "proj"]
        assert states == ["thinking", "working", "thinking", "idle"]


class TestActivityLayer:
    def test_emit_and_read_current(self, wired):
        from agent_comms import ActivityState

        wired.registry.require("PR111")
        wired.set_activity("PR111", ActivityState.WORKING, "bash: echo hi")
        activity = wired.activity_of("PR111")
        assert activity.state is ActivityState.WORKING
        assert activity.detail == "bash: echo hi"

    def test_idle_cannot_carry_detail(self, wired):
        from agent_comms import ActivityState

        with pytest.raises(Exception, match="Idle"):
            wired.set_activity("PR111", ActivityState.IDLE, "junk")

    def test_unknown_thread_fail_closed(self, wired):
        from agent_comms import ActivityState

        with pytest.raises(UnregisteredThreadError):
            wired.set_activity("ghost", ActivityState.THINKING)

    def test_stale_activity_reads_idle(self, wired):
        import time as _time

        from agent_comms.declarations import Activity, ActivityState

        wired.activity.emit(Activity(thread="PR111", state=ActivityState.WORKING, detail="old"))
        # Tamper the timestamp to be stale.
        import json as _json

        log = wired.activity._path
        lines = [_json.loads(line) for line in log.read_text().splitlines()]
        lines[-1]["ts"] = _time.time() - 1000
        log.write_text("\n".join(_json.dumps(line) for line in lines))
        assert wired.activity_of("PR111").state is ActivityState.IDLE

    def test_activity_persistence(self, tmp_path):
        from agent_comms.declarations import Activity, ActivityLog, ActivityState

        path = tmp_path / "activity.jsonl"
        log = ActivityLog(path)
        log.emit(Activity(thread="a", state=ActivityState.WORKING, detail="bash"))
        assert ActivityLog(path).current("a").detail == "bash"

    def test_all_current_latest_per_thread(self, tmp_path):
        from agent_comms.declarations import Activity, ActivityLog, ActivityState

        log = ActivityLog(tmp_path / "activity.jsonl")
        log.emit(Activity(thread="a", state=ActivityState.THINKING))
        log.emit(Activity(thread="a", state=ActivityState.WORKING, detail="bash"))
        log.emit(Activity(thread="b", state=ActivityState.THINKING))
        current = log.all_current()
        assert current["a"].state is ActivityState.WORKING
        assert current["b"].state is ActivityState.THINKING


class TestTargetPrefix:
    def test_dm_prefix(self):
        from agent_comms.acp import parse_target

        assert parse_target("@fixer hello there") == ("fixer", "hello there")

    def test_channel_prefix(self):
        from agent_comms.acp import parse_target

        assert parse_target("#ci flake again") == ("#ci", "flake again")

    def test_plain_goes_global(self):
        from agent_comms.acp import parse_target

        assert parse_target("plain text") == ("#all", "plain text")

    def test_bare_target_not_sent(self):
        from agent_comms.acp import parse_target

        # A bare "@name" with no body composes nothing — sent to global
        # verbatim so the room sees an incomplete line rather than dropping it.
        assert parse_target("@fixer")[0] == "#all"

    async def test_dm_prompt_reaches_only_target(self, wired):
        agent = CommsAgent(wired, reply_window=0.1, no_reply_window=0.05, reply_quiet=0.02)
        wired.register(Thread(name="peer", tags=frozenset(), worktree="/wt"))
        await agent.new_session(cwd="/wt/proj", mcp_servers=[])
        await agent.prompt(session_id="proj", prompt=[{"type": "text", "text": "@peer hi peer"}])
        # DM delivered, not broadcast.
        assert wired.pending_count("peer") == 1
        assert wired.pending_count("PR111") == 0
        assert [m.body for m in wired.dm_history("proj", "peer")] == ["hi peer"]


class TestFailureFeedback:
    async def test_agent_origin_failure_notifies_origin_without_waking(
        self, wired, tmp_path, monkeypatch
    ):
        from agent_comms.declarations import Message, MessageType, Thread

        agent = TestAgentTurn()._agent_with_stub(tmp_path, wired)
        message = "Codex error: The usage limit has been reached"

        async def events(*args, **kwargs):
            yield {"type": "error", "text": message}
            yield {"type": "settled"}
            yield {"type": "done", "ok": False, "text": message}

        monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", events)
        await agent.new_session(cwd=str(tmp_path / "proj"), mcp_servers=[])
        wired.register(
            Thread(name="peer", tags=frozenset({"team"}), worktree=str(tmp_path / "proj"))
        )
        origin = Message(sender="peer", target="#team", body="do the work", type=MessageType.INFO)
        await agent._run_agent_turn("proj", "proj", "do the work", origins=(origin,))

        history = wired.channel_history("#team")
        assert history, "failed agent delivery must be reported where the request came from"
        notice = history[-1]
        assert notice.notice is True
        assert notice.body == (
            "Delivery failed: backend turn did not complete; inspect local diagnostics."
        )
        assert message not in notice.body
        assert not notice.starts_turn
        assert len(history) == 1

    @pytest.mark.parametrize("channel", [False, True])
    async def test_failed_terminal_discards_partial_reply_and_deduplicates_notice(
        self, wired, tmp_path, monkeypatch, channel
    ):
        from agent_comms.declarations import Message, MessageType, Thread

        agent = TestAgentTurn()._agent_with_stub(tmp_path, wired)
        await agent.new_session(cwd=str(tmp_path / "proj"), mcp_servers=[])
        human = wired.user_identity(str(tmp_path / "proj"))
        if channel:
            wired.register(Thread("member", frozenset({"team"}), str(tmp_path / "proj")))
        origin_target = "#team" if channel else "proj"
        reply_target = "#team" if channel else human.name
        origin = Message(human.name, origin_target, "please help", MessageType.INFO)
        routed: list = []
        monkeypatch.setattr(wired, "record_turn_routing", lambda *args: routed.append(args))

        async def events(*args, **kwargs):
            yield {"type": "chunk", "text": "unfinished secret answer"}
            yield {"type": "settled"}
            yield {"type": "done", "ok": False, "text": "provider failed SECRET_PRIVATE_938"}

        monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", events)
        await agent._run_agent_turn(
            "proj", "proj", "answer", reply_targets=(reply_target,), origins=(origin,)
        )

        history = (
            wired.channel_history(reply_target) if channel else wired.dm_history("proj", human.name)
        )
        assert len(history) == 1
        assert history[0].notice is True
        assert history[0].type is MessageType.ALERT
        assert history[0].body == (
            "Request failed: backend turn did not complete; inspect local diagnostics."
        )
        assert "SECRET_PRIVATE_938" not in history[0].body
        assert not history[0].starts_turn
        assert not routed

    async def test_missing_terminal_discards_partial_reply_and_route(
        self, wired, tmp_path, monkeypatch
    ):
        from agent_comms.declarations import Message, MessageType

        agent = TestAgentTurn()._agent_with_stub(tmp_path, wired)
        await agent.new_session(cwd=str(tmp_path / "proj"), mcp_servers=[])
        human = wired.user_identity(str(tmp_path / "proj"))
        origin = Message(human.name, "proj", "please help", MessageType.INFO)
        routed: list = []
        monkeypatch.setattr(wired, "record_turn_routing", lambda *args: routed.append(args))

        async def events(*args, **kwargs):
            yield {"type": "chunk", "text": "unfinished secret answer"}
            yield {"type": "settled"}

        monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", events)
        await agent._run_agent_turn(
            "proj", "proj", "answer", reply_targets=(human.name,), origins=(origin,)
        )
        history = wired.dm_history("proj", human.name)
        assert len(history) == 1
        assert history[0].notice is True
        assert history[0].body == (
            "Request failed: backend turn did not complete; inspect local diagnostics."
        )
        assert not routed

    async def test_successful_terminal_sends_complete_reply_and_records_route(
        self, wired, tmp_path, monkeypatch
    ):
        from agent_comms.declarations import Message, MessageType

        agent = TestAgentTurn()._agent_with_stub(tmp_path, wired)
        await agent.new_session(cwd=str(tmp_path / "proj"), mcp_servers=[])
        human = wired.user_identity(str(tmp_path / "proj"))
        origin = Message(human.name, "proj", "please help", MessageType.INFO)
        routed: list = []
        monkeypatch.setattr(wired, "record_turn_routing", lambda *args: routed.append(args))

        async def events(*args, **kwargs):
            yield {"type": "chunk", "text": "complete answer"}
            yield {"type": "settled"}
            yield {"type": "done", "ok": True, "text": "complete answer"}

        monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", events)
        await agent._run_agent_turn(
            "proj", "proj", "answer", reply_targets=(human.name,), origins=(origin,)
        )
        history = wired.dm_history("proj", human.name)
        assert len(history) == 1
        assert history[0].notice is False
        assert history[0].body == "complete answer"
        assert len(routed) == 1

    async def test_failed_turn_notices_unique_reply_and_origin_targets(
        self, wired, tmp_path, monkeypatch
    ):
        from agent_comms.declarations import Message, MessageType, Thread

        agent = TestAgentTurn()._agent_with_stub(tmp_path, wired)
        await agent.new_session(cwd=str(tmp_path / "proj"), mcp_servers=[])
        human = wired.user_identity(str(tmp_path / "proj"))
        wired.register(Thread("member", frozenset({"team"}), str(tmp_path / "proj")))
        origin = Message(human.name, "#team", "please help", MessageType.INFO)

        async def events(*args, **kwargs):
            yield {"type": "chunk", "text": "unfinished"}
            yield {"type": "done", "ok": False, "text": "failed"}

        monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", events)
        await agent._run_agent_turn(
            "proj", "proj", "answer", reply_targets=(human.name, "#team"), origins=(origin,)
        )
        history = wired.full_history()
        assert len(history) == 2
        assert {message.target for message in history} == {human.name, "#team"}
        assert all(message.notice and not message.starts_turn for message in history)
        assert all("unfinished" not in message.body for message in history)


class TestLiveConfigSync:
    async def test_external_model_change_is_published_to_connected_clients(
        self, wired, tmp_path, monkeypatch
    ):
        agent = TestAgentTurn()._agent_with_stub(tmp_path, wired)
        sent: list = []

        class FakeClient:
            async def session_update(self, session_id=None, update=None, **kw):
                sent.append(update)

        async def options(_name: str):
            return []

        monkeypatch.setattr(agent, "_config_options", options)
        agent._client = FakeClient()
        await agent.new_session(cwd=str(tmp_path / "proj"), mcp_servers=[])
        # The session response already supplied the options, so the first sync
        # must not republish them.
        await agent._sync_thread_config("proj")
        assert not any(isinstance(update, ConfigOptionUpdate) for update in sent)
        assert len(sent) == 1 and isinstance(sent[0], SessionInfoUpdate)
        assert sent[0].field_meta == {"agentComms": {"goal": None, "goalExecution": None}}
        assert "title" not in sent[0].model_fields_set
        sent.clear()
        # Another thread can change this thread's model; the view must follow.
        wired.set_thread_model("proj", "openrouter/deepseek/deepseek-v4.1-flash")
        await agent._sync_thread_config("proj")
        assert len(sent) == 1
        assert isinstance(sent[0], ConfigOptionUpdate)

    async def test_goal_metadata_updates_once_per_change(self, wired, tmp_path, monkeypatch):
        agent = TestAgentTurn()._agent_with_stub(tmp_path, wired)
        monkeypatch.setattr(agent, "_ensure_live_drain", lambda _: None)
        sent: list = []

        class FakeClient:
            async def session_update(self, session_id=None, update=None, **kw):
                sent.append(update)

        async def assert_snapshot_update():
            goal, execution = wired.goal_snapshot("proj")
            await agent._sync_thread_config("proj")
            assert len(sent) == 1 and isinstance(sent[0], SessionInfoUpdate)
            assert sent[0].field_meta == {
                "agentComms": {
                    "goal": asdict(goal) if goal else None,
                    "goalExecution": asdict(execution) if execution else None,
                }
            }
            assert "title" not in sent[0].model_fields_set
            sent.clear()
            await agent._sync_thread_config("proj")
            assert sent == []

        agent._client = FakeClient()
        try:
            await agent.new_session(cwd=str(tmp_path / "proj"), mcp_servers=[])
            await assert_snapshot_update()
            goal = wired.update_goal("proj", "set", text="Handle assigned work")
            await assert_snapshot_update()

            wired.register(Thread("child", frozenset(), str(tmp_path)))
            wired.update_goal("proj", "standby", goal_id=goal.id, wait_for=["child"])
            await assert_snapshot_update()

            # Renaming a dependency changes only the execution projection.
            before_goal, before_execution = wired.goal_snapshot("proj")
            wired.registry.rename("child", "renamed-child")
            after_goal, after_execution = wired.goal_snapshot("proj")
            assert after_goal == before_goal
            assert after_execution != before_execution
            assert after_execution.wait_for[0].name == "renamed-child"
            await assert_snapshot_update()

            wired.update_goal("proj", "clear", goal_id=goal.id)
            await assert_snapshot_update()
        finally:
            await agent.shutdown()


class TestQueueControl:
    async def test_clear_queue_request_does_not_start_a_turn(self, wired, tmp_path, monkeypatch):
        from agent_comms.declarations import Thread

        agent = TestAgentTurn()._agent_with_stub(tmp_path, wired)
        ran: list[str] = []

        async def events(*args, **kwargs):
            ran.append("turn")
            yield {"type": "settled"}

        monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", events)
        await agent.new_session(cwd=str(tmp_path / "proj"), mcp_servers=[])
        wired.register(Thread(name="peer", tags=frozenset(), worktree=str(tmp_path / "proj")))
        agent._backend_inboxes["proj"] = __import__("asyncio").Queue()
        agent._queued_inputs["proj"] = {}
        response = await agent.prompt(
            session_id="proj",
            prompt=[{"type": "text", "text": " "}],
            _meta={"agentComms": {"clearQueue": True}},
        )
        assert response.stop_reason == "end_turn"
        assert ran == []
        assert "proj" not in agent._queued_inputs
