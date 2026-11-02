"""Genuine in-turn subagents act through their resident session's bound parent."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from agent_comms_hooks import ClaudeSubagentHook, ClaudeTranscriptHook, SubagentId, ToolRefusedError

from .shared import Binding
from ..comms import Comms
from ..tool_caller import CommanderForkCaller
from ..turn_phase import ModelWaitPhase, TurnPhase


@dataclass(frozen=True)
class SubagentActivity(ClaudeSubagentHook):
    """The actual SDK subagent id owns its activity; parent fork provenance is not a rank."""

    def enter(self, comms: Comms, binding: Binding, phase: TurnPhase) -> None:
        comms.registry.snapshot().born(binding.owner.require_recorded().lookup)
        comms.agents.enter_phase(self.agent_id.fork, phase)


@dataclass(frozen=True)
class SubagentHook(SubagentActivity):
    """An SDK subagent retains its parent's transcript and original bound incarnation."""

    agent_type: str

    def register(self, comms: Comms, binding: Binding) -> None:
        actor = comms.registry.snapshot().born(binding.owner.require_recorded().lookup)
        fork = self.agent_id.fork
        CommanderForkCaller(fork).join(comms, actor.name)
        self.warn(f"Core registered you as in-turn fork {fork}: pass `--fork {fork}`"
                  " on every agent-comms-commander verb; the action keeps your parent authority"
                  " and records your own incarnation. Record what you found before stopping.")
        self.enter(comms, binding, ModelWaitPhase())


@dataclass(frozen=True)
class SubagentStopHook(SubagentHook):
    """The first empty stop is refused; an already refused stop ends the same actual fork."""

    stop_hook_active: bool

    def retire(self, comms: Comms, binding: Binding) -> None:
        comms.registry.snapshot().born(binding.owner.require_recorded().lookup)
        actor = CommanderForkCaller(self.agent_id.fork)
        if self.stop_hook_active:
            actor.end(comms)
            return
        try:
            actor.retire(comms)
        except ToolRefusedError as refused:
            self.output.line(json.dumps({"decision": "block", "reason": str(refused)}))


class SessionSubagents(ClaudeTranscriptHook):
    """Remaining SDK children retire through their existing caller/lifecycle owner."""

    def subagents(self) -> tuple[SubagentId, ...]:
        directory = Path(self.transcript_path).with_suffix("") / "subagents"
        return tuple(SubagentId(path.stem.removeprefix("agent-"))
                     for path in sorted(directory.glob("agent-*.jsonl")))

    def end_subagents(self, comms: Comms, binding: Binding) -> tuple[str, ...]:
        comms.registry.snapshot().born(binding.owner.require_recorded().lookup)
        try:
            CommanderForkCaller.end_session(
                comms, tuple(CommanderForkCaller(agent.fork) for agent in self.subagents()))
        except ToolRefusedError as refused:
            return (str(refused),)
        return ()
