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
import shutil
import sys
from contextlib import suppress
from pathlib import Path

from .declarations import GLOBAL_CHANNEL, Message, is_channel_target
from .operations import wire

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
        from .declarations import Thread, UnregisteredThreadError, current_thread

        try:
            thread = current_thread()
        except UnregisteredThreadError:
            name = os.environ.get("AGENT_COMMS_THREAD") or "participant"
            thread = Thread(name=name, tags=frozenset({"bot"}), worktree=os.getcwd())
        self._thread_name = thread.name
        self._comms.register(thread)
        print(
            f"participant {thread.name!r} live on wire {self._comms.root}"
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
                self._comms.stop(self._thread_name)

    # ─── One poll ─────────────────────────────────────────────────────────────

    async def _tick(self, name: str) -> None:
        self._comms.heartbeat(name)
        inbox = self._comms.inbox(name)
        if not inbox:
            return
        self._comms.acknowledge(name)
        for message in inbox:
            await self._respond(name, message)

    async def _respond(self, name: str, message: Message) -> None:
        reply = await self._ask_agent(message)
        if not reply:
            return
        if is_channel_target(message.target) or message.target == "broadcast":
            target = GLOBAL_CHANNEL if message.target == "broadcast" else message.target
        else:
            target = message.sender
        self._comms.send(name, target, reply[:MAX_REPLY_CHARS])

    # ─── Backend ──────────────────────────────────────────────────────────────

    async def _ask_agent(self, message: Message) -> str:
        if shutil.which(self._agent_bin) is None:
            print(
                f"backend {self._agent_bin!r} not on PATH; message dropped: {message.body[:60]}",
                file=sys.stderr,
                flush=True,
            )
            return ""
        sender = None
        if message.sender in self._comms.registry:
            sender = self._comms.registry.require(message.sender)
        worktree = sender.worktree if sender and Path(sender.worktree).is_dir() else os.getcwd()
        try:
            proc = await asyncio.create_subprocess_exec(
                self._agent_bin,
                *self._agent_args,
                message.body,
                cwd=worktree,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                stdin=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            print(f"agent launch failed: {exc}", file=sys.stderr, flush=True)
            return ""
        assert proc.stdout is not None
        chunks: list[bytes] = []
        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                break
            chunks.append(chunk)
        await proc.wait()
        return b"".join(chunks).decode(errors="replace").strip()


def main() -> int:
    root = os.environ.get("AGENT_COMMS_ROOT")
    participant = Participant(root=root)
    with suppress(KeyboardInterrupt):
        asyncio.run(participant.run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
