"""Claude session hooks start/stop the configured bridge, never a Monitor reader."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

from agent_comms.child_process import ParentedProcess, ProcessIdentity
from agent_comms.cli_bridges.monitor import EndSession, VerifySession
from agent_comms.cli_bridges.shared import Serve
from agent_comms.launch_runtime import LaunchRuntime
from pathlib import Path
from agent_comms_hooks import ClaudeHookProgram, ClaudeTranscriptHook
from agent_comms_hooks.watch import LiveWatch, RecordedWatch, WatchClaim


@dataclass(frozen=True)
class SessionHook(ClaudeTranscriptHook):
    @classmethod
    def current(cls, session: str, transcript: str) -> SessionHook:
        return cls(session_id=session, hook_event_name="SessionStart", transcript_path=transcript)

    def owner(self) -> ProcessIdentity:
        if os.environ.get("CLAUDE_CODE_SESSION_ID") != self.session_id:
            raise ValueError("Claude hook session and CLAUDE_CODE_SESSION_ID disagree")
        pid = os.environ.get("CLAUDE_PID", "")
        if not pid:
            raise ValueError("Claude hook has no exported CLAUDE_PID")
        owner = ProcessIdentity.capture(int(pid))
        owner.require_ancestor_of(os.getpid())
        return owner

    def start(self) -> None:
        claim = WatchClaim(self.session_id)
        if not isinstance(claim.state(), RecordedWatch):
            return
        owner = self.owner()
        request = VerifySession.model_validate(dict(owner=owner))
        with claim.launching():
            state = claim.state()
            if not isinstance(state, RecordedWatch):
                return
            try:
                request.exchange(claim)
                return
            except (FileNotFoundError, ConnectionRefusedError) as error:
                if isinstance(state, LiveWatch):
                    raise RuntimeError(
                        "An older watch still owns this session. "
                        "Stop that Monitor once, then connect."
                    ) from error
            runtime = LaunchRuntime.installed()
            command = Serve(owner=owner, transcript=Path(self.transcript_path)).command(
                state.record, runtime)
            log = claim.path.with_suffix(".log")
            with log.open("ab") as output:
                child = ParentedProcess.launch(
                    command, env=runtime.environment(os.environ), output=output, runtime=runtime)
            deadline = time.monotonic() + 30
            while child.alive() and time.monotonic() < deadline:
                try:
                    request.exchange(claim)
                    return
                except FileNotFoundError, ConnectionRefusedError:
                    time.sleep(0.05)
            raise RuntimeError(
                f"Session bridge startup not observed (PID {child.pid}); inspect {log}. "
                "No launch retry."
            )

    def end(self) -> None:
        claim = WatchClaim(self.session_id)
        if not isinstance(claim.state(), LiveWatch):
            return
        EndSession.model_validate(dict(owner=self.owner())).exchange(claim)


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
