"""Claude session hooks start/stop the configured bridge, never a Monitor reader."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from agent_comms.child_process import ProcessIdentity
from agent_comms.cli_bridges.monitor import EndSession
from agent_comms.cli_bridges.shared import Serve
from agent_comms_hooks import ClaudeHookProgram, ClaudeTranscriptHook
from agent_comms_hooks.watch import WatchClaim


@dataclass(frozen=True)
class SessionHook(ClaudeTranscriptHook):
    @staticmethod
    def environment_session() -> str:
        """Claude exports the identity shared by its hook and bare Monitor command."""
        session = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
        if not session:
            raise ValueError("Claude did not export CLAUDE_CODE_SESSION_ID")
        return session

    @classmethod
    def current(cls, session: str, transcript: str) -> SessionHook:
        return cls(session_id=session, hook_event_name="SessionStart", transcript_path=transcript)

    def owner(self) -> ProcessIdentity:
        if self.environment_session() != self.session_id:
            raise ValueError("Claude hook session and CLAUDE_CODE_SESSION_ID disagree")
        pid = os.environ.get("CLAUDE_PID", "")
        if not pid:
            raise ValueError("Claude hook has no exported CLAUDE_PID")
        owner = ProcessIdentity.capture(int(pid))
        owner.require_ancestor_of(os.getpid())
        return owner

    def start(self) -> None:
        Serve.launch(WatchClaim(self.session_id), self.owner, Path(self.transcript_path))

    def end(self) -> None:
        claim = WatchClaim(self.session_id)
        claim.state().end(lambda: EndSession(owner=self.owner()))


class SessionStarted(ClaudeHookProgram):
    events = ("SessionStart",)

    @classmethod
    def command(cls) -> str:
        return cls.console_script(f"{started.__module__}:{started.__name__}")


class SessionEnded(ClaudeHookProgram):
    events = ("SessionEnd",)

    @classmethod
    def command(cls) -> str:
        return cls.console_script(f"{ended.__module__}:{ended.__name__}")


def started() -> None:
    SessionHook.read().start()


def ended() -> None:
    SessionHook.read().end()
