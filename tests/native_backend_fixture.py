"""Acquired original native fixture: a saved Pi session, its owner and a loopback provider."""

import asyncio
import json
import os
from contextlib import AsyncExitStack, asynccontextmanager
from functools import partial
from pathlib import Path
from unittest.mock import patch

import agent_comms.agent_events as events
import agent_comms.execution.backend as backend
from agent_comms.execution.acp import SdkSessionConnection
from agent_comms.agent_backend import NoInputStartListener
from agent_comms.comms import wire
from agent_comms.coordination.coordinator import Coordination
from agent_comms.execution.child_process import (
    BoundedRun,
    join_retirement,
)
from agent_comms.execution.controller import NO_CONTROLLER, NoController
from agent_comms.execution.cursor_publication import CursorDelivery
from agent_comms.execution.fresh_private_session import create_fresh_private_session
from agent_comms.execution.native_arguments import GivenArguments, GivenBinary, NativeArguments
from agent_comms.execution.native_child import PiSessionChild
from agent_comms.execution.selected.actions import NoSelectedTools
from agent_comms.execution.turn_inbox import (
    InputContent,
    InputRequest,
    SteerInputPolicy,
    TurnInbox,
)
from agent_comms.installer.native_package import VerifiedNativePackage
from agent_comms.nominal.field_codec import AcpSessionId, InputId
from agent_comms.execution.persistent_pi_session import PersistentPiSession
from agent_comms.projects.checkouts import Checkout
from agent_comms.projects.storage_rules import StorageRule
from agent_comms.registry.thread_identity import RecordedThreadIncarnation
from agent_comms.registry.threads import NativeThread
from compaction_loopback import LoopbackProvider
from delivery_owner_fixture import canonical_agent


async def wait_for(predicate):
    async with asyncio.timeout(30):
        while not predicate():
            await asyncio.sleep(0.01)


class AnnouncedInputStart(NoInputStartListener):
    started_events = (events.OriginalInputStarted(),)


class NativeBackendFixture(AnnouncedInputStart):
    @staticmethod
    def participant_at(root: Path, project: Path) -> RecordedThreadIncarnation:
        """The real thread incarnation used by a direct native turn on this bus."""
        return NativeThread(
            'direct-native', frozenset(), Checkout(project)).incarnation.require_recorded()

    def __init__(self, root, project, session, provider, config, package: VerifiedNativePackage):
        self.root, self.project, self.session, self.provider = root, project, session, provider
        self.config = config
        self.package = package
        self.persistent = PersistentPiSession()
        self.cursor_delivery = CursorDelivery()
        self.participant = self.participant_at(root, project)
        self.starts = []
        self.children: list[PiSessionChild] = []
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
        identifier = AcpSessionId(thread.name)
        await agent.sessions.bind_owned(thread, identifier)
        agent.inputs.ensure_live_drain(identifier)
        return identifier

    async def bind_saved_owner(self, agent, *, project, session):
        """Acquire the restored SDK source's original attested native child."""
        session_id = await self.attach_saved_owner(agent, project=project, session=session)
        original = await Coordination.run_worker(partial(agent._comms.registry.require, session_id))
        await agent.turns.backend_for(session_id, original).prepare(original)
        child = agent.turns.persistent_backends[session_id].child_state.child
        self.children.append(child)
        return session_id

    @staticmethod
    def native_arguments(options=("--no-tools",)):
        """One external native argument declaration for saved/ACP consumers."""
        return ("--provider", "response-local", "--model", "fixture", "--thinking", "off",
                "--offline", "--no-extensions", "--no-skills", "--no-context-files",
                "--no-prompt-templates", *options)

    @asynccontextmanager
    async def open_owner(self, *, auto_wake: bool,
                         native_options=("--no-tools",),
                         client: SdkSessionConnection | NoController = NO_CONTROLLER):
        """Acquire the real saved ACP owner and attested idle native child."""
        agent = canonical_agent(
            wire(self.root), auto_wake=auto_wake, agent_bin=GivenBinary("pi"),
            agent_args=GivenArguments(tuple(self.native_arguments(native_options))),
        )
        agent.on_connect(client)
        try:
            session_id = await self.bind_saved_owner(agent, project=self.project, session=self.session)
            yield agent, session_id
        finally:
            states = tuple(agent.inputs.drained.values())
            await agent.shutdown()
            assert not any(state.turn_task.busy or state.turn_open() for state in states)
            assert not agent.turns.persistent_backends

    def original(self, native_id: str, text: str) -> bool:
        return self._observe_start(None, native_id, text)

    def following(self, public_id: InputId, native_id: str, text: str) -> bool:
        return self._observe_start(public_id, native_id, text)

    def _observe_start(self, public_id, native_id, text):
        self.starts.append((public_id, native_id, text))
        child = backend.TurnSession.active[asyncio.current_task()].native
        # Cleanup already belongs to the saved-session owner at native input start;
        # there is no later copy from transient TurnSession process fields.
        assert self.persistent.child_state.child is child
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
                root=self.root, owner=self.participant, session_file=str(self.session),
                persistent_session=self.persistent,
                package=self.package,
                tool_mode=NoSelectedTools(),
                steering_queue=queue,
                input_start=self,
                cursor_delivery=self.cursor_delivery,
                **options,
             session_directory=self.root):
                result.append(event)
                self.observed.append(event)
                if followup is not None and isinstance(event, events.InputStarted):
                    queue.put_nowait(InputRequest(
                        input_id=InputId.mint(),
                        content=InputContent(text=followup),
                        when_busy=SteerInputPolicy,
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
async def native_backend_fixture(tmp_path, *, package: VerifiedNativePackage,
                                 provider_type: type[LoopbackProvider] = LoopbackProvider):
    root, project, config = (tmp_path / name for name in ("wire", "project", "config"))
    root.mkdir(mode=0o700)
    project.mkdir(mode=0o700)
    from agent_comms.projects.repo import Repo
    Repo.git(project, 'init', '--quiet')
    config.mkdir(mode=0o700)
    session = create_fresh_private_session(tmp_path / "sessions", worktree=project).path
    provider = provider_type(status=200, text="Native response lifecycle.")
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
    StorageRule().prepare_isolated(root)
    wire(root)
    environment = {
        NativeArguments.binary_key: NativeArguments.default_binary,
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
                async with AsyncExitStack() as retirement:
                    retirement.callback(owner.cursor_delivery.close)
                    retirement.push_async_callback(close_server)
                    for child in owner.children:
                        retirement.push_async_callback(child.shutdown)
                    retirement.push_async_callback(owner.persistent.close)
                assert all(child.retired or child.release_failure is not None for child in owner.children)

            await join_retirement(asyncio.create_task(retire_all()))
