"""Input forwarding owns pending native IDs and never replays written inputs."""

from __future__ import annotations

import asyncio
import getpass
import os
import secrets
import tempfile
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from . import agent_events as events
from . import pi_commands as commands
from . import turn_failure as failures

if TYPE_CHECKING:
    from .backend import TurnSession
    from .pi_events import MessageStart, Response


@dataclass(frozen=True)
class ForwardedInput:
    original: str | dict[str, Any]
    public_id: str | None
    selected: list[str]


@dataclass
class InputForwarding:
    pending: list[tuple[str | None, str, str | dict[str, Any], str]] = field(default_factory=list)
    accepted: set[str] = field(default_factory=set)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    generation: int = 0
    started: bool = False
    uncertain: bool = False

    def acknowledge(self, response: Response) -> None:
        if response.success is True and any(item[0] == response.id for item in self.pending):
            self.accepted.add(response.id)
        self.changed.set()

    async def forward(self, session: TurnSession) -> None:
        while True:
            message = await session.steering_queue.get()
            original = dict(message) if isinstance(message, dict) else message
            wire = (
                dict(original)
                if isinstance(original, dict)
                else {"type": "prompt", "message": original, "streamingBehavior": "steer"}
            )
            forwarded = ForwardedInput(
                original, wire.pop("_input_id", None), wire.pop("_input_ids", [])
            )
            try:
                command = commands.PiCommand.from_wire(wire)
            except (ValueError, TypeError) as error:

                session.output.record_failure(
                    failures.PromptSendFailed(f"Invalid queued Pi command: {error}")
                )
                await session.proc.stop()
                return
            if not await command.steer(session, forwarded):
                return

    async def interrupt(self, session: TurnSession, forwarded: ForwardedInput) -> bool:

        while True:
            self.changed.clear()
            candidates = [item for item in self.pending if item[0] in forwarded.selected]
            if not candidates or all(item[0] in self.accepted for item in candidates):
                break
            await self.changed.wait()
        if not candidates:
            return True
        public_id, sent_text, _, native_id = candidates[0]
        boundary = (
            session.interrupt_boundary(public_id, native_id, sent_text)
            if session.interrupt_boundary
            else nullcontext(True)
        )
        with boundary as authorized:
            if authorized:
                session.stdin.write(
                    session.reader.encode(
                        commands.InterruptSteering(input_ids=[item[3] for item in candidates])
                    )
                )
        if not authorized:
            self.uncertain = True
            session.output.final_assistant_stop = False
            session.output.record_failure(
                failures.AuthorityChanged("Input authority changed before immediate steering.")
            )
            await session.proc.stop()
            return False
        await session.stdin.drain()
        return True

    async def send_prompt(
        self, command: commands.Prompt, session: TurnSession, forwarded: ForwardedInput
    ) -> bool:
        from .backend import _maintenance_send_boundary

        public_id = forwarded.public_id or f"agent-comms-steer-{uuid4().hex}"
        native_id = secrets.token_hex(16)
        text = command.message
        if not session.require_input_id and isinstance(forwarded.original, str):
            text = f"[agent-comms input-id: {public_id}]\n{forwarded.original}"
        command = replace(command, id=public_id, input_id=native_id, message=text)
        self.pending.append((public_id, text, forwarded.original, native_id))
        boundary = _maintenance_send_boundary(
            Path(
                session.launch.env.get("AGENT_COMMS_ROOT")
                or os.environ.get("AGENT_COMMS_ROOT")
                or str(Path(tempfile.gettempdir()) / f"agent-comms-startup-{getpass.getuser()}")
            ),
            session.send_boundary,
            public_id,
            native_id,
            text,
        )
        with boundary as authorized:
            if authorized:
                if command.images:
                    session.output.sensitive = True
                self.generation += 1
                session.stdin.write(session.reader.encode(command))
        if authorized:
            await session.stdin.drain()
            return True
        if authorized is False:
            self.uncertain = True
            session.output.final_assistant_stop = False
            session.output.record_failure(
                failures.AuthorityChanged("Input authority changed before Pi prompt send.")
            )
            await session.proc.stop()
            return False
        # None means never sent; preserve the owner's UNKNOWN, not a replay.
        self.pending[:] = [item for item in self.pending if item[3] != native_id]
        session.rejected_commands.append(events.InputRefused(id=public_id))
        session.rejected_signal.set()
        return True

    def mark_started(self, session: TurnSession, payload: MessageStart) -> tuple[bool, str | None]:
        message = payload.message
        if message is None or not message.user:
            return False, None
        text = message.text
        native_id = message.input_id
        for index, (input_id, queued_text, _, expected_native_id) in enumerate(self.pending):
            if (
                (session.require_input_id or input_id in self.accepted)
                and text == queued_text
                and (not session.require_input_id or native_id == expected_native_id)
            ):
                if session.native_start is not None and (
                    not session.native_start(input_id, expected_native_id, queued_text)
                ):
                    return (False, None)
                self.accepted.discard(input_id)
                self.pending.pop(index)
                self.started = True
                return (True, input_id)
        return (False, None)

    def restore(self, session: TurnSession) -> None:
        if session.steering_queue is None or not self.pending:
            return
        queued: list[str | dict[str, Any]] = []
        while not session.steering_queue.empty():
            queued.append(session.steering_queue.get_nowait())
        for item in queued:
            session.steering_queue.put_nowait(item)
        self.pending.clear()
