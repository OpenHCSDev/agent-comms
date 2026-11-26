"""Live Antigravity run-state sync and self-healing tmux wake recovery.

Antigravity writes no closing `transcript.jsonl` record when a turn aborts on quota
exhaustion, an API error, or cancellation; meanwhile its local `ConversationService`
reports authoritative `state.runState` (`RUN_STATE_IDLE` / `RUN_STATE_RUNNING`).
`AntigravitySessionSupervisor` runs inside `agent-comms-inbox-watch` to:

1. Emit `TurnIdle` over the watch WebSocket whenever `GetConversation` reports
   `RUN_STATE_IDLE` after transcript activity or a `RUNNING -> IDLE` transition,
   retiring stuck turns immediately on the bus.
2. Recover sessions stuck idle at an empty `>` prompt — either because a session
   forgot to arm `--wait-log` or because a quota error aborted the turn after its
   one-shot `--wait-log` exited — respecting any `Resets in ...` quota countdown
   and never typing over active human input.
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import ClassVar, Self

from aiohttp import ClientError, ClientSession, ClientTimeout, ClientWebSocketResponse
from pydantic import Field

from agent_comms.cli_bridges.claude_transcript import TurnIdle
from agent_comms.cli_bridges.wire_models import WireModel
from agent_comms.nominal.declared_family import DeclaredFamily


class AntigravityRunState(DeclaredFamily, affix="RunState"):
    """Live execution state reported by Antigravity's local `ConversationService`."""

    is_idle: ClassVar[bool] = False

    @classmethod
    async def apply(
        cls,
        supervisor: AntigravitySessionSupervisor,
        socket: ClientWebSocketResponse,
    ) -> None:
        """Apply this observed run state to `supervisor`; unknown states change nothing."""


class IdleRunState(AntigravityRunState, declared_name="RUN_STATE_IDLE"):
    is_idle = True

    @classmethod
    async def apply(
        cls,
        supervisor: AntigravitySessionSupervisor,
        socket: ClientWebSocketResponse,
    ) -> None:
        await supervisor._on_idle(socket)


class ActiveRunState(AntigravityRunState, declared_name=None):
    """Any run state where Antigravity is actively executing a turn."""

    @classmethod
    async def apply(
        cls,
        supervisor: AntigravitySessionSupervisor,
        socket: ClientWebSocketResponse,
    ) -> None:
        del socket
        supervisor._on_active()


class RunningRunState(ActiveRunState, declared_name="RUN_STATE_RUNNING"):
    pass


class CancellingRunState(ActiveRunState, declared_name="RUN_STATE_CANCELLING"):
    pass


class UnknownRunState(AntigravityRunState, declared_name="unknown"):
    pass


class ConversationStatePayload(WireModel):
    """The `state` object returned by `product.v1.ConversationService/GetConversation`."""

    run_state: str = Field(default="", alias="runState")


class ConversationReply(WireModel):
    """Top-level reply from `product.v1.ConversationService/GetConversation`."""

    state: ConversationStatePayload = Field(default_factory=ConversationStatePayload)

    def run_state(self) -> type[AntigravityRunState]:
        try:
            return AntigravityRunState.member_named(self.state.run_state)
        except KeyError:
            return UnknownRunState


class RunStateProbe(ABC):
    """Source of an Antigravity conversation's live execution state."""

    @abstractmethod
    async def run_state(self, session: ClientSession) -> type[AntigravityRunState]: ...


@dataclass(frozen=True)
class AntigravityConversationApi(RunStateProbe):
    """Queries Antigravity's documented local Connect HTTP+JSON `ConversationService`."""

    address: str
    csrf_token: str
    conversation_id: str

    REQUEST_TIMEOUT: ClassVar[float] = 3.0

    @classmethod
    def from_env(cls, conversation_id: str) -> Self | None:
        env_conv = os.environ.get("ANTIGRAVITY_CONVERSATION_ID", "").strip()
        address = os.environ.get("ANTIGRAVITY_LS_ADDRESS", "").strip()
        csrf_token = os.environ.get("ANTIGRAVITY_CSRF_TOKEN", "").strip()
        if not (address and csrf_token and conversation_id and env_conv == conversation_id):
            return None
        return cls(address=address, csrf_token=csrf_token, conversation_id=conversation_id)

    @property
    def endpoint(self) -> str:
        return f"http://{self.address}/product.v1.ConversationService/GetConversation"

    async def run_state(self, session: ClientSession) -> type[AntigravityRunState]:
        headers = {
            "Content-Type": "application/json",
            "Connect-Protocol-Version": "1",
            "x-antigravity-csrf-token": self.csrf_token,
        }
        try:
            async with session.post(
                self.endpoint,
                headers=headers,
                json={"conversationId": self.conversation_id},
                timeout=ClientTimeout(total=self.REQUEST_TIMEOUT),
            ) as response:
                if response.status != 200:
                    return UnknownRunState
                reply = ConversationReply.model_validate_json(await response.text())
                return reply.run_state()
        except (ClientError, OSError, TimeoutError, ValueError):
            return UnknownRunState


