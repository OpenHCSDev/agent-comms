"""Real selected native tools over an isolated retained source; localhost only."""

import asyncio
import functools
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.bus_publication import stable_thread_lookup
from agent_comms.child_process import AttachedChild
from agent_comms.coordinated_runtime import SelectedExecution
from agent_comms.coordination_errors import CoordinationError
from agent_comms.native_entries import NativeEvidenceRead
from agent_comms.private_path import FileRevision
from agent_comms.tracked_turn import TrackedTurnSession
from compaction_loopback import LoopbackProvider
from test_coordinated_runtime import _root

PACKAGE = os.environ.get("PI_COMPACTION_TEST_PACKAGE")
SOURCE = os.environ.get("NATIVE_TOOL_LATENCY_SOURCE")
pytestmark = pytest.mark.skipif(not PACKAGE or not SOURCE, reason="Pinned native and isolated retained source required")


@pytest.fixture
def tmp_path():
    """Runtime requires /var/tmp; retain original fixture proof under our WT."""
    runtime = Path(tempfile.mkdtemp(prefix="arendt-retained-tool-", dir="/var/tmp"))
    artifacts = Path(os.environ["NATIVE_TOOL_LATENCY_ARTIFACTS"])
    artifacts.mkdir(parents=True, mode=0o700, exist_ok=True)
    try:
        yield runtime
    finally:
        shutil.copytree(runtime, artifacts / runtime.name)
        shutil.rmtree(runtime)


class ReadProvider(LoopbackProvider):
    cancel_final = False

    def response_chunks(self):
        if self.posts == 1:
            if self.cancel_final:
                self.response_gate = asyncio.Event()
            for number in range(3):
                yield {"tool_calls": [{
                    "index": number, "id": f"isolated_read_{number}", "type": "function",
                    "function": {"name": "read", "arguments": json.dumps({"path": f"read-{number}.txt"})},
                }]}, None
            yield {}, "tool_calls"
        else:
            yield from super().response_chunks()


