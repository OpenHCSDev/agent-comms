"""State-owning ACP attachment lifecycle; turn and delivery effects stay external."""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any

from acp import RequestError
from acp.schema import (
    AgentCapabilities,
    Implementation,
    InitializeResponse,
    LoadSessionResponse,
    NewSessionResponse,
    PromptCapabilities,
    SessionInfoUpdate,
    TerminalAuthMethod,
)

from .pi_vocabulary import ThinkingLevel
from .acp_extension import (
    ContextUsage,
    CoordinationChangedUpdate,
    GoalChangedUpdate,
    encode_updates,
)
from .comms import Comms
from .config_options import ConfigOptions
from .native_arguments import NativeArguments
from .runtime import RuntimeProxy, RuntimeServer
from .session_effects import SessionEffects
from .thread_identity import ThreadIncarnation
from .threads import Thread
from .transcript_updates import TranscriptReplay
from .session_load import SessionLoadAdmission, FailedSessionLoadAdmission


class SessionLifecycle:
    def __init__(
        self,
        comms: Comms,
        agent_bin: str,
        agent_args: NativeArguments,
        runtime: RuntimeServer,
        runtime_enabled: bool,
        effects: SessionEffects,
    ):
        self.comms, self.agent_bin, self.agent_args = comms, agent_bin, agent_args
        self.runtime, self.runtime_enabled, self.effects = runtime, runtime_enabled, effects
        self.bindings: dict[str, str] = {}
        self.client: Any = None
        self.titles: dict[str, str] = {}
        self.display_titles: dict[str, str | None] = {}
        self.worktrees: dict[str, str] = {}
        self.proxies: dict[str, RuntimeProxy] = {}
        self.transcript = TranscriptReplay(comms, runtime)
        self.config = ConfigOptions(comms, agent_bin, agent_args, runtime, self, effects)

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: Any = None,
        client_info: Any = None,
    ) -> InitializeResponse:
        capabilities = (
            client_capabilities
            if isinstance(client_capabilities, dict)
            else (
                client_capabilities.model_dump(by_alias=True, exclude_none=True)
                if client_capabilities is not None
                else {}
            )
        )
        methods: list[Any] = []
        if (capabilities.get("auth") or {}).get("terminal"):
            methods = [
                TerminalAuthMethod(
                    type="terminal",
                    id=f"pi-login-{provider or 'all'}",
                    name=name,
                    description="Sign in using Pi's native provider UI",
                    args=["--login", provider] if provider else ["--login"],
                )
                for provider, name in (
                    ("openai-codex", "ChatGPT subscription"),
                    ("openrouter", "OpenRouter"),
                    ("openai", "OpenAI API"),
                    ("", "Other Pi provider"),
                )
            ]
        return InitializeResponse(
            protocol_version=protocol_version,
            agent_capabilities=AgentCapabilities(
                load_session=True,
                prompt_capabilities=PromptCapabilities(image=True),
            ),
            agent_info=Implementation(name="agent-comms", title="Agent Comms", version="0.1.0"),
            auth_methods=methods,
        )

    @staticmethod
    def thread_name_for(cwd: str) -> str:
        leaf = re.sub(r"[^A-Za-z0-9_-]+", "-", Path(cwd).name or "session").strip("-")
        return leaf or "session"

    def require(self, session_id: str) -> str:
        name = self.bindings.get(session_id)
        if name is None:
            raise RequestError.invalid_params({"reason": f"Unknown sessionId: {session_id!r}"})
        return name

    @staticmethod
    def reject_foreign_mcp(mcp_servers: list[Any] | None) -> None:
        if mcp_servers is not None and (type(mcp_servers) is not list or mcp_servers):
            raise RequestError.invalid_params(
                {
                    "reason": (
                        "ACP mcpServers are unsupported; use Pi's native MCP package configuration."
                    )
                }
            )

    def declare_thread(self, cwd: str, owner_pid: int) -> Thread:
        return self.comms.threads.claim_thread(
            self.thread_name_for(cwd),
            tags=frozenset({"acp"}),
            worktree=cwd,
            pid=owner_pid,
            start_at_latest=True,
            model=self.agent_args.model,
            thinking_level=self.agent_args.thinking,
            auto_title_pending=True,
        )

    def validated_thread(self, cwd: str, session_id: str) -> Thread:
        thread = self.comms.registry.require(session_id)
        known_paths = {
            str(Path(path).expanduser().resolve())
            for path in (thread.worktree, *thread.previous_worktrees)
        }
        if str(Path(cwd).expanduser().resolve()) not in known_paths:
            raise RequestError.invalid_params(
                {"reason": "The saved thread belongs to a different working directory."}
            )
        return thread

    async def bind_owned(self, thread: Thread, session_id: str) -> None:
        self.bindings[session_id] = thread.name
        self.titles[session_id] = thread.name
        self.worktrees[session_id] = thread.worktree
        if self.runtime_enabled:
            await self.runtime.start()

    async def new_session(
        self, cwd: str, mcp_servers: list[Any] | None = None, **kwargs: Any
    ) -> NewSessionResponse:
        self.reject_foreign_mcp(mcp_servers)
        self.effects._private_nk_marker()
        thread = self.declare_thread(cwd, os.getpid())
        await self.bind_owned(thread, thread.name)
        self.effects.inputs.ensure_live_drain(thread.name)
        options = await self.config.session_options(thread.name, thread.name)
        return NewSessionResponse(
            session_id=thread.name,
            config_options=options,
            field_meta=self.metadata(thread.name, session_id=thread.name),
        )

    async def load_session(
        self, cwd: str, session_id: str, mcp_servers: list[Any] | None = None, **kwargs: Any
    ) -> LoadSessionResponse:
        self.reject_foreign_mcp(mcp_servers)
        self.effects._private_nk_marker()
        thread = self.validated_thread(cwd, session_id)
        if not (
            self.bindings.get(session_id) == thread.name
            and thread.pid == os.getpid()
            and self.comms.registry.status(thread.name).active
        ):
            thread = self.comms.owners.acquire_thread(thread.name, owner_pid=os.getpid())
        if thread.pid != os.getpid():
            return await self.attach_owner(thread, session_id)
        self.comms.threads.heartbeat(thread.name)
        await self.bind_owned(thread, session_id)
        await self.transcript.replay(session_id, thread.name)
        await self.effects.inputs.replay_unknown_inputs(session_id)
        options = await self.config.session_options(session_id, thread.name)
        self.effects.inputs.ensure_live_drain(session_id)
        return LoadSessionResponse(
            config_options=options,
            field_meta=self.metadata(thread.name, session_id=session_id),
        )

    async def attach_owner(self, thread: Thread, session_id: str) -> LoadSessionResponse:
        snapshot = self.comms.registry.snapshot()
        binding = snapshot.owner_binding(thread.name)
        failed_command = FailedSessionLoadAdmission(binding)
        proxy = self.effects._create_runtime_proxy(thread, session_id)
        try:
            snapshot.require_owner_process(snapshot.owner_identity(thread.name), thread.require_process())
            metadata = await proxy.subscribe()
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            await proxy.close()
            raise RequestError.invalid_params(
                {"reason": f"Unable to attach to {thread.name!r} owner {thread.pid}: {error}",
                 **failed_command.failure_metadata()}
            ) from error
        self.proxies[session_id] = proxy
        return LoadSessionResponse(
            config_options=metadata.pop("configOptions", []), field_meta=metadata
        )

    async def sync_identity(self, session_id: str) -> str:
        cached_name = self.require(session_id)
        thread = self.comms.registry.require(cached_name)
        name = thread.name
        if (
            name != cached_name
            or thread.pid != os.getpid()
            or not self.comms.registry.status(name).running
        ):
            await self.effects.turns.close_idle_backend(session_id)
        self.bindings[session_id] = name
        if (
            self.titles.get(session_id) != name
            or self.display_titles.get(session_id) != thread.title
        ):
            await self.runtime.session_update(
                session_id=session_id,
                update=SessionInfoUpdate(
                    session_update="session_info_update",
                    title=thread.title or name,
                    field_meta=self.metadata(name, session_id=session_id),
                ),
            )
            self.titles[session_id], self.display_titles[session_id] = name, thread.title
        if self.worktrees.get(session_id) != thread.worktree:
            await self.runtime.session_update(
                session_id=session_id,
                update=SessionInfoUpdate(
                    session_update="session_info_update",
                    field_meta=self.metadata(name, session_id=session_id),
                ),
            )
            self.worktrees[session_id] = thread.worktree
        return name

    def metadata(self, thread_name: str, *, session_id: str | None = None) -> dict[str, Any]:
        thread = self.comms.registry.require(thread_name)
        goal, execution = self.comms.goals.goal_snapshot(thread_name)
        info = self.comms.agents.agent_info_of(thread_name)
        usage = (
            ContextUsage(info.context_used, info.context_size)
            if info is not None and info.context_used is not None and info.context_size
            else None
        )
        return encode_updates(
            CoordinationChangedUpdate(
                ThreadIncarnation(thread.name, thread.created_at),
                str(self.comms.root.resolve()),
                os.getpid(),
                thread.worktree,
                thread.model,
                ThinkingLevel.optional_name(thread.thinking_level),
                thread.title or thread.name,
                usage,
            ),
            GoalChangedUpdate(goal, execution),
            self.effects.inputs.queue_state(session_id or thread_name),
            *self.effects.cursors.trusted_metadata(thread_name, session_id or thread_name),
        )

    async def close_proxies(self) -> None:
        for proxy in self.proxies.values():
            await proxy.close()
        self.proxies.clear()

    async def release_owned(self) -> None:
        for name in set(self.bindings.values()):
            try:
                canonical = self.comms.registry.require(name).name
                if (
                    self.comms.registry.require(canonical).pid == os.getpid()
                    and self.comms.registry.status(canonical).running
                ):
                    self.comms.owners.stop(canonical)
            except Exception as error:
                self.effects._debug_log(f"shutdown error: {error!r}")
        await self.runtime.close()


class AttachedSessionLifecycle(SessionLifecycle):
    """A stdio attachment requests a separate executor; it never claims ownership."""

    async def new_session(
        self, cwd: str, mcp_servers: list[Any] | None = None, **kwargs: Any
    ) -> NewSessionResponse:
        self.reject_foreign_mcp(mcp_servers)
        thread = self.declare_thread(cwd, 0)
        loaded = await self.load_session(cwd, thread.name, mcp_servers, **kwargs)
        return NewSessionResponse(
            session_id=thread.name,
            config_options=loaded.config_options,
            field_meta=loaded.field_meta,
        )

    async def load_session(
        self, cwd: str, session_id: str, mcp_servers: list[Any] | None = None, **kwargs: Any
    ) -> LoadSessionResponse:
        self.reject_foreign_mcp(mcp_servers)
        thread = self.validated_thread(cwd, session_id)
        try:
            admission = SessionLoadAdmission.at_ingress(kwargs.get("agentCommsLoad"))
            owner = await admission.resolve(self, thread)
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            raise RequestError.invalid_params(
                {"reason": f"Session load not admitted for {thread.name!r}: {error}"}
            ) from error
        return await self.attach_owner(owner, session_id)