class PaneWakeTarget(ABC):
    """Interactive terminal pane hosting an Antigravity session."""

    @abstractmethod
    async def capture_tail(self) -> str: ...

    @abstractmethod
    async def poke(self, message: str) -> bool: ...

    @staticmethod
    def prompt_is_empty(pane_text: str) -> bool:
        """True only when Antigravity's `>` input line is visible and empty, so a wake
        never types over a human composing a prompt."""
        lines = pane_text.splitlines()
        for line in reversed(lines):
            stripped = line.strip()
            if stripped.startswith(">"):
                return stripped == ">"
        return False

    QUOTA_BANNER_RE: ClassVar[re.Pattern[str]] = re.compile(
        r"quota reached",
        re.IGNORECASE,
    )
    QUOTA_RESET_RE: ClassVar[re.Pattern[str]] = re.compile(
        r"Resets in\s+(?:(?P<hours>\d+)h)?\s*(?:(?P<minutes>\d+)m)?\s*(?:(?P<seconds>\d+)s)?",
        re.IGNORECASE,
    )
    QUOTA_BUFFER_SECONDS: ClassVar[float] = 15.0
    QUOTA_MIN_SECONDS: ClassVar[float] = 30.0
    QUOTA_DEFAULT_SECONDS: ClassVar[float] = 120.0
    QUOTA_MAX_SECONDS: ClassVar[float] = 3600.0

    @classmethod
    def quota_retry_info(cls, pane_text: str) -> tuple[float, str] | None:
        """If the pane's tail shows an Antigravity quota error banner above the `>` prompt,
        return `(wait_seconds, banner_signature)` so the supervisor waits once per distinct
        quota error (never looping forever on stale scrollback text); otherwise `None`."""
        lines = pane_text.splitlines()[-20:]
        recent = "\n".join(lines)
        if not cls.QUOTA_BANNER_RE.search(recent):
            return None
        signature = "\n".join(
            line.strip()
            for line in lines
            if cls.QUOTA_BANNER_RE.search(line)
            or cls.QUOTA_RESET_RE.search(line)
            or "Error ID:" in line
        )
        match = cls.QUOTA_RESET_RE.search(recent)
        if match is None:
            return cls.QUOTA_DEFAULT_SECONDS, signature
        hours = int(match.group("hours") or 0)
        minutes = int(match.group("minutes") or 0)
        seconds = int(match.group("seconds") or 0)
        total = hours * 3600 + minutes * 60 + seconds
        if total <= 0:
            return cls.QUOTA_DEFAULT_SECONDS, signature
        wait = max(cls.QUOTA_MIN_SECONDS, min(cls.QUOTA_MAX_SECONDS, total + cls.QUOTA_BUFFER_SECONDS))
        return wait, signature


