"""Native callbacks borrow the actual session's input, output and child facts."""
from __future__ import annotations
from abc import ABC, abstractmethod
from asyncio import Event
from pathlib import Path

from agent_comms import agent_events as events
from agent_comms.execution import pi_commands as commands
from agent_comms.execution.acp_failure import FailureSource
from agent_comms.agent_backend import InputStartListener
from agent_comms.execution.native_child import NativeChildState, PiSessionChild
from agent_comms.execution.native_pi import NativePiRpcLaunch
from agent_comms.execution.native_session_reopen import NativeSessionIdentity
from agent_comms.execution.native_startup import NativeStartup
from agent_comms.execution.session_retention import NativeSessionRetention
from agent_comms.execution.turn_failure import TurnFailure
from agent_comms.execution.pi_wire import PiDiagnostic
from agent_comms.execution.send_outcome import SendBoundary
from agent_comms.execution.turn_failure_reads import (
    TerminalInputsRead,
    TerminalOutputRead,
    TerminalPromptRead,
    TerminalSessionRead,
)
from agent_comms.transport.native_tool_transport import ToolTransport


class SessionInputsCallbacks(TerminalInputsRead):
    __slots__ = ()

    @property
    @abstractmethod
    def settled(self) -> bool: ...


class SessionOutputCallbacks(TerminalOutputRead):
    __slots__ = ()
    sensitive: bool

    @abstractmethod
    def record_failure(self, failure: TurnFailure) -> None: ...

    @abstractmethod
    def error(
        self, text: str, diagnostics: tuple[PiDiagnostic, ...] = (),
        *, source: FailureSource,
    ) -> events.Error: ...

    @abstractmethod
    def permits_retention(self, session: NativeSessionCallbacks) -> bool: ...


class SessionPromptCallbacks(TerminalPromptRead):
    __slots__ = ()

    @abstractmethod
    def validate_terminal(self, session: NativeSessionCallbacks, transport_ok: bool) -> None: ...

    @abstractmethod
    def permits_extension_ui(self, session: NativeSessionCallbacks) -> bool: ...


class SessionStatsCallbacks(ABC):
    __slots__ = ()
    complete: bool

    @abstractmethod
    async def request(self, session: NativeSessionCallbacks) -> None: ...


class NativeSessionCallbacks(TerminalSessionRead):
    __slots__ = ()
    inputs: SessionInputsCallbacks
    output: SessionOutputCallbacks
    prompt_state: SessionPromptCallbacks
    stats: SessionStatsCallbacks
    native: PiSessionChild
    native_session: NativeSessionRetention[PiSessionChild, NativeChildState, NativeSessionIdentity]
    startup: NativeStartup
    launch: NativePiRpcLaunch
    tool_socket: ToolTransport
    send_boundary: SendBoundary
    rejected_commands: list[events.InputRefused]
    rejected_signal: Event
    active_session_file: Path

    @abstractmethod
    def grant_prompt(self) -> None: ...


class PromptSessionCallbacks(NativeSessionCallbacks):
    __slots__ = ()
    command: commands.Prompt
    input_start: InputStartListener

    @abstractmethod
    def following_started(self, command: commands.FollowingPrompt) -> None: ...
