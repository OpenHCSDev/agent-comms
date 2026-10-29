"""Claude subagents operate on their resident bridge's admitted bus and actor."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from agent_comms_hooks import ClaudeTranscriptHook, SubagentId, ToolRefusedError

from .session_watch import WatchClaim
from .shared import Binding
from ..comms import Comms
from ..store_files import AtomicFile
from ..thread_text import SubagentRole
from ..tool_caller import CommanderForkCaller, SubagentCaller
from ..turn_phase import ModelWaitPhase, TurnPhase


class SubagentSeats(ClaudeTranscriptHook):
    """One occupant-to-slot pairing per seat; registry/Caller owners perform the effects."""

    @property
    def seats(self) -> Path:
        return WatchClaim(self.session_id).path.with_suffix(".slots")

    def vacate(self, comms: Comms, seat: Path) -> tuple[str, ...]:
        try:
            SubagentCaller(seat.read_text()).end(comms)
        except ToolRefusedError as refused:
            return (str(refused),)
        seat.unlink()
        return ()


@dataclass(frozen=True)
class SeatedSubagent(SubagentSeats):
    """The declared role chooses the existing seat or commander-fork activity identity."""

    agent_id: SubagentId

    @property
    def seat(self) -> Path:
        return self.seats / self.agent_id

    def enter(self, comms: Comms, binding: Binding, phase: TurnPhase) -> None:
        selected = comms.registry.snapshot().commander_project.session_output(
            binding.owner, commander=lambda: self.agent_id.fork, commanded=self.seat.read_text)
        comms.agents.enter_phase(selected(), phase)


@dataclass(frozen=True)
class SubagentHook(SeatedSubagent):
    """Claude's subagent event retains its parent's transcript and actual binding."""

    agent_type: str

    def register(self, comms: Comms, binding: Binding) -> None:
        selected = comms.registry.snapshot().commander_project.session_output(
            binding.owner, commander=self.join, commanded=self.claim)
        selected(comms, binding)
        self.enter(comms, binding, ModelWaitPhase())

    def join(self, comms: Comms, binding: Binding) -> None:
        fork = self.agent_id.fork
        CommanderForkCaller(fork).commanding(comms, binding.owner.name)
        self.warn(f"Core registered you as commander fork {fork}: pass `--fork {fork}`"
                  " on every agent-comms-commander verb; your stop is refused until Core holds"
                  " a record of what you found.")

    def claim(self, comms: Comms, binding: Binding) -> None:
        slot = comms.registry.claim_slot(
            binding.owner.name, SubagentRole(self.agent_type), comms.goals).name
        self.seats.mkdir(parents=True, exist_ok=True, mode=0o700)
        AtomicFile(self.seat).write_text(slot)
        self.warn(f"Core seated you in slot {slot}, a child of {binding.owner.name}: messages to {slot}"
                  f" are for this role (`agent-comms inbox --name {slot}`), and its history is"
                  " what earlier occupants of the role did.")


@dataclass(frozen=True)
class SubagentStopHook(SubagentHook):
    """The first empty stop is refused; an already refused stop always ends its fork."""

    stop_hook_active: bool

    def retire(self, comms: Comms, binding: Binding) -> None:
        selected = comms.registry.snapshot().commander_project.session_output(
            binding.owner, commander=self.retire_fork, commanded=self.leave)
        selected(comms)

    def leave(self, comms: Comms) -> None:
        if not self.seat.exists():
            raise SystemExit(f"Subagent {self.agent_id} stopped without a seat: SubagentStart"
                             " never ran for it under this session's bus binding.")
        for refusal in self.vacate(comms, self.seat):
            raise SystemExit(refusal)

    def retire_fork(self, comms: Comms) -> None:
        actor = CommanderForkCaller(self.agent_id.fork)
        if self.stop_hook_active:
            actor.end(comms)
            return
        try:
            actor.retire(comms)
        except ToolRefusedError as refused:
            self.output.line(json.dumps({"decision": "block", "reason": str(refused)}))


class SessionSubagents(SubagentSeats):
    """A session's remaining children end through their existing lifecycle owners."""

    def subagents(self) -> tuple[SubagentId, ...]:
        directory = Path(self.transcript_path).with_suffix("") / "subagents"
        return tuple(SubagentId(path.stem.removeprefix("agent-"))
                     for path in sorted(directory.glob("agent-*.jsonl")))

    def end_subagents(self, comms: Comms, binding: Binding) -> tuple[str, ...]:
        selected = comms.registry.snapshot().commander_project.session_output(
            binding.owner, commander=self.end_forks, commanded=self.vacate_seats)
        return selected(comms)

    def vacate_seats(self, comms: Comms) -> tuple[str, ...]:
        seats = sorted(self.seats.glob("*")) if self.seats.is_dir() else []
        return tuple(refusal for seat in seats for refusal in self.vacate(comms, seat))

    def end_forks(self, comms: Comms) -> tuple[str, ...]:
        try:
            CommanderForkCaller.end_session(
                comms, tuple(CommanderForkCaller(agent.fork) for agent in self.subagents()))
        except ToolRefusedError as refused:
            return (str(refused),)
        return ()
