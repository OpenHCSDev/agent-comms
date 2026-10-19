"""Acquired original native fixture shared by pytest and installed drivers."""
from agent_comms.native_package import VerifiedNativePackage

import asyncio
import json
import os
from pathlib import Path
from contextlib import AsyncExitStack, asynccontextmanager
from functools import partial
from unittest.mock import patch

import agent_comms.agent_events as events
import agent_comms.backend as backend
from agent_comms.agent_backend import InputContent, InputId, InputRequest, TurnInbox, WhenBusy
from agent_comms.comms import Comms
from agent_comms.fresh_private_session import create_fresh_private_session
from agent_comms.child_process import BoundedRun, join_retirement
from agent_comms.coordinator import Coordination
from agent_comms.field_codec import FieldCodec
from agent_comms.pi_native_backend import PersistentPiSession
from agent_comms.selected_actions import NoSelectedTools
from agent_comms.owned_turn import OwnedTurn
from agent_comms.queued_input import InitialInput
from agent_comms.storage_rules import StorageRule
from delivery_owner_fixture import canonical_agent
from compaction_loopback import LoopbackProvider
from native_proof_cases import read_proof_rows


async def wait_for(predicate):
    async with asyncio.timeout(30):
        while not predicate():
            await asyncio.sleep(0.01)


