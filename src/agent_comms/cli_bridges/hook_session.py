"""Resident Claude session hook behavior, behind the existing session socket."""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path


from agent_comms_hooks import ClaudeTranscriptHook
from agent_comms_hooks.watch import SkillText
from agent_comms_hooks.watch import SessionEndpoint
from .session_watch import WatchClaim
from .shared import Serve
from ..child_process import ProcessIdentity


@dataclass(frozen=True)
class SessionHook(ClaudeTranscriptHook):
    @classmethod
    def current(cls, session: str, transcript: str) -> SessionHook:
        return cls(session_id=session, hook_event_name="SessionStart", transcript_path=transcript)

    def owner(self) -> ProcessIdentity:
        if SessionEndpoint.environment_session() != self.session_id:
            raise ValueError("Claude hook session and CLAUDE_CODE_SESSION_ID disagree")
        pid = os.environ.get("CLAUDE_PID", "")
        if not pid:
            raise ValueError("Claude hook has no exported CLAUDE_PID")
        owner = ProcessIdentity.capture(int(pid))
        owner.require_ancestor_of(os.getpid())
        return owner

    def start(self) -> None:
        Serve.launch(WatchClaim(self.session_id), self.owner, Path(self.transcript_path))

    @classmethod
    def main(cls) -> None:
        """Launch once, then deal through that resident's actual Comms and Binding."""
        record = json.load(sys.stdin)
        hook = cls.decode(record)
        hook.start()
        SessionEndpoint(hook.session_id).session().invoke(SkillText, record)


if __name__ == "__main__":
    SessionHook.main()
