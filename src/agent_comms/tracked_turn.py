"""Tracked native turns share Pi declarations and the existing turn-session authority."""

from __future__ import annotations

import asyncio
import math
import os
import re
from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager
from pathlib import Path

from . import pi_commands as commands
from . import pi_events as pi
from .agent_events import AgentEvent
from .backend import MODEL_WAIT_TIMEOUT_SECONDS, TurnSession
from .child_process import AttachedChild
from .errors import RelationViolationError
from .fresh_private_session import FreshPrivateSession
from .maintenance_barrier import MaintenanceBarrier
from .mro_dispatch import MroDispatch, handles
from .native_custody import PiSessionChild
from .native_pi import (
    CAPABILITY,
    NativeContextProof,
    NativePiPromptRejected,
    NativePiRpcLaunch,
    NativePiTerminalFailure,
    NativePiUnavailable,
    NativeTurnResult,
    _fresh_selected_revision,
    _require_reviewed_selected_source_cli,
    _session_location,
    _verify_context,
)
from .native_prompt_send import PromptSendUnknown, send_fenced_prompt
from .native_tool_call import SelectedToolDenied
from .pi_payloads import AssistantMessage, TextDelta
from .pi_rpc import PiRpcChannel
from .pi_vocabulary import ThinkingLevel
from .selected_tool_broker import NativeToolMode, OwnerToolSocket
from .store_files import _store_lock