@dataclass(frozen=True)
class TmuxPaneWake(PaneWakeTarget):
    """Captures and wakes the tmux pane this `agy` process runs inside."""

    pane_id: str

    COMMAND_TIMEOUT: ClassVar[float] = 3.0

    @classmethod
    def from_env(cls, conversation_id: str) -> Self | None:
        env_conv = os.environ.get("ANTIGRAVITY_CONVERSATION_ID", "").strip()
        tmux = os.environ.get("TMUX", "").strip()
        pane_id = os.environ.get("TMUX_PANE", "").strip()
        if not (tmux and pane_id and conversation_id and env_conv == conversation_id):
            return None
        return cls(pane_id=pane_id)

    async def capture_tail(self) -> str:
        try:
            proc = await asyncio.create_subprocess_exec(
                "tmux", "capture-pane", "-t", self.pane_id, "-p", "-S", "-25",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=self.COMMAND_TIMEOUT)
            return stdout.decode("utf-8", errors="replace") if proc.returncode == 0 else ""
        except (OSError, TimeoutError):
            return ""

    async def poke(self, message: str) -> bool:
        try:
            send_text = await asyncio.create_subprocess_exec(
                "tmux", "send-keys", "-t", self.pane_id, "-l", message,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(send_text.wait(), timeout=self.COMMAND_TIMEOUT)
            if send_text.returncode != 0:
                return False
            send_enter = await asyncio.create_subprocess_exec(
                "tmux", "send-keys", "-t", self.pane_id, "Enter",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(send_enter.wait(), timeout=self.COMMAND_TIMEOUT)
            return send_enter.returncode == 0
        except (OSError, TimeoutError):
            return False


@dataclass
class AntigravitySessionSupervisor:
    """Coordinates live run-state idle reporting and self-healing pane wakes for one `agy`
    inbox-watch process."""

    probe: RunStateProbe | None = None
    wake: PaneWakeTarget | None = None
    poll_interval: float = 2.0
    wake_grace: float = 5.0
    retry_backoff: tuple[float, ...] = (30.0, 60.0, 120.0, 300.0)
    prompt_busy_delay: float = 15.0

    _turn_possibly_open: bool = field(default=False, init=False)
    _was_running: bool = field(default=False, init=False)
    _pending_wake: bool = field(default=False, init=False)
    _wake_not_before: float = field(default=0.0, init=False)
    _wake_attempts: int = field(default=0, init=False)
    _wake_reason: str = field(default="", init=False)
    _quota_waited_signature: str = field(default="", init=False)
    _changed: asyncio.Event | None = field(default=None, init=False)

    @classmethod
    def inactive(cls) -> Self:
        return cls(probe=None, wake=None)

    @classmethod
    def from_env(cls, conversation_id: str) -> Self:
        return cls(
            probe=AntigravityConversationApi.from_env(conversation_id),
            wake=TmuxPaneWake.from_env(conversation_id),
        )

    @property
    def active(self) -> bool:
        return self.probe is not None

    def _signal(self) -> None:
        if self._changed is not None:
            self._changed.set()

    def note_window_ended(self) -> None:
        """Initial transcript replay finished: check live run state to retire any stale turn."""
        self._turn_possibly_open = True
        self._signal()

    def note_transcript_activity(self) -> None:
        """New transcript lines streamed: watch run state until the session settles idle."""
        self._turn_possibly_open = True
        self._signal()

    def note_delivery(self, line: str) -> None:
        """A bus message was pushed to this watch: arm fallback wake if the session stays idle."""
        summary = line.splitlines()[0][:140] if line else "Comms message arrived"
        self._pending_wake = True
        self._wake_reason = (
            f"Comms wakeup ({summary}): check comms_inbox, re-arm --wait-log, and respond."
        )
        now = time.monotonic()
        if self._wake_attempts == 0:
            self._wake_not_before = now + self.wake_grace
        else:
            self._wake_not_before = min(self._wake_not_before, now + self.wake_grace)
        self._signal()

    def _next_backoff(self) -> float:
        index = min(self._wake_attempts, len(self.retry_backoff) - 1)
        return self.retry_backoff[index]

    def _on_active(self) -> None:
        self._was_running = True
        self._pending_wake = False
        self._wake_attempts = 0

    async def _on_idle(self, socket: ClientWebSocketResponse) -> None:
        # Authoritatively idle: retire any open turn on the bus immediately.
        if self._turn_possibly_open or self._was_running:
            await socket.send_str(TurnIdle(printed=()).line())
            self._turn_possibly_open = False
            if self._was_running and self.wake is not None and not self._pending_wake:
                tail = await self.wake.capture_tail()
                quota_info = self.wake.quota_retry_info(tail)
                if quota_info is not None and quota_info[1] != self._quota_waited_signature:
                    quota_wait, signature = quota_info
                    self._quota_waited_signature = signature
                    self._pending_wake = True
                    self._wake_attempts = 0
                    self._wake_not_before = time.monotonic() + quota_wait
                    self._wake_reason = (
                        "Quota reset — check comms_inbox, re-arm --wait-log, and resume."
                    )
            self._was_running = False

        if self._pending_wake and self.wake is not None and time.monotonic() >= self._wake_not_before:
            tail = await self.wake.capture_tail()
            quota_info = self.wake.quota_retry_info(tail)
            if quota_info is not None and quota_info[1] != self._quota_waited_signature:
                quota_wait, signature = quota_info
                self._quota_waited_signature = signature
                self._wake_not_before = time.monotonic() + quota_wait
                self._wake_reason = (
                    "Quota reset — check comms_inbox, re-arm --wait-log, and resume."
                )
                return
            if not self.wake.prompt_is_empty(tail):
                self._wake_not_before = time.monotonic() + self.prompt_busy_delay
                return
            poked = await self.wake.poke(self._wake_reason)
            delay = self._next_backoff()
            if poked:
                self._wake_attempts += 1
            self._wake_not_before = time.monotonic() + delay

    async def _tick(self, session: ClientSession, socket: ClientWebSocketResponse) -> None:
        if self.probe is None:
            return
        state = await self.probe.run_state(session)
        await state.apply(self, socket)

    def _wait_seconds(self) -> float | None:
        if self._turn_possibly_open or self._was_running:
            return self.poll_interval
        if self._pending_wake and self.wake is not None:
            remaining = max(0.05, self._wake_not_before - time.monotonic())
            return min(remaining, max(self.poll_interval, 5.0))
        return None

    async def run(self, socket: ClientWebSocketResponse) -> None:
        if not self.active:
            return
        self._changed = asyncio.Event()
        # Localhost LS must bypass any HTTP proxy configured for outbound cloud sockets.
        async with ClientSession(trust_env=False) as http:
            while not socket.closed:
                await self._tick(http, socket)
                self._changed.clear()
                wait_for = self._wait_seconds()
                if wait_for is None:
                    await self._changed.wait()
                else:
                    try:
                        await asyncio.wait_for(self._changed.wait(), timeout=wait_for)
                    except TimeoutError:
                        pass
