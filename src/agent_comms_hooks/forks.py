"""A Claude subagent of a bus-bound session is that session's child thread from its first instant.

Claude Code names each subagent by `agent_id` on its start and stop, and keeps that id when
the subagent is resumed; `agent_type` names its role. The session's role in the registry picks
the child: the commander's subagent is a commander fork, registered and retired by Core's own
commander verbs; any other bound session's subagent takes the first vacant slot of its role,
a durable thread whose bus history the next occupant inherits. An unbound session has no bus
to put a child on. The hook only relays to Core, and Core's refusals refuse the event.

Core's refusal to retire a fork that recorded nothing sends its first stop back; the stop
Claude Code makes after that send-back ends the fork, and Core archives it and reports the
missing record to its commander. A slot is vacated at its occupant's stop with no send-back.
SubagentStop never comes for a subagent killed mid-turn or cut off by its session's end, so
the session's end ends every child Core still holds for it.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from agent_comms.cli_commands import RegisteredResult
from agent_comms.comms import client_wire
from agent_comms.commander.order import EndVerb, OrderVerb, RetireVerb, SayVerb, SessionEndVerb
from agent_comms.field_codec import FieldCodec
from agent_comms.store_files import AtomicFile
from agent_comms.tool_caller import CommanderForkCaller, ThreadCaller
from agent_comms.turn_phase import ModelWaitPhase
from agent_comms_hooks import ClaudeHookProgram, SubagentId
from agent_comms_hooks.seats import SeatedSubagent, SubagentSeats
from agent_comms_hooks.watch import WatchClaim, WatchRecord


@dataclass(frozen=True)
class SubagentHook(SeatedSubagent):
    """What Claude Code adds for a subagent event; `transcript_path` stays the parent's."""

    agent_type: str

    def commanding(self, record: WatchRecord,
                   verb: type[OrderVerb]) -> subprocess.CompletedProcess[str]:
        with client_wire(Path(record.root)) as comms:
            command = CommanderForkCaller(self.agent_id.fork).commander_command(
                comms, *verb.command_arguments())
        return self.run(*shlex.split(command))

    def register(self) -> None:
        state = WatchClaim(self.session_id).state()
        state.by_role(lambda: None, self.claim, self.join)
        self.enter(ModelWaitPhase())

    def join(self, record: WatchRecord) -> None:
        """Use the hook's client and captured parent; no commander CLI subprocess."""
        fork = self.agent_id.fork
        caller = CommanderForkCaller(fork)
        with client_wire(Path(record.root)) as comms:
            caller.join(comms, record.bus)
            arguments = shlex.join(caller.commander_arguments(comms))
        self.warn(f"Core registered you as commander fork {fork}: pass `{arguments}`"
                  " on every agent-comms-commander verb; your stop is refused until Core holds"
                  " a record of what you found.")

    def claim(self, record: WatchRecord) -> None:
        run = self.core(record, "claim-slot", "--parent", record.bus, "--role", self.agent_type)
        if run.returncode:
            raise SystemExit(run.stdout.strip())
        slot = FieldCodec.decode(RegisteredResult, json.loads(run.stdout)).registered
        self.seats.mkdir(parents=True, exist_ok=True, mode=0o700)
        AtomicFile(self.seat).write_text(slot)
        self.warn(f"Core seated you in slot {slot}, a child of {record.bus}: messages to {slot}"
                  f" are for this role (`agent-comms inbox --name {slot}`), and its history is"
                  " what earlier occupants of the role did.")


@dataclass(frozen=True)
class SubagentStopHook(SubagentHook):
    """A subagent's stop; `stop_hook_active` says this hook already sent this stop back once."""

    stop_hook_active: bool

    def retire(self) -> None:
        state = WatchClaim(self.session_id).state()
        state.by_role(lambda: None, self.leave, self.retire_fork)

    def leave(self, record: WatchRecord) -> None:
        if not self.seat.exists():
            raise SystemExit(f"Subagent {self.agent_id} stopped without a seat: SubagentStart"
                             " never ran for it under this session's bus binding.")
        for refusal in self.vacate(record, self.seat):
            raise SystemExit(refusal)

    def retire_fork(self, record: WatchRecord) -> None:
        if self.stop_hook_active:
            self.end(record)
            return
        run = self.commanding(record, RetireVerb)
        if run.returncode:
            with client_wire(Path(record.root)) as comms:
                command = CommanderForkCaller(self.agent_id.fork).commander_command(
                    comms, *SayVerb.command_arguments('#comms', 'FILE'))
            reason = run.stderr.strip() or f"exit status {run.returncode}"
            print(json.dumps({"decision": "block",
                              "reason": f"{reason}\nRecord a head verdict or post what you found"
                                        f" with `{command}` before stopping."}))

    def end(self, record: WatchRecord) -> None:
        """A send-back is never repeated: the fork ends here whatever it recorded."""
        run = self.commanding(record, EndVerb)
        if run.returncode:
            raise SystemExit(run.stderr.strip() or f"exit status {run.returncode}")


class SessionSubagents(SubagentSeats):
    """A session's subagents end with it, whether or not each one stopped on its own."""

    def subagents(self) -> tuple[SubagentId, ...]:
        """Claude Code keeps each subagent's transcript beside the session's own."""
        return tuple(SubagentId(path.stem.removeprefix("agent-"))
                     for path in sorted(self.subagent_transcripts.glob("agent-*.jsonl")))

    def end_subagents(self) -> tuple[str, ...]:
        """Core's refusals to end this session's children, or none."""
        state = WatchClaim(self.session_id).state()
        return state.by_role(lambda: (), self.vacate_seats, self.end_forks)

    def vacate_seats(self, record: WatchRecord) -> tuple[str, ...]:
        """Every slot an occupant still holds: one with no stop left it seated."""
        seats = sorted(self.seats.glob("*")) if self.seats.is_dir() else []
        return tuple(refusal for seat in seats for refusal in self.vacate(record, seat))

    def end_forks(self, record: WatchRecord) -> tuple[str, ...]:
        """Core's one run for this session's forks, or none for a session with none."""
        subagents = self.subagents()
        if not subagents:
            return ()
        with client_wire(Path(record.root)) as comms:
            command = ThreadCaller(record.bus).commander_command(
                comms, *SessionEndVerb.command_arguments('--forks',
                                                       *(agent.fork for agent in subagents)))
        run = self.run(*shlex.split(command))
        return (run.stderr.strip() or f"exit status {run.returncode}",) if run.returncode else ()


class SubagentStartProgram(ClaudeHookProgram):
    events = ("SubagentStart",)

    @classmethod
    def command(cls) -> str:
        return cls.console_script(f"{started.__module__}:{started.__name__}")


class SubagentStopProgram(ClaudeHookProgram):
    events = ("SubagentStop",)

    @classmethod
    def command(cls) -> str:
        return cls.console_script(f"{stopped.__module__}:{stopped.__name__}")


def started() -> None:
    SubagentHook.read().register()


def stopped() -> None:
    SubagentStopHook.read().retire()
