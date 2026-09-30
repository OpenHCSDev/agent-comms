"""Installed selected coding journey through real CLI/tools; localhost responses only."""

import asyncio
import json
import os
import time
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.child_process import AttachedChild
from agent_comms.compaction_journal import CompactionJournal
from agent_comms.compaction_records import PrivateRawInput
from agent_comms.coordinated_runtime import SelectedExecution
from agent_comms.coordination_errors import CoordinationError
from agent_comms.coordinator import Coordination
from agent_comms.native_pi import NativePiUnavailable
from agent_comms.native_runtime_input import NativeRuntimeInput
from compaction_loopback import LoopbackProvider
from native_proof_cases import read_proof_rows
from test_coordinated_runtime import _root, tmp_path  # noqa: F401

PACKAGE = os.environ.get("PI_COMPACTION_TEST_PACKAGE")
pytestmark = pytest.mark.skipif(not PACKAGE, reason="Reviewed prepared native package required")


class CodingProvider(LoopbackProvider):
    def __init__(self, seconds):
        super().__init__(status=200, text="Completed real coding tool.")
        self.seconds = seconds
        self.round = 0

    def response_chunks(self):
        self.round += 1
        if self.round == 1:
            yield {"tool_calls": [{
                "index": 0, "id": "actual_sleep", "type": "function",
                "function": {"name": "bash", "arguments": json.dumps({
                    "command": (
                        f"printf native-started > native-started; sleep {self.seconds}; "
                        "printf native-tool-finished"
                    ),
                    "timeout": self.seconds + 10,
                })},
            }]}, None
            yield {}, "tool_calls"
        else:
            yield from super().response_chunks()


@pytest.mark.parametrize("outcome", ["complete", "cancel", "exit"])
async def test_installed_selected_long_tool_and_uncertain_cleanup(tmp_path, monkeypatch, outcome):
    package = Path(PACKAGE).resolve(strict=True)
    # The default acceptance crosses the removed whole-turn90 boundary. A shorter
    # environment override is for fixture development, never evidence of that gate.
    seconds = float(os.environ.get("TRACKED_NATIVE_LONG_TOOL_SECONDS", "95"))
    provider = CodingProvider(seconds if outcome == "complete" else 20)
    root, root_id, comms, initial, _ = _root(tmp_path, direct=True, body="Isolated coding fixture")
    owner = comms.registry.require("beta")
    comms.registry.register(replace(owner, model="response-local/fixture", thinking_level="off"))
    config = tmp_path / "config"
    config.mkdir(mode=0o700)
    connections = set()

    async def serve(reader, writer):
        task = asyncio.current_task()
        connections.add(task)
        try:
            await provider.handle(reader, writer)
        finally:
            connections.discard(task)

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    (config / "models.json").write_text(json.dumps({"providers": {"response-local": {
        "baseUrl": f"http://127.0.0.1:{port}/v1", "api": "openai-completions",
        "models": [{"id": "fixture", "name": "Local coding fixture", "contextWindow": 272000,
                    "maxTokens": 128}],
    }}}))
    (config / "auth.json").write_text(json.dumps({
        "response-local": {"type": "api_key", "key": "local-only"},
    }))
    (config / "settings.json").write_text(json.dumps({
        "compaction": {"enabled": False}, "retry": {"enabled": False},
    }))
    monkeypatch.setenv("AGENT_COMMS_NATIVE_CONFIG_DIR", str(config))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(config))
    children = []
    start = AttachedChild.start

    async def observe_child(command, **options):
        child = await start(command, **options)
        children.append(child)
        return child

    monkeypatch.setattr(AttachedChild, "start", observe_child)
    started = time.monotonic()
    task = asyncio.create_task(SelectedExecution(
        root=root, wire_root_id=root_id, owner_name="beta", native_package=package,
    ).run())
    try:
        if outcome == "complete":
            result = await asyncio.wait_for(task, seconds + 40)
            elapsed = time.monotonic() - started
            assert result.response_message_id
            assert elapsed >= seconds
            assert provider.posts == 2
            assert comms.views.full_history()[-1].body == provider.text
        else:
            async with asyncio.timeout(20):
                while not (tmp_path / "native-started").exists():
                    assert not task.done(), task.result() if task.done() else None
                    await asyncio.sleep(0.01)
            if outcome == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                await children[-1].stop()
                with pytest.raises(NativePiUnavailable, match="native exit=.*stderr="):
                    await task
                diagnostics = list((root / "diagnostics").glob("*.json"))
                assert len(diagnostics) == 1
                diagnostic = json.loads(diagnostics[0].read_text())
                assert "NativePiUnavailable" in diagnostic["source_error"]
                assert "native exit=" in diagnostic["source_error"]
            elapsed = time.monotonic() - started
            assert provider.posts == 1
        assert children and all(not child.alive() for child in children)
        sessions = list((root / "native-sessions").rglob("*.jsonl"))
        assert len(sessions) == 1
        entries = [json.loads(line) for line in sessions[0].read_text().splitlines()]
        inputs = [row["message"] for row in entries
                  if row["type"] == "message" and row["message"]["role"] == "user"]
        assert len(inputs) == 1
        proofs = read_proof_rows(sessions[0])
        assert {row["inputId"] for row in proofs} == {inputs[0]["inputId"]}
        with Coordination(root / "coordination.sqlite3") as store, store.session.read():
            rows = NativeRuntimeInput.select(store.session._connection)
            assert len(rows) == 1
            if outcome != "complete":
                assert rows[0].session_id is None
        journal = CompactionJournal(root / "compaction-commits.sqlite3")
        with journal.transaction() as db:
            assert len(PrivateRawInput.select(db)) == 1
        before = provider.posts
        try:
            replay = await SelectedExecution(
                root=root, wire_root_id=root_id, owner_name="beta", native_package=package,
            ).run()
            assert replay is None
        except CoordinationError:
            assert outcome != "complete"
        assert provider.posts == before
        print(json.dumps({"outcome": outcome, "elapsed_seconds": elapsed,
                          "provider_posts": provider.posts, "saved_inputs": len(inputs),
                          "native_children_retired": True, "no_replay": True}), flush=True)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for child in children:
            await child.stop()
        server.close()
        await server.wait_closed()
        for connection in tuple(connections):
            connection.cancel()
        await asyncio.gather(*connections, return_exceptions=True)