class NativeBackendFixture:
    def __init__(self, root, project, session, provider, config, package: VerifiedNativePackage):
        self.root, self.project, self.session, self.provider = root, project, session, provider
        self.config = config
        self.package = package
        self.persistent = PersistentPiSession()
        self.starts = []
        self.children = []
        self.observed = []

    async def author_history(self):
        """The SDK authors saved rows; no provider or acknowledgement is invented."""
        package = str(self.package.path)
        result = await BoundedRun.run(
            ("node", "--input-type=module", "-e", """
import {pathToFileURL} from 'node:url';
import {join} from 'node:path';
const {SessionManager} = await import(pathToFileURL(join(process.argv[1],
  'dist/core/session-manager.js')));
const manager = SessionManager.open(process.argv[2]);
manager.appendModelChange('response-local','fixture');
manager.appendThinkingLevelChange('off');
manager.appendMessage({role:'user',content:'Original saved question',timestamp:1});
manager.appendMessage({role:'assistant',content:[{type:'text',text:'Original saved answer'}],
  api:'openai-completions',provider:'response-local',model:'fixture',stopReason:'stop',
  timestamp:2,usage:{input:8,output:4,cacheRead:0,cacheWrite:0,totalTokens:12,
    cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}}});
""", package, str(self.session)),
            timeout=10, cwd=self.project,
        )
        assert result.outcome.successful, result.stderr.decode()

    @staticmethod
    async def attach_saved_owner(agent, *, project, session):
        """Restore this saved declaration before acquiring its live process."""
        def acquire():
            comms = agent._comms
            owned = agent.sessions.declare_thread(str(project))
            return comms.threads.attach_session(owned, str(session), process=owned.process_identity)

        thread = await Coordination.run_worker(acquire)
        await agent.sessions.bind_owned(thread, thread.name)
        agent.inputs.ensure_live_drain(thread.name)
        return thread.name

    async def bind_saved_owner(self, agent, *, project, session):
        """Acquire the restored SDK source's original attested native child."""
        session_id = await self.attach_saved_owner(agent, project=project, session=session)
        original = await Coordination.run_worker(partial(agent._comms.registry.require, session_id))
        await agent.turns.backend_for(session_id, original).prepare(original)
        child = agent.turns.persistent_backends[session_id].child_state.child.proc
        self.children.append(child)
        return session_id

    @staticmethod
    def native_arguments(options=("--no-tools",)):
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
            await agent._runtime.start()
            session_id = await self.bind_saved_owner(agent, project=self.project, session=self.session)
            yield agent, session_id
        finally:
            await agent.shutdown()
            assert not agent.turns.turn_tasks and not any(state.inbox for state in agent.inputs.drained.values())
            assert not agent.turns.persistent_backends

    @asynccontextmanager
    async def original_input(self, agent, session_id, text):
        """Reserve and acquire through the same input/turn owners as ACP dispatch."""
        async with AsyncExitStack() as resources:
            async with InitialInput.reserve(
                agent.inputs, session_id, agent.sessions.require(session_id),
                text=text, prompt=text, echo=True, images=(), controller=None,
            ) as reservation:
                item = reservation.item
                resources.push_async_callback(reservation.release().aclose)
            # Native acquisition begins after the wire reservation ends; its
            # rollback actions remain with this turn until acquisition finishes.
            turn = OwnedTurn(
                agent.turns, session_id, agent.sessions.require(session_id), text,
                original_keys=(item.key,), initial_display_text=text,
                original_owner_input=True, original_goal_id=item.context.active_goal_id,
            )
            async with AsyncExitStack() as permits:
                assert await turn.acquire(resources, permits)
                yield turn

    async def force_reopen(self):
        """Retire this fixture's actual attested child; the next turn opens the saved session."""
        self.persistent.child_state.child.attestation.require_identity()
        await self.persistent.close_idle()

    def started(self, public_id, native_id, text):
        self.starts.append((public_id, native_id, text))
        child = backend.TurnSession.active[asyncio.current_task()].native.proc
        # Cleanup already belongs to the saved-session owner at native input start;
        # there is no later copy from transient TurnSession process fields.
        assert self.persistent.child_state.child.proc is child
        assert self.persistent.child_state.child.reader is not None
        if child not in self.children:
            self.children.append(child)
        return True

    async def run(self, text, *, followup=None, queue=None, **options):
        queue = queue if queue is not None else TurnInbox()
        result = []
        async with asyncio.timeout(25):
            async for event in backend.stream_agent_events(
                "pi",
                self.native_arguments(),
                text,
                str(self.project),
                root=self.root, session_file=str(self.session),
                persistent_session=self.persistent,
                package=self.package,
                tool_mode=NoSelectedTools(),
                steering_queue=queue,
                native_start=self.started,
                **options,
             session_directory=self.root):
                result.append(event)
                self.observed.append(event)
                if followup is not None and isinstance(event, events.InputStarted):
                    queue.put_nowait(InputRequest(
                        input_id=InputId("queued"),
                        content=InputContent(text=followup),
                        when_busy=WhenBusy.STEER,
                    ))
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
async def native_backend_fixture(tmp_path, *, package: VerifiedNativePackage):
    root, project, config = (tmp_path / name for name in ("wire", "project", "config"))
    root.mkdir(mode=0o700)
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
                            },
                            {
                                "id": "fixture-second",
                                "name": "Second local response fixture",
                                "contextWindow": 2000000,
                                "maxTokens": 8192,
                            },
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
    (root / "storage.json").write_text(json.dumps(FieldCodec.encode(
        StorageRule(scratch_base=project.parent),
    )))
    Comms(root).messaging.initialize_private_initial_protocol()
    environment = {
        "AGENT_COMMS_ROOT": str(root),
        "AGENT_COMMS_NATIVE_CONFIG_DIR": str(config),
        "PI_CODING_AGENT_DIR": str(config),
    }
    owner = NativeBackendFixture(root, project, session, provider, config, package)
    with patch.dict(os.environ, environment):
        try:
            yield owner
        finally:
            async def close_server():
                server.close()
                await server.wait_closed()
                for task in tuple(connections):
                    task.cancel()
                await asyncio.gather(*connections, return_exceptions=True)

            async def retire_all():
                try:
                    async with AsyncExitStack() as retirement:
                        retirement.push_async_callback(close_server)
                        for child in owner.children:
                            retirement.push_async_callback(child.stop)
                        retirement.push_async_callback(owner.persistent.close)
                finally:
                    assert all(child.retired for child in owner.children)
                    print(json.dumps({
                        "saved_native_fixture": str(root),
                        "localhost_posts": provider.posts,
                        "native_input_proofs": len(read_proof_rows(session))
                            if Path(str(session) + ".input-proof").is_file() else 0,
                        "children": [FieldCodec.encode(child.identity) for child in owner.children],
                        "children_retired": True,
                    }))

            await join_retirement(asyncio.create_task(retire_all()))