@pytest.mark.parametrize("outcome", ["complete", "cancel"])
async def test_installed_retained_native_read_round_preserves_original_custody(tmp_path, monkeypatch, outcome):
    source = Path(SOURCE).absolute()
    original = FileRevision.from_stat(source.stat())
    original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    assert original.size >= 40 * 1024**2, "This acceptance requires representative retained history"
    root, root_id, comms, initial, _ = _root(tmp_path, direct=True, body="Perform three isolated fixture reads, then answer once.")
    owner = comms.registry.require("beta")
    directory = root / "native-sessions" / stable_thread_lookup(owner.created_at)
    directory.mkdir(parents=True, mode=0o700)
    saved = directory / source.name
    shutil.copyfile(source, saved)
    saved.chmod(0o600)
    with sqlite3.connect(Path(str(source) + ".input-proof").as_uri() + "?mode=ro", uri=True) as original_db:
        with sqlite3.connect(str(saved) + ".input-proof") as target_db:
            original_db.backup(target_db)
    Path(str(saved) + ".input-proof").chmod(0o600)
    comms.registry.register(replace(owner, model="response-local/fixture", thinking_level="off", session_file=str(saved)))
    for number in range(3):
        (tmp_path / f"read-{number}.txt").write_text(f"Isolated native read {number}\n")
    config = tmp_path / "config"
    config.mkdir(mode=0o700)
    provider = ReadProvider(status=200, text="Three isolated reads completed.")
    provider.cancel_final = outcome == "cancel"
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
        "models": [{"id": "fixture", "name": "Local retained reads", "contextWindow": 2000000, "maxTokens": 256}],
    }}}))
    (config / "auth.json").write_text(json.dumps({"response-local": {"type": "api_key", "key": "localhost-only"}}))
    (config / "settings.json").write_text(json.dumps({"compaction": {"enabled": False}, "retry": {"enabled": False}}))
    monkeypatch.setenv("AGENT_COMMS_NATIVE_CONFIG_DIR", str(config))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(config))
    acquisitions, proofs, starts, ends, children, readers = [], [], {}, {}, [], []
    captured_sizes, sizes_after_decode = [], []
    observe = NativeEvidenceRead.observe
    context = TrackedTurnSession.context_proof
    tool_start, tool_end = TrackedTurnSession.tool_started, TrackedTurnSession.tool_finished
    child_start = AttachedChild.start

    @functools.wraps(observe)
    def timed_observe(self):
        if self not in readers:
            readers.append(self)
        started = time.perf_counter()
        result = observe(self)
        acquisitions.append(time.perf_counter() - started)
        captured_sizes.append(self.source.size)
        sizes_after_decode.append(self.source.path.stat().st_size)
        return result

    @functools.wraps(context)
    def timed_context(self):
        started = time.perf_counter()
        result = context(self)
        proofs.append(time.perf_counter() - started)
        return result

    @functools.wraps(tool_start)
    async def timed_start(self, event):
        starts[event.tool_call_id] = time.perf_counter()
        await tool_start(self, event)

    @functools.wraps(tool_end)
    async def timed_end(self, event):
        ends[event.tool_call_id] = time.perf_counter()
        await tool_end(self, event)

    async def observe_child(command, **options):
        child = await child_start(command, **options)
        children.append(child)
        return child

    monkeypatch.setattr(NativeEvidenceRead, "observe", timed_observe)
    monkeypatch.setattr(TrackedTurnSession, "context_proof", timed_context)
    monkeypatch.setattr(TrackedTurnSession, "tool_started", timed_start)
    monkeypatch.setattr(TrackedTurnSession, "tool_finished", timed_end)
    monkeypatch.setattr(AttachedChild, "start", observe_child)
    started = time.perf_counter()
    try:
        execution = SelectedExecution(
            root=root, wire_root_id=root_id, owner_name="beta",
            native_package=Path(PACKAGE).resolve(strict=True), session_file=saved,
        )
        task = asyncio.create_task(execution.run())
        if outcome == "complete":
            result = await asyncio.wait_for(task, 90)
            assert result.response_message_id
        else:
            async with asyncio.timeout(60):
                while len(ends) != 3 or provider.posts != 2:
                    assert not task.done(), task.result() if task.done() else None
                    await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            before = provider.posts
            try:
                replay = await SelectedExecution(
                    root=root, wire_root_id=root_id, owner_name="beta",
                    native_package=Path(PACKAGE).resolve(strict=True), session_file=saved,
                ).run()
            except CoordinationError:
                replay = None
            assert replay is None
            assert provider.posts == before
        assert provider.posts == 2
        assert set(starts) == set(ends) == {f"isolated_read_{number}" for number in range(3)}
        assert len(proofs) == (4 if outcome == "complete" else 3)
        assert max(proofs) < acquisitions[0] / 4
        assert sizes_after_decode[0] > captured_sizes[0], "Fast native append during initial decoding must be exercised"
        assert saved.read_bytes().startswith(source.read_bytes())
        if outcome == "complete":
            assert comms.views.full_history()[-1].body == provider.text
        assert all(not child.alive() for child in children)
        assert all(reader.source.stream.closed and not reader.entries for reader in readers)
        receipt = {
            "outcome": outcome,
            "source_bytes": original.size, "source_sha256": original_hash,
            "elapsed_seconds": time.perf_counter() - started, "provider_posts": provider.posts,
            "initial_decode_seconds": acquisitions[0], "context_check_seconds": proofs,
            "initial_snapshot_bytes": captured_sizes[0], "bytes_after_initial_decode": sizes_after_decode[0],
            "native_appended_during_initial_decode": sizes_after_decode[0] > captured_sizes[0],
            "python_tool_start_to_end_seconds": [ends[key] - starts[key] for key in starts],
            "clock": "Same-process perf_counter; tool boundaries are original event consumption, not native producer timestamps",
            "original_source_unchanged": FileRevision.from_stat(source.stat()) == original,
            "original_sha256_unchanged": hashlib.sha256(source.read_bytes()).hexdigest() == original_hash,
            "native_children_retired": all(not child.alive() for child in children),
            "evidence_readers_closed": all(reader.source.stream.closed for reader in readers),
        }
        assert receipt["original_source_unchanged"] and receipt["original_sha256_unchanged"]
        print("retained_native_tool_latency=" + json.dumps(receipt))
        if os.environ.get("NATIVE_TOOL_LATENCY_RECEIPT"):
            path = Path(os.environ["NATIVE_TOOL_LATENCY_RECEIPT"])
            path.with_name(path.stem + "-" + outcome + path.suffix).write_text(json.dumps(receipt, indent=2) + "\n")
    finally:
        for child in children:
            await child.stop()
        server.close()
        await server.wait_closed()
        for task in tuple(connections):
            task.cancel()
        await asyncio.gather(*connections, return_exceptions=True)
