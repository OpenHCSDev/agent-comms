"""Declared hook events execute in the already resident Claude session owner."""
from __future__ import annotations

import asyncio
from abc import abstractmethod
from dataclasses import dataclass, field
from typing import ClassVar

from agent_comms_hooks.watch import HookAnswer, SessionHookProgram, SkillText
from pydantic import JsonValue

from agent_comms_hooks import Bash, ClaudeHook, ClaudeHookProgram, ClaudeToolHook, HookOutput, ToolRefusedError
from agent_comms_hooks.bounded_waits import BoundedWaits
from agent_comms_hooks.deferral import DeferralGuard
from agent_comms_hooks.forks import SubagentStartProgram, SubagentStopProgram
from agent_comms_hooks.queue_bypass import QueueBypassGuard
from agent_comms_hooks.read_gate import ReadGateProgram
from agent_comms_hooks.reread import ReminderProgram
from agent_comms_hooks.session import SessionEnded

from ..comms import Comms
from .hook_deferral import StopHook
from .hook_forks import SessionSubagents, SubagentHook, SubagentStopHook
from .hook_read_gate import GatedCall
from .hook_reread import ReminderCall
from agent_comms_hooks.skill import DeckStart, Prime
from ..cli_verbs.merge_review import ReviewedMerge
from .shared import Binding
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
class HookRequest[Event: ClaudeHook](SessionRequest[HookReply], SessionHookProgram, declared_name=None):
    """The generic event declaration owns decoding; the request class owns its effect."""

    payload: dict[str, JsonValue]
    reply_timeout: ClassVar[float] = HookAnswer.timeout

    @classmethod
    def installation_members(cls) -> tuple[type[ClaudeHookProgram], ...]:
        return ()

    @classmethod
    def event_type(cls) -> type[Event]:
        return cls.declared_argument(HookRequest)

    def decode_event(self, comms: Comms, binding: Binding, output: ResidentHookOutput) -> Event:
        """The request supplies non-wire context only where its event requires it."""
        return self.event_type().decode(self.payload, output=output)

    @abstractmethod
    def run_hook(self, event: Event, comms: Comms, binding: Binding) -> None: ...

    def completed(self, service: MonitorService) -> None:
        """Most hook events do not retire the session that answered them."""

    def evaluate(self, event: Event, comms: Comms, output: ResidentHookOutput,
                 binding: Binding) -> HookReply:
        try:
            self.run_hook(event, comms, binding)
        except ToolRefusedError as refused:
            return output.answer(stderr=f"{refused}\n", status=self.failure_status())
        return output.answer()

    async def handle(self, service: MonitorService, peer: MonitorPeer) -> SessionReply:
        output = ResidentHookOutput()
        event = await asyncio.to_thread(self.decode_event, service.bridge.comms,
                                        service.bridge.binding, output)
        if event.session_id != service.bridge.binding.thread or event.hook_event_name not in self.events:
            return SessionRefused(error="Hook event does not belong to this resident session/program")
        reply = await asyncio.to_thread(self.evaluate, event, service.bridge.comms, output,
                                        service.bridge.binding)
        self.completed(service)
        return reply


@dataclass(frozen=True)
class ReadGateRequest(ReadGateProgram, HookRequest[GatedCall], declared_name=ReadGateProgram.wire_name()):
    def decode_event(self, comms: Comms, binding: Binding, output: ResidentHookOutput) -> GatedCall:
        return self.event_type().of(self.payload, output=output, comms=comms, binding=binding)

    def run_hook(self, event: GatedCall, comms: Comms, binding: Binding) -> None:
        event.judge()


@dataclass(frozen=True)
class ReminderRequest(ReminderProgram, HookRequest[ReminderCall], declared_name=ReminderProgram.wire_name()):
    def decode_event(self, comms: Comms, binding: Binding, output: ResidentHookOutput) -> ReminderCall:
        return self.event_type().of(self.payload, output=output, comms=comms, binding=binding)

    def run_hook(self, event: ReminderCall, comms: Comms, binding: Binding) -> None:
        event.warn(*event.take(comms, binding, event.completed()))


@dataclass(frozen=True)
class SkillRequest(SkillText, HookRequest[DeckStart], declared_name=SkillText.wire_name()):
    def decode_event(self, comms: Comms, binding: Binding, output: ResidentHookOutput) -> DeckStart:
        return DeckStart.of(self.payload, output=output)

    def run_hook(self, event: DeckStart, comms: Comms, binding: Binding) -> None:
        event.warn(*event.take(comms, binding))


@dataclass(frozen=True)
class PrimeRequest(HookRequest[DeckStart], declared_name="prime"):
    """The explicit /prime command uses the same resident transport as startup."""

    prime: type[Prime]
    events = ("Prime",)

    def run_hook(self, event: DeckStart, comms: Comms, binding: Binding) -> None:
        event.warn(*event.primed(self.prime, comms, binding))


@dataclass(frozen=True)
class StopRequest(DeferralGuard, HookRequest[StopHook], declared_name=DeferralGuard.wire_name()):
    def decode_event(self, comms: Comms, binding: Binding, output: ResidentHookOutput) -> StopHook:
        return self.event_type().decode(self.payload, output=output, comms=comms, binding=binding)

    def run_hook(self, event: StopHook, comms: Comms, binding: Binding) -> None:
        event.guard()


@dataclass(frozen=True)
class SubagentStartRequest(SubagentStartProgram, HookRequest[SubagentHook], declared_name=SubagentStartProgram.wire_name()):
    def run_hook(self, event: SubagentHook, comms: Comms, binding: Binding) -> None:
        event.register(comms, binding)


@dataclass(frozen=True)
class SubagentStopRequest(SubagentStopProgram, HookRequest[SubagentStopHook], declared_name=SubagentStopProgram.wire_name()):
    def run_hook(self, event: SubagentStopHook, comms: Comms, binding: Binding) -> None:
        event.retire(comms, binding)


@dataclass(frozen=True)
class SessionEndRequest(SessionEnded, HookRequest[SessionSubagents], declared_name=SessionEnded.wire_name()):
    def run_hook(self, event: SessionSubagents, comms: Comms, binding: Binding) -> None:
        for refusal in event.end_subagents(comms, binding):
            raise ToolRefusedError(refusal)

    def completed(self, service: MonitorService) -> None:
        SessionReady().end_session(service)


@dataclass(frozen=True)
class BoundedWaitRequest(BoundedWaits, HookRequest[ClaudeToolHook], declared_name=BoundedWaits.wire_name()):
    def run_hook(self, event: ClaudeToolHook, comms: Comms, binding: Binding) -> None:
        BoundedWaits.judge(event)


@dataclass(frozen=True)
class QueueBypassRequest(ReviewedMerge, QueueBypassGuard, HookRequest[ClaudeToolHook],
                         declared_name=QueueBypassGuard.wire_name()):
    def run_hook(self, event: ClaudeToolHook, comms: Comms, binding: Binding) -> None:
        if not self.binds(event):
            return
        reasons: tuple[str, ...] = ()
        try:
            bypasses = self.bypasses(Bash.decode(event.tool_input).command)
            if bypasses:
                self.reviewed_merges(comms, comms.registry.snapshot(),
                                     binding.owner.require_recorded(), bypasses)
        except ToolRefusedError as refused:
            reasons = (str(refused),)
        self.refuse(event, reasons)
