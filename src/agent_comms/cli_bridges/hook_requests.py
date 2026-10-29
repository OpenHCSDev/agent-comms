"""Declared hook events execute in the already resident Claude session owner."""
from __future__ import annotations

import asyncio
import subprocess
import sys
import traceback
from importlib.metadata import entry_points
from abc import abstractmethod
from dataclasses import dataclass, field
from typing import ClassVar, cast

from agent_comms_hooks.watch import HookAnswer
from pydantic import JsonValue

from agent_comms_hooks import Bash, ClaudeHook, ClaudeHookProgram, ClaudeToolHook, HookOutput, HookRefused
from agent_comms_hooks.bounded_waits import BoundedWaits
from agent_comms_hooks.deferral import DeferralGuard
from agent_comms_hooks.forks import SubagentStartProgram, SubagentStopProgram
from agent_comms_hooks.queue_bypass import QueueBypassGuard
from agent_comms_hooks.read_gate import ReadGateProgram
from agent_comms_hooks.reread import ReminderProgram
from agent_comms_hooks.session import SessionEnded
from agent_comms_hooks.skill import SkillText

from ..comms import Comms
from .hook_deferral import StopHook
from .hook_forks import SessionSubagents, SubagentHook, SubagentStopHook
from .hook_read_gate import GatedCall
from .hook_reread import BoardReminder
from .hook_skill import DeckStart
from .session_watch import WatchClaim
from .monitor import MonitorPeer, MonitorService, SessionReady, SessionRefused, SessionReply, SessionRequest


@dataclass
class ResidentHookOutput(HookOutput):
    """Each request owns its explicit answer; no global stdout redirection."""

    lines: list[str] = field(default_factory=list)

    def line(self, text: str) -> None:
        self.lines.append(text + "\n")

    def answer(self, *, stderr: str = "", status: int = 0) -> HookReply:
        return HookReply(stdout="".join(self.lines), stderr=stderr, status=status)


@dataclass(frozen=True)
class HookReply(HookAnswer, SessionReply, declared_name=HookAnswer.request):
    """The stdlib client consumes the same declared result Core emits."""


@dataclass(frozen=True)
class HookRequest[Event: ClaudeHook](SessionRequest[HookReply], ClaudeHookProgram, declared_name=None):
    """The generic event declaration owns decoding; the request class owns its effect."""

    payload: dict[str, JsonValue]
    reply_timeout: ClassVar[float] = HookAnswer.timeout

    @classmethod
    def installation_members(cls) -> tuple[type[ClaudeHookProgram], ...]:
        return ()

    @classmethod
    def event_type(cls) -> type[Event]:
        # The only cast is this Python generic-metadata boundary, before an event exists.
        return cast(type[Event], cls.declared_argument(HookRequest))

    @abstractmethod
    def run_hook(self, event: Event, comms: Comms) -> None: ...

    def completed(self, service: MonitorService) -> None:
        """Most hook events do not retire the session that answered them."""

    def evaluate(self, event: Event, comms: Comms, output: ResidentHookOutput) -> HookReply:
        try:
            self.run_hook(event, comms)
        except SystemExit as stopped:
            if stopped.code is None or stopped.code == 0:
                return output.answer()
            if isinstance(stopped.code, int):
                return output.answer(status=self.failure_status(stopped.code))
            return output.answer(stderr=f"{stopped.code}\n", status=self.failure_status())
        except Exception:
            # A program failure is a failed hook result, never success or death of the serving owner.
            return output.answer(stderr=traceback.format_exc(), status=self.failure_status())
        return output.answer()

    async def handle(self, service: MonitorService, peer: MonitorPeer) -> SessionReply:
        output = ResidentHookOutput()
        try:
            event = cast(Event, await asyncio.to_thread(self.event_type().of, self.payload, output=output))
        except Exception:
            return SessionRefused(error=traceback.format_exc())
        if event.session_id != service.bridge.binding.thread or event.hook_event_name not in self.events:
            return SessionRefused(error="Hook event does not belong to this resident session/program")
        reply = await asyncio.to_thread(self.evaluate, event, service.bridge.comms, output)
        self.completed(service)
        return reply


@dataclass(frozen=True)
class ReadGateRequest(ReadGateProgram, HookRequest[GatedCall], declared_name=ReadGateProgram.command()):
    def run_hook(self, event: GatedCall, comms: Comms) -> None:
        event.judge(comms)


@dataclass(frozen=True)
class ReminderRequest(ReminderProgram, HookRequest[BoardReminder], declared_name=ReminderProgram.command()):
    def run_hook(self, event: BoardReminder, comms: Comms) -> None:
        number = GatedCall.of(self.payload, output=event.output).completed(comms)
        event.warn(*event.take(number))


@dataclass(frozen=True)
class SkillRequest(SkillText, HookRequest[DeckStart], declared_name=SkillText.command()):
    def run_hook(self, event: DeckStart, comms: Comms) -> None:
        event.warn(*event.take())


@dataclass(frozen=True)
class StopRequest(DeferralGuard, HookRequest[StopHook], declared_name=DeferralGuard.command()):
    def run_hook(self, event: StopHook, comms: Comms) -> None:
        event.guard()


@dataclass(frozen=True)
class SubagentStartRequest(SubagentStartProgram, HookRequest[SubagentHook], declared_name=SubagentStartProgram.command()):
    def run_hook(self, event: SubagentHook, comms: Comms) -> None:
        event.register()


@dataclass(frozen=True)
class SubagentStopRequest(SubagentStopProgram, HookRequest[SubagentStopHook], declared_name=SubagentStopProgram.command()):
    def run_hook(self, event: SubagentStopHook, comms: Comms) -> None:
        event.retire()


@dataclass(frozen=True)
class SessionEndRequest(SessionEnded, HookRequest[SessionSubagents], declared_name=SessionEnded.command()):
    def run_hook(self, event: SessionSubagents, comms: Comms) -> None:
        for refusal in event.end_subagents():
            raise SystemExit(refusal)

    def completed(self, service: MonitorService) -> None:
        SessionReady().end_session(service)


@dataclass(frozen=True)
class BoundedWaitRequest(BoundedWaits, HookRequest[ClaudeToolHook], declared_name=BoundedWaits.command()):
    def run_hook(self, event: ClaudeToolHook, comms: Comms) -> None:
        BoundedWaits.judge(event)


@dataclass(frozen=True)
class QueueBypassRequest(QueueBypassGuard, HookRequest[ClaudeToolHook], declared_name=QueueBypassGuard.command()):
    def run_hook(self, event: ClaudeToolHook, comms: Comms) -> None:
        self.judge(event)

    @classmethod
    def reasons_for(cls, hook: ClaudeToolHook) -> tuple[str, ...]:
        command = Bash.decode(hook.tool_input).command
        reasons = cls.reasons(command)
        state = WatchClaim(hook.session_id).state()
        if not reasons or not state.commands():
            return reasons
        (entry,) = entry_points(group="console_scripts", name="agent-comms")
        for binding in state.bindings():
            try:
                checked = subprocess.run(
                    (sys.executable, "-m", entry.module, "--root", binding.root,
                     "merge-review", "--session", hook.session_id, "--bash", command),
                    capture_output=True, text=True, timeout=60,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise HookRefused(f"Core's merge-verdict reader failed: {error}") from error
            if checked.returncode:
                return (checked.stdout.strip(),)
        return ()
