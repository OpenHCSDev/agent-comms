"""Genuine in-turn subagents act through their resident session's bound parent."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from agent_comms.cli_bridges.shared import Binding
from agent_comms.comms import Comms
from agent_comms.execution.turn_phase import ModelWaitPhase, TurnPhase
from agent_comms.registry.threads import Thread
from agent_comms.transport.tool_caller import ForkCaller, ForkRecordOwed
from agent_comms_hooks import ClaudeSubagentHook, ClaudeTranscriptHook, SubagentId, ToolRefusedError


@dataclass(frozen=True)
class SubagentActivity(ClaudeSubagentHook):
    """The actual SDK subagent id owns its activity; parent fork provenance is not a rank."""

    def actor(self, comms: Comms, binding: Binding) -> Thread:
        """The bound parent incarnation every event of this subagent acts under."""
        return comms.registry.snapshot().born(binding.owner.require_recorded().lookup)

    def enter(self, comms: Comms, binding: Binding, phase: TurnPhase) -> None:
        """Claude Code does not raise SubagentStart for every agent type (a web-fetch agent's
        first event is its first tool call), so the fork is declared on whichever of its events
        arrives first, under the same parent the start event would have used."""
        actor = self.actor(comms, binding)
        if self.agent_id.fork not in comms.registry:
            ForkCaller(self.agent_id.fork).join(comms, actor.name)
        comms.agents.enter_phase(self.agent_id.fork, phase)


@dataclass(frozen=True)
class SubagentHook(SubagentActivity):
    """An SDK subagent retains its parent's transcript and original bound incarnation."""

    agent_type: str

    def register(self, comms: Comms, binding: Binding) -> None:
        fork = self.agent_id.fork
        ForkCaller(fork).join(comms, self.actor(comms, binding).name)
        self.warn(f"Core registered you as in-turn fork {fork}: pass `--fork {fork}`"
                  " on every agent-comms-team verb; the action keeps your parent authority"
                  " and records your own incarnation. Record what you found before stopping.")
        self.enter(comms, binding, ModelWaitPhase())


@dataclass(frozen=True)
class SubagentStopHook(SubagentHook):
    """The first empty stop is refused; an already refused stop ends the same actual fork."""

    stop_hook_active: bool

    def retire(self, comms: Comms, binding: Binding) -> None:
        comms.registry.snapshot().born(binding.owner.require_recorded().lookup)
        actor = ForkCaller(self.agent_id.fork)
        if self.stop_hook_active:
            actor.end(comms)
            return
        try:
            actor.retire(comms)
        except ForkRecordOwed as refused:
            self.output.line(json.dumps({"decision": "block", "reason": str(refused)}))


class SessionSubagents(ClaudeTranscriptHook):
    """Remaining SDK children retire through their existing caller/lifecycle owner."""

    def subagents(self) -> tuple[SubagentId, ...]:
        directory = Path(self.transcript_path).with_suffix("") / "subagents"
        return tuple(SubagentId.of_transcript(path)
                     for path in sorted(directory.glob("agent-*.jsonl")))

    def end_subagents(self, comms: Comms, binding: Binding) -> tuple[str, ...]:
        comms.registry.snapshot().born(binding.owner.require_recorded().lookup)
        try:
            ForkCaller.end_session(
                comms, tuple(ForkCaller(agent.fork) for agent in self.subagents()))
        except ToolRefusedError as refused:
            return (str(refused),)
        return ()
