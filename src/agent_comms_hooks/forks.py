"""A Claude subagent of a bus-bound session is that session's child thread from its first instant.

Claude Code names each subagent by `agent_id` on its start and stop, and keeps that id when
the subagent is resumed; `agent_type` names its role. The session's role in the registry picks
the child: the commander's subagent is a commander fork, registered and retired by Core's own
commander verbs; any other bound session's subagent takes the first vacant slot of its role,
a durable thread whose bus history the next occupant inherits. An unbound session has no bus
to put a child on. The hook only relays to Core, and Core's refusals refuse the event.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from agent_comms.store_files import AtomicFile
from agent_comms_hooks import ClaudeHookProgram, SubagentId
from agent_comms_hooks.watch import CommanderSession, WatchClaim, WatchRecord


@dataclass(frozen=True)
class SubagentHook(CommanderSession):
    """What Claude Code adds for a subagent event; `transcript_path` stays the parent's."""

    agent_id: SubagentId
    agent_type: str
    command: ClassVar[tuple[str, ...]] = ("agent-comms-commander", "order")
    timeout: ClassVar[float] = 30

    @property
    def seat(self) -> Path:
        """The slot this subagent occupies: written only here, when Core seats it, and read
        when it stops, since Claude Code names the subagent and Core names the slot."""
        return WatchClaim(self.session_id).path.with_suffix(".slots") / self.agent_id

    def run(self, *argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(argv, capture_output=True, text=True, timeout=self.timeout)

    def commanding(self, verb: str) -> subprocess.CompletedProcess[str]:
        return self.run(*self.command, verb, "--fork", self.agent_id.fork)

    def core(self, record: WatchRecord, *argv: str) -> dict[str, str]:
        """Core's answer on this session's bus; Core writes a refusal as its JSON `error`."""
        run = self.run(sys.executable, "-m", "agent_comms.cli", "--root", record.root, *argv)
        answer = json.loads(run.stdout)
        if run.returncode:
            raise SystemExit(answer["error"])
        return answer

    def register(self) -> None:
        state = WatchClaim(self.session_id).state()
        state.by_role(lambda: None, lambda: self.claim(*state.bindings()), self.join)

    def retire(self) -> None:
        state = WatchClaim(self.session_id).state()
        state.by_role(lambda: None, lambda: self.vacate(*state.bindings()), self.leave)

    def join(self) -> None:
        run = self.commanding("join")
        if run.returncode:
            raise SystemExit(run.stderr.strip() or f"exit status {run.returncode}")
        fork = self.agent_id.fork
        self.warn(f"Core registered you as commander fork {fork}: pass `--fork {fork}`"
                  " on every agent-comms-commander verb; your stop is refused until Core holds"
                  " a record of what you found.")

    def leave(self) -> None:
        run = self.commanding("retire")
        if run.returncode:
            print(json.dumps({"decision": "block",
                              "reason": run.stderr.strip() or f"exit status {run.returncode}"}))

    def claim(self, record: WatchRecord) -> None:
        slot = self.core(record, "claim-slot", "--parent", record.bus, "--role", self.agent_type)["registered"]
        self.seat.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        AtomicFile(self.seat).write_text(slot)
        self.warn(f"Core seated you in slot {slot}, a child of {record.bus}: messages to {slot}"
                  f" are for this role (`agent-comms inbox --name {slot}`), and its history is"
                  " what earlier occupants of the role did.")

    def vacate(self, record: WatchRecord) -> None:
        if not self.seat.exists():
            raise SystemExit(f"Subagent {self.agent_id} stopped without a seat: SubagentStart"
                             " never ran for it under this session's bus binding.")
        self.core(record, "retire-slot", "--name", self.seat.read_text())
        self.seat.unlink()


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
    SubagentHook.read().retire()
