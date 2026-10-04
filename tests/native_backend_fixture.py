"""Acquired original native fixture shared by pytest and installed drivers."""

import asyncio
import json
import os
from pathlib import Path
from contextlib import AsyncExitStack, ExitStack, asynccontextmanager
from functools import partial
from unittest.mock import patch

from agent_comms import agent_events as events
from agent_comms import backend
from agent_comms.comms import Comms
from agent_comms.fresh_private_session import create_fresh_private_session
from agent_comms.child_process import BoundedRun
from agent_comms.coordinator import Coordination
from agent_comms.field_codec import FieldCodec
from agent_comms.owned_turn import OwnedTurn
from agent_comms.owner_compaction_prepare import NativeWitness
from agent_comms.queued_input import InitialInput
from agent_comms.store_files import _store_lock
from delivery_owner_fixture import canonical_agent
from compaction_loopback import LoopbackProvider


class NativeBackendFixture:
    def __init__(self, root, project, session, provider, config):
        self.root, self.project, self.session, self.provider = root, project, session, provider
        self.config = config
        self.persistent = backend.PersistentPiSession()
        self.starts = []
        self.children = []
        self.observed = []

    async def author_history(self):
        """The SDK authors saved rows and their cut; no provider or receipt is invented."""
        package = os.environ["PI_COMPACTION_TEST_PACKAGE"]
        result = await BoundedRun.run(
            ("node", "--input-type=module", "-e", """
import {pathToFileURL} from 'node:url';
import {join} from 'node:path';
const {SessionManager} = await import(pathToFileURL(join(process.argv[1],
  'dist/core/session-manager.js')));
const manager = SessionManager.open(process.argv[2]);
manager.appendModelChange('response-local','fixture');
manager.appendThinkingLevelChange('off');
const kept = manager.appendMessage({role:'user',content:'Original saved question',timestamp:1});
manager.appendMessage({role:'assistant',content:[{type:'text',text:'Original saved answer'}],
  api:'openai-completions',provider:'response-local',model:'fixture',stopReason:'stop',
  timestamp:2,usage:{input:8,output:4,cacheRead:0,cacheWrite:0,totalTokens:12,
    cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}}});
console.log(JSON.stringify(manager.captureCompactionWitness(kept)));
""", package, str(self.session)),
            timeout=10, cwd=self.project,
        )
        assert result.outcome.successful, result.stderr.decode()
        return FieldCodec.decode(NativeWitness, json.loads(result.stdout))

    @staticmethod
    async def attach_saved_owner(agent, *, project, session):
        """Restore this saved declaration before acquiring its live process."""
        def acquire():
            comms = agent._comms
            arguments = agent.sessions.agent_args
            original = comms.threads.claim_thread(
                agent.sessions.thread_name_for(str(project)), tags=frozenset({"acp"}),
                worktree=str(project), start_at_latest=True,
                model=arguments.model, thinking_level=arguments.thinking,
                auto_title_pending=True,
            )
            # This original stopped-restoration producer commits membership;
            # a reader or an active fixture must never invent participant rows.
            comms.threads.restore_stopped(comms.registry.snapshot(), (original.name,))
            owned = comms.owners.acquire_thread(original.name, owner_pid=os.getpid())
            return comms.threads.attach_session(owned, str(session))

        thread = await Coordination.run_worker(acquire)
        await agent.sessions.bind_owned(thread, thread.name)
        agent.inputs.ensure_live_drain(thread.name)
        return thread.name

    async def bind_saved_owner(self, agent, *, project, session):
        """Acquire the restored SDK source's original attested native child."""
        session_id = await self.attach_saved_owner(agent, project=project, session=session)
        original = await Coordination.run_worker(partial(agent._comms.registry.require, session_id))
        await agent.turns.prepare_selected_session(session_id, original)
        child = agent.turns.persistent_backends[session_id].custody.child.proc
        self.children.append(child)
        return session_id

    def native_arguments(self, options=("--no-tools",)):
        """One external native argument declaration for saved/ACP consumers."""
        return ("--provider", "response-local", "--model", "fixture", "--thinking", "off",
                "--offline", "--no-extensions", "--no-skills", "--no-context-files",
                "--no-prompt-templates", *options)

    @asynccontextmanager
    async def open_owner(self, *, runtime_enabled=False, auto_wake=False,
                         native_options=("--no-tools",), client=None):
        """Acquire the real saved ACP owner and attested idle native child."""
        agent = canonical_agent(
            Comms(self.root), auto_wake=auto_wake, runtime_enabled=runtime_enabled, agent_bin="pi",
            agent_args=self.native_arguments(native_options),
        )
        agent.on_connect(client)
        try:
            session_id = await self.bind_saved_owner(agent, project=self.project, session=self.session)
            yield agent, session_id
        finally:
            await agent.shutdown()
            assert not agent.turns.turn_tasks and not agent.inputs.backend_inboxes
            assert not agent.turns.persistent_backends

    @asynccontextmanager
    async def original_input(self, agent, session_id, text):
        """Reserve and acquire through the same input/turn owners as ACP dispatch."""
        def capture(custody):
            with _store_lock(agent._comms._wire_lock_path):
                return InitialInput.capture(
                    agent.inputs, agent.sessions.require(session_id), text=text, prompt=text,
                    echo=True, images=(), controller=None, custody=custody,
                )

        async with AsyncExitStack() as resources:
            reservation = ExitStack()
            resources.push_async_callback(Coordination.run_worker, reservation.close)
            item, _owner = await Coordination.run_worker(partial(capture, reservation))
            turn = OwnedTurn(
                agent.turns, session_id, agent.sessions.require(session_id), text,
                original_keys=(item.key,), initial_display_text=text,
                original_owner_input=True, original_goal_id=item.context.active_goal_id,
            )
            async with AsyncExitStack() as permits:
                assert await turn.acquire(resources, permits)
                yield turn

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
                self.native_arguments(),
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
