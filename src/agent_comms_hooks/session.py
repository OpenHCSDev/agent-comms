"""Claude session hooks start/stop the configured bridge, never a Monitor reader."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

from agent_comms_hooks import ClaudeHook, ClaudeHookProgram, PythonHookCommand
from agent_comms_hooks.watch import LiveWatch, RecordedWatch, WatchClaim


@dataclass(frozen=True)
class SessionHook(ClaudeHook):
    transcript_path: str

    @classmethod
    def current(cls, session: str, transcript: str) -> SessionHook:
        return cls(session_id=session, hook_event_name="SessionStart", transcript_path=transcript)

    def owner(self):
        from agent_comms.child_process import ProcessIdentity

        if os.environ.get("CLAUDE_CODE_SESSION_ID") != self.session_id:
            raise ValueError("Claude hook session and CLAUDE_CODE_SESSION_ID disagree")
        owner = ProcessIdentity.capture(int(os.environ["CLAUDE_PID"]))
        owner.require_ancestor_of(os.getpid())
        return owner

    def start(self) -> None:
        claim = WatchClaim(self.session_id)
        if not isinstance(claim.state(), RecordedWatch):
            return
        import json

        from agent_comms.child_process import ParentedProcess
        from agent_comms.cli_bridges.monitor import VerifySession
        from agent_comms.cli_bridges.shared import Serve
        from agent_comms.field_codec import FieldCodec

        owner = self.owner()
        request = VerifySession(owner=owner)
        with claim.launching():
            state = claim.state()
            if not isinstance(state, RecordedWatch):
                return
            try:
                request.exchange(claim)
                return
            except FileNotFoundError, ConnectionRefusedError:
                if isinstance(state, LiveWatch):
                    raise RuntimeError(
                        "An older watch still owns this session. Stop that Monitor once, then connect."
                    )
            command = (
                state.record.command[0],
                Serve.__name__.lower(),
                *state.record.command[2:],
                "--owner",
                json.dumps(FieldCodec.encode_record(owner)),
                "--transcript",
                self.transcript_path,
            )
            log = claim.path.with_suffix(".log")
            with log.open("ab") as output:
                child = ParentedProcess.launch(command, env=dict(os.environ), output=output)
            deadline = time.monotonic() + 30
            while child.alive and time.monotonic() < deadline:
                try:
                    request.exchange(claim)
                    return
                except FileNotFoundError, ConnectionRefusedError:
                    time.sleep(0.05)
            raise RuntimeError(
                f"Session bridge startup not observed (PID {child.pid}); inspect {log}. No launch retry."
            )

    def end(self) -> None:
        claim = WatchClaim(self.session_id)
        if not isinstance(claim.state(), LiveWatch):
            return
        from agent_comms.cli_bridges.monitor import EndSession

        try:
            EndSession(owner=self.owner()).exchange(claim)
        except FileNotFoundError, ConnectionRefusedError:
            pass


def started() -> None:
    SessionHook.read().start()


def ended() -> None:
    SessionHook.read().end()


class SessionStarted(PythonHookCommand, ClaudeHookProgram):
    events = ("SessionStart",)
    target = staticmethod(started)


class SessionEnded(PythonHookCommand, ClaudeHookProgram):
    events = ("SessionEnd",)
    target = staticmethod(ended)
