"""Resident Claude session hook behavior, behind the existing session socket."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


from .hook_skill import DeckStart
from .session_watch import WatchClaim
from .shared import Serve
from ..child_process import ProcessIdentity


@dataclass(frozen=True)
class SessionHook(DeckStart):
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
        # Launch and the initial deal are one ordered startup, not parallel hooks racing a socket.
        self.warn(*self.take())


if __name__ == "__main__":
    SessionHook.read().start()
