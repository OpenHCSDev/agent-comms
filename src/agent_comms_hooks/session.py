"""Claude session hooks start/stop the configured bridge, never a Monitor reader."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import aclosing
from dataclasses import dataclass

from agent_comms.child_process import ParentedProcess, ProcessIdentity
from agent_comms.cli_bridges.monitor import EndSession, VerifySession
from agent_comms.cli_bridges.shared import Serve
from agent_comms.field_codec import FieldCodec
from agent_comms.wire_watch import WatchSource, WireWatch
from agent_comms_hooks import ClaudeHook, ClaudeHookProgram, PythonHookCommand
from agent_comms_hooks.watch import RecordedWatch, WatchClaim


@dataclass(frozen=True)
class SessionHook(ClaudeHook):
    transcript_path: str

    @classmethod
    def current(cls, session: str, transcript: str) -> SessionHook:
        return cls(session_id=session, hook_event_name="SessionStart", transcript_path=transcript)

    def owner(self) -> ProcessIdentity:
        if os.environ.get("CLAUDE_CODE_SESSION_ID") != self.session_id:
            raise ValueError("Claude hook session and CLAUDE_CODE_SESSION_ID disagree")
        owner = ProcessIdentity.capture(int(os.environ["CLAUDE_PID"]))
        owner.require_ancestor_of(os.getpid())
        return owner

    async def ready(self, request: VerifySession, claim: WatchClaim) -> None:
        async with (
            asyncio.timeout(30),
            aclosing(
                WireWatch.observations(WatchSource.modified_file(claim.socket_path))
            ) as changes,
        ):
            async for _ in changes:
                try:
                    await asyncio.to_thread(request.exchange, claim)
                except FileNotFoundError, ConnectionRefusedError:
                    continue
                return

    def start(self) -> None:
        claim = WatchClaim(self.session_id)
        with claim.launching():
            claim.state().start_session(self)

    def start_recorded(self, state: RecordedWatch) -> None:
        owner = self.owner()
        request = VerifySession.model_validate(dict(owner=owner))
        try:
            request.exchange(state.claim)
            return
        except (FileNotFoundError, ConnectionRefusedError) as error:
            state.require_launch(error)
        command = (
            state.record.command[0],
            Serve.__name__.lower(),
            *state.record.command[2:],
            "--owner",
            json.dumps(FieldCodec.encode_record(owner)),
            "--transcript",
            self.transcript_path,
        )
        log = state.claim.path.with_suffix(".log")
        with log.open("ab") as output:
            child = ParentedProcess.launch(command, env=dict(os.environ), output=output)
        try:
            asyncio.run(self.ready(request, state.claim))
        except TimeoutError as error:
            raise RuntimeError(
                f"Session bridge startup not observed (PID {child.pid}); inspect {log}. "
                "No launch retry."
            ) from error

    def end(self) -> None:
        WatchClaim(self.session_id).state().end_session(self)

    def end_recorded(self, state: RecordedWatch) -> None:
        try:
            EndSession.model_validate(dict(owner=self.owner())).exchange(state.claim)
        except FileNotFoundError, ConnectionRefusedError:
            return


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