class TrackedTurnSession(TurnSession, MroDispatch):
    """Own one strict input/proof exchange; never grant publication or retry authority."""

    def __init__(
        self,
        launch,
        command,
        *,
        provider,
        model,
        model_wait_timeout,
        prompt_send_boundary,
        maintenance_root,
        fresh_selected,
        selected_revision,
        selected_tool_mode,
        observe_event,
    ):
        super().__init__(
            launch,
            command.message,
            session_file=launch.session_file,
            model_wait_timeout=model_wait_timeout,
        )
        self.command = command
        self.provider, self.model = provider, model
        self.proc: AttachedChild | None = None
        self.initial_session_id: str | None = None
        self.prompt_send_boundary = prompt_send_boundary
        self.maintenance_root = maintenance_root
        self.fresh_selected, self.selected_revision = fresh_selected, selected_revision
        self.selected_tool_mode, self.observe_event = selected_tool_mode, observe_event
        self.tool_socket: OwnerToolSocket | None = None
        self.prompt_response: pi.Response | None = None
        self.input_event: pi.InputCommitted | None = None
        self.context_event: pi.ContextCommitted | None = None
        self.text_parts: list[str] = []
        self.final_messages: list[str] = []
        self.terminal_error: str | None = None
        self.finished = False

    @classmethod
    async def execute(
        cls,
        package: Path,
        *,
        input_id: str,
        prompt: str,
        worktree: Path,
        session_dir: Path,
        session_file: Path | None = None,
        provider: str = "openrouter",
        model: str = "z-ai/glm-5.3-flash",
        thinking_level: str | None = None,
        model_wait_timeout: float | None = MODEL_WAIT_TIMEOUT_SECONDS,
        prompt_send_boundary: Callable[..., AbstractContextManager[None]] | None = None,
        maintenance_root: Path | None = None,
        fresh_selected: FreshPrivateSession | None = None,
        selected_tool_mode: NativeToolMode | None = None,
        observe_event: Callable[[pi.PiEvent | AgentEvent], Awaitable[None]] | None = None,
    ) -> NativeTurnResult:
        if type(input_id) is not str or re.fullmatch(r"[0-9a-f]{32}", input_id) is None:
            raise ValueError("A native turn requires a 128-bit lowercase hex input ID")
        if not prompt or not isinstance(prompt, str):
            raise ValueError("A native turn requires a nonempty prompt")
        if model_wait_timeout is not None and (
            not math.isfinite(model_wait_timeout) or model_wait_timeout <= 0
        ):
            raise ValueError("A model progress wait must be positive and finite")
        selected_revision = None
        if fresh_selected is not None:
            if (
                type(fresh_selected) is not FreshPrivateSession
                or session_file != fresh_selected.path
                or not ThinkingLevel.supports_selected(fresh_selected.selected_thinking_level)
                or prompt_send_boundary is None
                or maintenance_root is None
            ):
                raise NativePiUnavailable("Selected first source requires enrolled locked prewrite")
            selected_revision = _fresh_selected_revision(fresh_selected)
            _require_reviewed_selected_source_cli()
        launch = NativePiRpcLaunch.tracked(
            package,
            worktree=worktree,
            session_dir=session_dir,
            session_file=session_file,
            provider=provider,
            model=model,
            thinking_level=thinking_level,
            selected_thinking_level=(
                fresh_selected.selected_thinking_level if fresh_selected else None
            ),
            selected_tool_mode=selected_tool_mode,
        )
        turn = cls(
            launch,
            commands.Prompt(id="native-prompt", input_id=input_id, message=prompt),
            provider=provider,
            model=model,
            model_wait_timeout=model_wait_timeout,
            prompt_send_boundary=prompt_send_boundary,
            maintenance_root=maintenance_root,
            fresh_selected=fresh_selected,
            selected_revision=selected_revision,
            selected_tool_mode=selected_tool_mode,
            observe_event=observe_event,
        )
        return await turn.complete()

    async def complete(self) -> NativeTurnResult:
        self.watchdog.launching(asyncio.get_running_loop().time, self.session_file)
        self.watchdog.reading(True)
        try:
            await self.open_tools()
            async with asyncio.timeout(self.watchdog.read_timeout(self)):
                self.proc = await AttachedChild.start(
                    self.launch.argv, cwd=self.launch.cwd, env=self.launch.env
                )
            self.watchdog.spawned(False)
            if self.tool_socket is not None:
                self.tool_socket.expected_pid = self.proc.pid
            self.stdin = self.proc.stdin
            assert self.stdin is not None and self.proc.stdout is not None
            self.reader = PiRpcChannel(self.proc.stdout)
            assert self.proc.stderr is not None
            self.stderr_task = asyncio.create_task(PiSessionChild.stderr_tail(self.proc.stderr))
            try:
                await self.attest()
                await self.admit_prompt()
                while not self.finished:
                    event = await self.next_event()
                    async for update in self.consume_native_event(event):
                        if self.observe_event is not None:
                            await self.observe_event(update)
                    if not self.finished and self.observe_event is not None:
                        await self.observe_event(event)
                return self.result()
            finally:
                self.reader.pending.cancel_all()
                await self.proc.stop()
                await asyncio.gather(self.stderr_task, return_exceptions=True)
        except SelectedToolDenied as error:
            raise NativePiUnavailable(str(error)) from error
        except PromptSendUnknown as error:
            raise NativePiUnavailable(
                f"Native Pi prompt send is UNKNOWN; no retry: {error}"
            ) from error
        except TimeoutError as error:
            raise NativePiUnavailable(
                f"Native Pi tracked operation expired; send may be UNKNOWN; "
                f"{self.watchdog.expired_wait(self)}"
            ) from error
        except OSError as error:
            raise NativePiUnavailable(
                f"Native Pi tracked transport failed; send may be UNKNOWN; "
                f"{type(error).__name__}: {error}"
            ) from error
        finally:
            if self.proc is not None:
                await self.proc.stop()
            if self.tool_socket is not None:
                await self.tool_socket.close()

    async def open_tools(self) -> None:
        if self.selected_tool_mode is not None:
            self.tool_socket = self.selected_tool_mode.socket(
                self.launch.session_dir, os.urandom(32).hex()
            )
            await self.tool_socket.start()
            self.launch.env["AGENT_COMMS_SELECTED_TOOL_SOCKET"] = str(self.tool_socket.path)
            self.launch.env["AGENT_COMMS_SELECTED_TOOL_TOKEN"] = self.tool_socket.token

    @property
    def awaiting_native_attestation(self):
        return self.initial_session_id is None

    @property
    def started_input(self):
        return self.input_event is not None

    @property
    def active_tools(self):
        return self.tool_socket.active_tools if self.tool_socket is not None else set()

    async def apply_native_event(self, event):
        await self.dispatch(event)
        if False:
            yield

    async def next_event(self) -> pi.PiEvent:
        try:
            event = await asyncio.wait_for(
                self.reader.receive(strict=True), timeout=self.watchdog.read_timeout(self)
            )
        except (UnicodeError, ValueError, TypeError) as error:
            raise NativePiUnavailable(
                f"Native Pi RPC record is invalid or incomplete; {type(error).__name__}: {error}"
            ) from error
        if event is None:
            await self.proc.finish()
            stderr = await self.stderr_task
            raise NativePiUnavailable(
                f"Native Pi RPC record is incomplete; native exit={self.proc.returncode}; "
                f"stderr={stderr or '(empty)'}"
            )
        return event

    async def send(self, command: commands.PiCommand) -> None:
        payload = self.reader.encode(command)
        if self.maintenance_root is not None:
            try:
                with _store_lock(self.maintenance_root / "wire"):
                    MaintenanceBarrier(
                        self.maintenance_root / "registry.json"
                    ).assert_open_unlocked()
                    self.stdin.write(payload)
            except RelationViolationError as error:
                raise NativePiUnavailable(
                    "Maintenance closed before Pi capability preflight"
                ) from error
        else:
            self.stdin.write(payload)
        await asyncio.wait_for(self.stdin.drain(), timeout=self.watchdog.read_timeout(self))

    async def attest(self) -> None:
        request = commands.GetState(id="native-capability")
        await self.send(request)
        event = await self.next_event()
        if not isinstance(event, pi.Response) or self.reader.correlate(event) is not request:
            raise NativePiUnavailable("Native Pi emitted an unexpected preflight event")
        state = event.data
        if (
            event.success is not True
            or state is None
            or state.native_input_proof_capability != CAPABILITY
            or not state.session_id
        ):
            raise NativePiUnavailable("Patched persisted Pi capability is unavailable")
        if state.session_file is None:
            raise NativePiUnavailable("Native Pi omitted its private session file")
        self.active_session_file = _session_location(self.launch.session_dir, state.session_file)
        if self.session_file is not None and self.active_session_file != self.session_file:
            raise NativePiUnavailable("Native Pi rebound its session")
        self.initial_session_id = state.session_id
        if self.fresh_selected is not None:
            self.attest_selected(state)

    def attest_selected(self, state) -> None:
        selected = self.fresh_selected
        if (
            state.session_id != selected.session_id
            or state.model is None
            or state.model.provider != "openrouter"
            or state.model.id != "z-ai/glm-5.3-flash"
            or ThinkingLevel.optional_name(state.thinking_level) != selected.selected_thinking_level
            or state.message_count != 0
            or state.pending_message_count != 0
            or state.is_streaming is not False
            or state.is_compacting is not False
        ):
            raise NativePiUnavailable("Selected first source runtime or inode differs")
        revision = _fresh_selected_revision(selected, started=True)
        if revision[:2] != self.selected_revision[:2]:
            raise NativePiUnavailable("Selected startup changed enrolled inode")
        self.selected_revision = revision

    def prompt_boundary(self):
        if self.fresh_selected is not None:
            return self.prompt_send_boundary(self.active_session_file, self.selected_revision)
        return self.prompt_send_boundary(self.active_session_file)

    async def admit_prompt(self) -> None:
        self.watchdog.await_input()
        if self.prompt_send_boundary is None:
            await self.send(self.command)
        else:
            await send_fenced_prompt(
                self.stdin,
                self.reader.encode(self.command),
                self.prompt_boundary,
                timeout=self.watchdog.read_timeout(self),
            )

    @handles(pi.Response)
    async def response(self, event: pi.Response) -> None:
        request = self.reader.correlate(event)
        if event.id == self.command.id:
            if request is not self.command:
                raise NativePiUnavailable("Native Pi prompt acknowledgement differs")
            if event.success is not True:
                raise NativePiPromptRejected(event)
            self.prompt_response = event
        elif issubclass(event.command, commands.MutatesSession):
            raise NativePiUnavailable("Native Pi session identity changed during a turn")

    @handles(pi.InputCommitted)
    async def committed_input(self, event: pi.InputCommitted) -> None:
        if event.input_id == self.command.input_id:
            if self.input_event is not None:
                raise NativePiUnavailable("Native Pi repeated the input commitment")
            self.input_event = event

    @handles(pi.ContextCommitted)
    async def committed_context(self, event: pi.ContextCommitted) -> None:
        if event.input_id == self.command.input_id:
            self.context_event = event

    @handles(pi.MessageUpdate)
    async def message_update(self, event: pi.MessageUpdate) -> None:
        if event.assistant_message_event is not None:
            await self.dispatch(event.assistant_message_event)

    @handles(TextDelta)
    async def text_delta(self, delta: TextDelta) -> None:
        self.text_parts.append(delta.delta)

    @handles(pi.MessageEnd)
    async def message_end(self, event: pi.MessageEnd) -> None:
        await self.dispatch(event.message)

    @handles(AssistantMessage)
    async def assistant_end(self, message: AssistantMessage) -> None:
        if message.error_message:
            self.terminal_error = str(message.error_message)
            return
        content = message.content
        if content is None or isinstance(content, str):
            raise NativePiUnavailable("Native Pi assistant content is malformed")
        message.stop_reason.tracked(self, message)

    def accept_tool_round(self, message):
        if self.tool_socket is None:
            return False
        self.tool_socket.announce(message.content)
        self.text_parts.clear()
        self.final_messages.clear()
        return True

    def accept_final_message(self, message):
        if self.tool_socket is not None:
            self.tool_socket.assert_complete()
        if any(not item.final_text_allowed for item in message.content):
            raise NativePiUnavailable("Native Pi assistant returned non-text content")
        self.final_messages.append("".join(item.text for item in message.content))

    def context_proof(self) -> NativeContextProof:
        if self.prompt_response is None or self.input_event is None or self.context_event is None:
            raise NativePiUnavailable("Native Pi did not commit a tracked model context")
        return _verify_context(
            self.active_session_file,
            self.command.input_id,
            self.initial_session_id,
            self.input_event,
            self.context_event,
        )

    @handles(pi.ToolExecutionStart)
    async def tool_started(self, event: pi.ToolExecutionStart) -> None:
        if self.tool_socket is None:
            raise NativePiUnavailable("Native Pi tool preceded tracked context proof")
        self.context_proof()
        self.tool_socket.tool_started(event)

    @handles(pi.ToolExecutionEnd)
    async def tool_finished(self, event: pi.ToolExecutionEnd) -> None:
        if self.tool_socket is None:
            raise NativePiUnavailable("Native Pi tool has no owner policy")
        self.tool_socket.tool_finished(event, self.command.input_id)

    @handles(pi.AgentSettled)
    async def settled(self, event: pi.AgentSettled) -> None:
        self.finished = True

    def result(self) -> NativeTurnResult:
        proof = self.context_proof()
        if self.terminal_error is not None:
            raise NativePiTerminalFailure(self.terminal_error, proof, self.provider, self.model)
        if self.tool_socket is not None:
            self.tool_socket.assert_complete()
        if (
            len(self.final_messages) != 1
            or not self.final_messages[0]
            or "".join(self.text_parts) != self.final_messages[0]
        ):
            raise NativePiUnavailable("Native Pi has no unique authoritative completed response")
        if self.selected_tool_mode is not None:
            self.selected_tool_mode.finish()
        return NativeTurnResult(
            self.final_messages[0].strip(),
            proof,
            self.tool_socket.selected_call_id if self.tool_socket else None,
        )
