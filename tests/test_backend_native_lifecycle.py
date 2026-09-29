"""Actual pinned CLI through ordinary TurnSession; localhost provider only."""

import asyncio
import json
import os
from pathlib import Path

import pytest

from agent_comms import agent_events as events
from agent_comms import backend
from agent_comms.comms import Comms
from agent_comms.fresh_private_session import create_fresh_private_session
from compaction_loopback import LoopbackProvider


class NativeBackendFixture:
    def __init__(self, root, project, session, provider, config):
        self.root, self.project, self.session, self.provider = root, project, session, provider
        self.config = config
        self.persistent = backend.PersistentPiSession()
        self.starts = []
        self.children = []
        self.observed = []

    def started(self, public_id, native_id, text):
        self.starts.append((public_id, native_id, text))
        child = backend.TurnSession.active[asyncio.current_task()].native.proc
        # Custody is already with the saved-session owner at native input start;
        # there is no later copy from transient TurnSession process fields.
        assert self.persistent.custody.child.proc is child
        assert self.persistent.custody.child.reader is not None
        if child not in self.children:
            self.children.append(child)
        return True

    async def run(self, text, *, followup=None, queue=None, **options):
        queue = queue if queue is not None else asyncio.Queue()
        result = []
        async with asyncio.timeout(25):
            async for event in backend.stream_agent_events(
                "pi",
                [
                    "--provider",
                    "response-local",
                    "--model",
                    "fixture",
                    "--thinking",
                    "off",
                    "--offline",
                    "--no-extensions",
                    "--no-skills",
                    "--no-context-files",
                    "--no-prompt-templates",
                    "--no-tools",
                ],
                text,
                str(self.project),
                session_file=str(self.session),
                persistent_session=self.persistent,
                steering_queue=queue,
                native_start=self.started,
                **options,
            ):
                result.append(event)
                self.observed.append(event)
                if followup is not None and isinstance(event, events.InputStarted):
                    queue.put_nowait(
                        {
                            "type": "prompt",
                            "message": followup,
                            "_input_id": "queued",
                            "streamingBehavior": "steer",
                        }
                    )
                    followup = None
        return result

    def saved_inputs(self):
        rows = map(json.loads, self.session.read_text().splitlines())
        return [
            row["message"]
            for row in rows
            if row.get("type") == "message" and row["message"].get("role") == "user"
        ]


@pytest.fixture
async def native_backend(tmp_path, monkeypatch):
    pin = os.environ.get("PI_COMPACTION_TEST_PACKAGE")
    if not pin:
        pytest.skip("Set PI_COMPACTION_TEST_PACKAGE to the immutable native bundle")
    package = Path(pin).resolve(strict=True)
    root, project, config = (tmp_path / name for name in ("wire", "project", "config"))
    project.mkdir(mode=0o700)
    config.mkdir(mode=0o700)
    session = create_fresh_private_session(tmp_path / "sessions", worktree=project).path
    provider = LoopbackProvider(status=200, text="Native response lifecycle.")
    connections = set()

    async def serve(reader, writer):
        task = asyncio.current_task()
        connections.add(task)
        try:
            await provider.handle(reader, writer)
        finally:
            connections.remove(task)

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    (config / "models.json").write_text(
        json.dumps(
            {
                "providers": {
                    "response-local": {
                        "baseUrl": f"http://127.0.0.1:{port}/v1",
                        "api": "openai-completions",
                        "models": [
                            {
                                "id": "fixture",
                                "name": "Local response fixture",
                                "contextWindow": 2000000,
                                "maxTokens": 8192,
                            }
                        ],
                    }
                }
            }
        )
    )
    (config / "auth.json").write_text(
        json.dumps({"response-local": {"type": "api_key", "key": "local-only"}})
    )
    (config / "settings.json").write_text(
        json.dumps(
            {
                "compaction": {"enabled": False},
                "retry": {"enabled": False, "maxRetries": 0, "provider": {"maxRetries": 0}},
            }
        )
    )
    root_id = Comms(root).messaging.initialize_private_initial_protocol()
    for key, value in {
        "AGENT_COMMS_ROOT": str(root),
        "AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID": root_id,
        "AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE": str(package),
        "AGENT_COMMS_NATIVE_CONFIG_DIR": str(config),
        "PI_CODING_AGENT_DIR": str(config),
    }.items():
        monkeypatch.setenv(key, value)
    owner = NativeBackendFixture(root, project, session, provider, config)
    try:
        yield owner
    finally:
        await owner.persistent.close()
        for child in owner.children:
            await child.stop()
        server.close()
        await server.wait_closed()
        for task in tuple(connections):
            task.cancel()
        await asyncio.gather(*connections, return_exceptions=True)
        assert all(not child.alive() for child in owner.children)


