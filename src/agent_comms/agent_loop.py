"""Agent communications — participant agent loop.

Turns any thread into a live participant: it adopts its identity from the
environment (``AGENT_COMMS_THREAD`` / ``PI_AGENT_ID``), polls its inbox,
runs every incoming message through a real agent backend (pi by default),
and replies to the sender. Channel messages get channel replies; DMs get
DM replies.

This is how an agent (pi, opencode, anything that can run headless) joins
the chat without an ACP client: launch it and it answers its DMs.

    AGENT_COMMS_THREAD=buddy agent-comms-agent
"""

from __future__ import annotations

import asyncio
import os
import sys
from contextlib import suppress
from pathlib import Path

from . import agent_events as events
from . import backend
from .activity import ActivityState
from .channel_targets import BuiltinChannel, is_channel_target
from .comms import Comms, wire
from .messages import Message
from .mro_dispatch import handles

DEFAULT_AGENT_BIN = "pi"
DEFAULT_AGENT_ARGS = [
    "--print",
    "--no-session",
    "--provider",
    "openrouter",
    "--model",
    "z-ai/glm-5.3-flash",
]
POLL_INTERVAL = 1.0
MAX_REPLY_CHARS = 4000


class ParticipantEventConsumer(events.AgentEventConsumer):
    def __init__(self, comms: Comms, name: str, task: str) -> None:
        self._comms = comms
        self._name = name
        self.task = task
        self.reply_parts: list[str] = []

    @property
    def comms(self) -> Comms:
        return self._comms

    @property
    def thread_name(self) -> str:
        return self._name

    def update_activity(self, state: ActivityState, detail: str) -> None:
        self.comms.agents.set_activity(self.thread_name, state, detail)

    @handles(events.Chunk)
    async def chunk(self, event: events.Chunk) -> None:
        self.reply_parts.append(event.text)

    @handles(events.ToolEnd)
    async def tool_end(self, event: events.ToolEnd) -> None:
        if event.ok:
            self.update_activity(ActivityState.THINKING, self.task[:80])


class Participant:
    """One thread answering its own inbox via a headless agent backend."""

    def __init__(
        self,
        root: Path | str | None = None,
        agent_bin: str | None = None,
        agent_args: list[str] | None = None,
    ):
        self._comms = wire(root)
        self._agent_bin = agent_bin or os.environ.get("AGENT_COMMS_AGENT_BIN", DEFAULT_AGENT_BIN)
        arg_env = os.environ.get("AGENT_COMMS_AGENT_ARGS", "")
        self._agent_args = (
            agent_args
            if agent_args is not None
            else (arg_env.split() if arg_env else list(DEFAULT_AGENT_ARGS))
        )
        self._thread_name: str | None = None

    # ─── Lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        from .errors import UnregisteredThreadError
        from .threads import Thread, current_thread

        try:
            thread = current_thread()
        except UnregisteredThreadError:
            name = os.environ.get("AGENT_COMMS_THREAD") or "participant"
            thread = Thread(name=name, tags=frozenset({"bot"}), worktree=os.getcwd())
        self._comms.threads.register(thread)
        self._thread_name = self._comms.registry.require(thread.name).name
        print(
            f"participant {self._thread_name!r} live on wire {self._comms.root}"
            f" (backend: {self._agent_bin}, tags: {sorted(thread.tags)})",
            flush=True,
        )

    async def run(self) -> None:
        self.start()
        assert self._thread_name is not None
        try:
            while True:
                await self._tick(self._thread_name)
                await asyncio.sleep(POLL_INTERVAL)
        finally:
            if self._thread_name:
                self._comms.owners.stop(self._thread_name)

    # ─── One poll ─────────────────────────────────────────────────────────────

    async def _tick(self, name: str) -> None:
        name = self._comms.registry.require(name).name
        self._thread_name = name
        self._comms.threads.heartbeat(name)
        inbox = self._comms.bus.inbox(name)
        if not inbox:
            return
        self._comms.messaging.acknowledge(name)
        for message in inbox:
            await self._respond(name, message)

    async def _respond(self, name: str, message: Message) -> None:
        self._comms.agents.set_activity(name, ActivityState.THINKING, message.body[:80])
        reply = await self._ask_agent(name, message)
        if reply:
            if is_channel_target(message.target) or BuiltinChannel.is_alias(message.target):
                target = BuiltinChannel.canonical(message.target)
            else:
                target = message.sender
            self._comms.messaging.send(name, target, reply[:MAX_REPLY_CHARS])
        self._comms.agents.set_activity(name, ActivityState.IDLE)

    # ─── Backend ──────────────────────────────────────────────────────────────

    async def _ask_agent(self, name: str, message: Message) -> str:
        thread = self._comms.registry.require(name)
        name = thread.name
        sender = None
        if message.sender in self._comms.registry:
            sender = self._comms.registry.require(message.sender)
        worktree = sender.worktree if sender and Path(sender.worktree).is_dir() else os.getcwd()
        prompt = (
            f"You are agent-comms thread {name!r}. Message #{message.seq} from "
            f"{message.sender!r} to {message.target!r} follows. Use agent-comms tools "
            "for requested coordination or lifecycle actions. Do not call comms_send "
            "for your reply; your final response is delivered automatically.\n\n"
            f"{message.body}"
        )
        env_extra = {
            "AGENT_COMMS_THREAD": name,
            "PI_AGENT_ID": name,
            "AGENT_COMMS_ROOT": str(self._comms.root),
        }
        consumer = ParticipantEventConsumer(self._comms, name, message.body)
        try:
            async for event in backend.stream_agent_events(
                self._agent_bin,
                self._agent_args,
                prompt,
                worktree,
                env_extra,
            ):
                await consumer.dispatch(event)
        except OSError as exc:
            print(f"agent launch failed: {exc}", file=sys.stderr, flush=True)
        return "".join(consumer.reply_parts).strip()


def main() -> int:
    root = os.environ.get("AGENT_COMMS_ROOT")
    participant = Participant(root=root)
    with suppress(KeyboardInterrupt):
        asyncio.run(participant.run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
