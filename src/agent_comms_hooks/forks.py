"""A Claude subagent of the commander is a commander fork in Core from its first instant.

Claude Code names each subagent by `agent_id` on its start and stop, and keeps that id when
the subagent is resumed. Core's own commander verbs register and retire the fork; the hook
only relays them, and Core's refusal to retire a fork that recorded nothing refuses the stop.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import ClassVar

from agent_comms_hooks import ClaudeHookProgram, ClaudeTranscriptHook


@dataclass(frozen=True)
class SubagentHook(ClaudeTranscriptHook):
    """What Claude Code adds for a subagent event; `transcript_path` stays the parent's."""

    agent_id: str
    command: ClassVar[tuple[str, ...]] = ("agent-comms-commander", "order")
    timeout: ClassVar[float] = 30

    @property
    def fork(self) -> str:
        return f"claude-{self.agent_id}"

    def commanding(self, verb: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run((*self.command, verb, "--fork", self.fork),
                              capture_output=True, text=True, timeout=self.timeout)

    def register(self) -> None:
        """A subagent of any other session is not the commander's fork."""
        if not self.parts.exists():
            return
        run = self.commanding("join")
        if run.returncode:
            raise SystemExit(run.stderr.strip() or f"exit status {run.returncode}")
        self.warn(f"Core registered you as commander fork {self.fork}: pass `--fork {self.fork}`"
                  " on every agent-comms-commander verb; your stop is refused until Core holds"
                  " a record of what you found.")

    def retire(self) -> None:
        if not self.parts.exists():
            return
        run = self.commanding("retire")
        if run.returncode:
            print(json.dumps({"decision": "block",
                              "reason": run.stderr.strip() or f"exit status {run.returncode}"}))


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
