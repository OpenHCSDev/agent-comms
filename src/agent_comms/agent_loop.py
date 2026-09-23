"""Agent communications — participant agent loop.

Turns any thread into a live participant: it adopts its identity from the
environment (``AGENT_COMMS_THREAD`` / ``PI_AGENT_ID``), polls its inbox,
runs response-eligible messages through a real agent backend (pi by default),
and replies to the sender. Observed informational/mentioned-only rows remain
in wire history but do not launch a turn. Channel messages get channel replies;
DMs get DM replies.

This is how an agent (pi, opencode, anything that can run headless) joins
the chat without an ACP client: launch it and it answers its DMs.

    AGENT_COMMS_THREAD=buddy agent-comms-agent
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import suppress
from pathlib import Path

from . import backend
from .declarations import GLOBAL_CHANNEL, ActivityState, Message, is_channel_target
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

    def _require_legacy_wire(self) -> None:
        """Never ACK a private cohort with the old inbox-based participant loop.

        This is a fail-closed compatibility guard, not proof that every old
        process has quiesced before a production protocol cutover.
        """
        metadata = self._comms.root / "bus_meta.json"
        try:
            value = json.loads(metadata.read_text())
        except FileNotFoundError:
            return
        except (OSError, ValueError) as error:
            raise RuntimeError("Cannot verify the participant bus protocol") from error
        if not isinstance(value, dict):
            raise RuntimeError("Cannot verify the participant bus protocol")
        if "writer_protocol_version" in value:
            raise RuntimeError("Legacy participant cannot consume a private cohort wire")

    def start(self) -> None:
        from .declarations import Thread, UnregisteredThreadError, current_thread

        self._require_legacy_wire()
        try:
            thread = current_thread()
        except UnregisteredThreadError:
            name = os.environ.get("AGENT_COMMS_THREAD") or "participant"
            thread = Thread(name=name, tags=frozenset({"bot"}), worktree=os.getcwd())
        self._comms.register(thread)
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
                self._comms.stop(self._thread_name)

    # ─── One poll ─────────────────────────────────────────────────────────────

    async def _tick(self, name: str) -> None:
        self._require_legacy_wire()
        name = self._comms.registry.require(name).name
        self._thread_name = name
        self._comms.heartbeat(name)
        inbox = self._comms.inbox(name)
        if not inbox:
            return
        self._comms.acknowledge(name)
        for message in inbox:
            if message.starts_turn_for(name):
                await self._respond(name, message)

    async def _respond(self, name: str, message: Message) -> None:
        self._comms.set_activity(name, ActivityState.THINKING, message.body[:80])
        reply = await self._ask_agent(name, message)
        if reply:
            if is_channel_target(message.target) or message.target == "broadcast":
                target = GLOBAL_CHANNEL if message.target == "broadcast" else message.target
            else:
                target = message.sender
            self._comms.send(name, target, reply[:MAX_REPLY_CHARS])
        self._comms.set_activity(name, ActivityState.IDLE)

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
        reply_parts: list[str] = []
        try:
            async for event in backend.stream_agent_events(
                self._agent_bin,
                self._agent_args,
                prompt,
                worktree,
                env_extra,
            ):
                kind = event.get("type")
                if kind == "chunk":
                    reply_parts.append(event.get("text") or "")
                elif kind == "agent_info":
                    self._comms.set_agent_info(
                        name,
                        model=event.get("model"),
                        session_name=event.get("session_name"),
                        context_used=event.get("context_used"),
                        context_size=event.get("context_size"),
                    )
                elif kind == "tool_start":
                    self._comms.set_activity(name, ActivityState.WORKING, event.get("title", ""))
                elif kind == "tool_end" and event.get("ok"):
                    self._comms.set_activity(name, ActivityState.THINKING, message.body[:80])
        except OSError as exc:
            print(f"agent launch failed: {exc}", file=sys.stderr, flush=True)
        return "".join(reply_parts).strip()


def main() -> int:
    root = os.environ.get("AGENT_COMMS_ROOT")
    participant = Participant(root=root)
    with suppress(KeyboardInterrupt):
        asyncio.run(participant.run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
