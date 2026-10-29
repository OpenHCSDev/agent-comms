"""Resident Claude forks hook behavior, behind the existing session socket."""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from agent_comms_hooks import SubagentId

from .session_watch import (
    CommanderSession,
    WatchClaim,
    WatchRecord,
)
from ..cli_result import RegisteredResult
from ..comms import client_wire
from ..field_codec import FieldCodec
from ..store_files import AtomicFile
from ..turn_phase import (
    ModelWaitPhase,
    TurnPhase,
)


class SubagentSeats(CommanderSession):
    """The slots this session's subagents occupy, one file per occupant naming its slot:
    Claude Code names the subagent and Core names the slot, so the pairing is written only
    here, when Core seats an occupant, and removed when Core vacates it."""

    command: ClassVar[tuple[str, ...]] = ("agent-comms-commander", "order")
    timeout: ClassVar[float] = 30

    @property
    def seats(self) -> Path:
        return WatchClaim(self.session_id).path.with_suffix(".slots")

    def run(self, *argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(argv, capture_output=True, text=True, timeout=self.timeout)

    def core(self, record: WatchRecord, *argv: str) -> subprocess.CompletedProcess[str]:
        """Core on this session's bus; Core writes its answer, or its refusal, to stdout."""
        return self.run(sys.executable, "-m", "agent_comms.cli", "--root", record.root, *argv)

    def vacate(self, record: WatchRecord, seat: Path) -> tuple[str, ...]:
        """Core's refusal to vacate the seat's slot, or none once it is vacated."""
        run = self.core(record, "retire-slot", "--name", seat.read_text())
        if run.returncode:
            return (run.stdout.strip(),)
        seat.unlink()
        return ()


@dataclass(frozen=True)
class SeatedSubagent(SubagentSeats):
    """The existing seat or commander-fork name identifies whose activity a hook reports."""

    agent_id: SubagentId

    @property
    def seat(self) -> Path:
        return self.seats / self.agent_id

    def enter(self, phase: TurnPhase) -> None:
        state = WatchClaim(self.session_id).state()
        state.by_role(
            lambda: None,
            lambda record: self.record_phase(record, self.seat.read_text(), phase),
            lambda record: self.record_phase(record, self.agent_id.fork, phase),
        )

    @staticmethod
    def record_phase(record: WatchRecord, name: str, phase: TurnPhase) -> None:
        with client_wire(Path(record.root)) as comms:
            comms.agents.enter_phase(name, phase)


@dataclass(frozen=True)
class SubagentHook(SeatedSubagent):
    """What Claude Code adds for a subagent event; `transcript_path` stays the parent's."""

    agent_type: str

    def commanding(self, verb: str) -> subprocess.CompletedProcess[str]:
        return self.run(*self.command, verb, "--fork", self.agent_id.fork)

    def register(self) -> None:
        state = WatchClaim(self.session_id).state()
        state.by_role(lambda: None, self.claim, self.join)
        self.enter(ModelWaitPhase())

    def join(self, record: WatchRecord) -> None:
        run = self.commanding("join")
        if run.returncode:
            raise SystemExit(run.stderr.strip() or f"exit status {run.returncode}")
        fork = self.agent_id.fork
        self.warn(f"Core registered you as commander fork {fork}: pass `--fork {fork}`"
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
            self.end()
            return
        run = self.commanding("retire")
        if run.returncode:
            self.output.line(json.dumps({"decision": "block",
                              "reason": run.stderr.strip() or f"exit status {run.returncode}"}))

    def end(self) -> None:
        """A send-back is never repeated: the fork ends here whatever it recorded."""
        run = self.commanding("end")
        if run.returncode:
            raise SystemExit(run.stderr.strip() or f"exit status {run.returncode}")


class SessionSubagents(SubagentSeats):
    """A session's subagents end with it, whether or not each one stopped on its own."""

    def subagents(self) -> tuple[SubagentId, ...]:
        """Claude Code keeps each subagent's transcript beside the session's own."""
        directory = Path(self.transcript_path).with_suffix("") / "subagents"
        return tuple(SubagentId(path.stem.removeprefix("agent-"))
                     for path in sorted(directory.glob("agent-*.jsonl")))

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
        run = self.run(*self.command, "session-end", "--forks",
                       *(agent.fork for agent in subagents))
        return (run.stderr.strip() or f"exit status {run.returncode}",) if run.returncode else ()
