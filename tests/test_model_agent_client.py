"""agent-comms-model-agent registers its thread before it watches or sends from it.

The bridge used to watch and send as its --name without ever declaring the thread, so a fresh name
raised UnregisteredThreadError on its first reply and nobody could @mention it (Tristan,
2026-11-18). These run the real `ModelAgent` against an installed scratch bus; only the HTTP
endpoint is replaced, because the pod is not the thing under test.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from agent_comms.cli_bridges.model_agent_client import ChatCompletion, ChatEndpoint, ModelAgent
from agent_comms.comms import Comms
from agent_comms.messaging.agent_inbox import TakenInputStart
from agent_comms.messaging.messages import InfoKind
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.threads import NativeThread

from tests.conftest import declare_test_bus


@dataclass(frozen=True, slots=True)
class EchoEndpoint(ChatEndpoint):
    """The endpoint answers with the last user turn, so the reply proves the round trip."""

    def call(self, messages: list[dict]) -> ChatCompletion:
        return ChatCompletion(content=f"echo: {messages[-1]['content']}", finish_reason="stop",
                              completion_tokens=3)


def agent_on(comms: Comms, channels: frozenset[str] = frozenset({"model_test"})) -> ModelAgent:
    return ModelAgent(name="rl2", endpoint=EchoEndpoint(url="http://unused", model="rl2"),
                      root=comms.root, channels=channels)


def test_register_declares_the_thread_in_its_channels(comms: Comms) -> None:
    declare_test_bus(comms, channels=("model_test",))
    asyncio.run(agent_on(comms)._register(comms))
    assert comms.registry.snapshot().taken("rl2")
    assert "model_test" in comms.registry.require("rl2").tags


def test_register_keeps_an_already_registered_thread(comms: Comms) -> None:
    declare_test_bus(comms, channels=("model_test", "other"))
    asyncio.run(agent_on(comms)._register(comms))
    first = comms.registry.require("rl2")
    asyncio.run(agent_on(comms, frozenset({"other"}))._register(comms))
    assert comms.registry.require("rl2").incarnation == first.incarnation
    assert comms.registry.require("rl2").tags == first.tags


def test_a_message_to_a_fresh_name_gets_the_endpoint_reply(comms: Comms) -> None:
    declare_test_bus(comms, channels=("model_test",), threads=(
        NativeThread(name="tristan", tags=frozenset({"model_test"}),
                     worktree=Checkout(Path("/tmp/wt1"))),
    ))

    async def round_trip() -> str:
        serving = asyncio.create_task(agent_on(comms).run())
        try:
            while not comms.registry.snapshot().taken("rl2"):
                if serving.done():
                    serving.result()
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.2)  # the agent starts its watch past the empty backlog
            comms.messaging.send("tristan", "rl2", "are you there", InfoKind)
            async for message in comms.watch("tristan", after=TakenInputStart()):
                if message.sender == "rl2":
                    return message.body
            raise AssertionError("watch ended without a reply")
        finally:
            serving.cancel()

    body = asyncio.run(asyncio.wait_for(round_trip(), timeout=30))
    assert body.startswith("echo: are you there")
