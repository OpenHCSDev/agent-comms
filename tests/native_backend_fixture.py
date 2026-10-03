"""Acquired original native fixture shared by pytest and installed drivers."""

import asyncio
import json
import os
from pathlib import Path
from contextlib import asynccontextmanager
from unittest.mock import patch

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

    async def force_reopen(self):
        """Retire this fixture's actual attested child, preserving saved identity."""
        identity = self.persistent.custody.child.attestation.require_identity()
        self.persistent.require_reopen(identity)
        await self.persistent.close_idle()

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


@asynccontextmanager
async def native_backend_fixture(tmp_path):
    pin = os.environ.get("PI_COMPACTION_TEST_PACKAGE")
    if not pin:
        raise ValueError("Set PI_COMPACTION_TEST_PACKAGE to the immutable native bundle")
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
    environment = {
        "AGENT_COMMS_ROOT": str(root),
        "AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID": root_id,
        "AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE": str(package),
        "AGENT_COMMS_NATIVE_CONFIG_DIR": str(config),
        "PI_CODING_AGENT_DIR": str(config),
    }
    owner = NativeBackendFixture(root, project, session, provider, config)
    with patch.dict(os.environ, environment):
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