async def test_actual_native_queued_settlement_large_reuse_and_validated_reopen(native_backend):
    owner = native_backend
    first = await owner.run("first", followup="queued followup")
    assert isinstance(first[-1], events.Done) and first[-1].ok, first[-1]
    assert [row[2] for row in owner.starts] == ["first", "queued followup"]
    assert [row[0] for row in owner.starts] == [None, "queued"]
    assert len([event for event in first if isinstance(event, events.StreamSettled)]) == 1
    retained = owner.persistent.custody.child.proc
    assert retained is not None and retained.alive()

    await owner.persistent.discard_for_external_write(str(owner.session))
    assert not retained.alive()
    owner.provider.text = "Reopened retained context."
    reopened = await owner.run("after validated reopen")
    assert reopened[-1].ok and reopened[-1].text == owner.provider.text, reopened[-1]
    assert owner.persistent.custody.child.proc is not retained and owner.persistent.custody.child.proc.alive()
    assert owner.persistent.custody.idle().current
    reopened_child = owner.persistent.custody.child.proc
    # The deliberately oversized output tests framing, not permission to admit
    # a later prompt beyond the model's stored-context budget.
    owner.provider.text = "L" * (2 * 1024 * 1024 + 257)
    second = await owner.run("large response")
    assert second[-1].ok and second[-1].text == owner.provider.text, second[-1]
    assert owner.persistent.custody.child.proc is reopened_child
    assert len(owner.saved_inputs()) == 4
    print(f"ordinary_native_response_bytes={len(second[-1].text.encode())}")

    assert len(owner.starts) == len(owner.saved_inputs()) == owner.provider.posts == 4
    assert len({row[1] for row in owner.starts}) == 4
    proofs = [
        json.loads(row)
        for row in Path(str(owner.session) + ".input-proof").read_text().splitlines()
    ]
    assert {row["inputId"] for row in proofs} == {row[1] for row in owner.starts}


@pytest.mark.parametrize("termination", ["cancel", "eof", "owner_stop"])
async def test_actual_native_interrupted_turn_never_replays_or_retains(native_backend, termination):
    owner = native_backend
    owner.provider.status = 0
    turn = asyncio.create_task(owner.run("single uncertain input"))
    try:
        async with asyncio.timeout(15):
            while owner.provider.posts != 1:
                assert not turn.done(), turn.result() if turn.done() else None
                await asyncio.sleep(0.01)
        child = owner.children[-1]
        if termination == "cancel":
            turn.cancel()
            with pytest.raises(asyncio.CancelledError):
                await turn
        else:
            if termination == "owner_stop":
                await backend.terminate_task_process(turn)
            else:
                await child.stop()
            result = await turn
            assert isinstance(result[-1], events.Done) and not result[-1].ok
        assert turn not in backend.TurnSession.active
        assert not child.alive()
        assert not owner.persistent.available
        assert len(owner.starts) == len(owner.saved_inputs()) == owner.provider.posts == 1
    finally:
        if not turn.done():
            turn.cancel()
        await asyncio.gather(turn, return_exceptions=True)
