"""The read gate and the reminder Claude Code runs on every tool call, as clients of the
session bridge that decides them.

Each call used to import Core in its own process: about 1.6 s for the gate and 4.2 s for the
reminder per call on 2026-10-30, with six agents calling tools every few seconds. The bridge
(`agent-comms-claude-bridge serve`, one per bound session) already holds Core open and serves
this session's socket, so the hook sends it Claude Code's record and prints the answer it
returns (agent_comms.cli_bridges.monitor.ToolCallHook, agent_comms_hooks.reread.ToolHookEvent).
This module imports the standard library only.

A session with no bridge listening has nothing to decide it: an unbound session has no deal,
board or deck. A bound session whose bridge is down or mid-swap is told so after the call, so
the gate and the reminder being off is never silent.
"""

from __future__ import annotations

import json
import socket
import sys
from pathlib import Path
from typing import ClassVar

from agent_comms_hooks import ClaudeHookProgram
from agent_comms_hooks.board import SessionEndpoint, SessionWire


class BridgedToolCall(SessionWire):
    """One tool-call hook record sent to the session bridge, and the answer it prints."""

    request: ClassVar[str] = "tool-call"
    timeout: ClassVar[float] = 10
    absent: ClassVar[str] = (
        "No session bridge answers at {socket}: the read gate, the reminder deck and the board"
        " are off until it runs again. Re-arm the session's Monitor watch.")

    def __init__(self, line: str) -> None:
        self.line = line
        record = json.loads(line)
        self.event = record["hook_event_name"]
        self.endpoint = SessionEndpoint(record["session_id"])

    def forward(self) -> None:
        try:
            reply = self.asked(self.endpoint.socket_path)
        except (FileNotFoundError, ConnectionRefusedError):
            self.unanswered()
            return
        except (OSError, json.JSONDecodeError) as error:
            raise SystemExit(f"The session bridge did not answer this {self.event}: {error}") from error
        if reply.pop(self.family_discriminator) != self.request:
            raise SystemExit(f"The session bridge refused this {self.event}: {reply}")
        sys.stdout.write(reply["output"])

    def asked(self, endpoint: Path) -> dict[str, str]:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
            peer.settimeout(self.timeout)
            peer.connect(str(endpoint))
            request = {self.family_discriminator: self.request, "record": self.line}
            peer.sendall(json.dumps(request).encode() + b"\n")
            with peer.makefile("rb") as stream:
                return json.loads(stream.readline())

    def unanswered(self) -> None:
        """Only a bound session is told: its record outlives its bridge."""
        if self.event == "PostToolUse" and self.endpoint.record_path.exists():
            context = self.absent.format(socket=self.endpoint.socket_path)
            print(json.dumps({"hookSpecificOutput": {"hookEventName": self.event,
                                                     "additionalContext": context}}))


class ReadGateProgram(ClaudeHookProgram):
    events = ("PreToolUse",)

    @classmethod
    def command(cls) -> str:
        return cls.console_script(f"{read_gate.__module__}:{read_gate.__name__}")


class ReminderProgram(ClaudeHookProgram):
    events = ("PostToolUse",)

    @classmethod
    def command(cls) -> str:
        return cls.console_script(f"{reread.__module__}:{reread.__name__}")


def read_gate() -> None:
    BridgedToolCall(sys.stdin.read()).forward()


def reread() -> None:
    BridgedToolCall(sys.stdin.read()).forward()
