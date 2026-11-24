"""agent-comms-model-agent registers its thread before it watches or sends from it.

The bridge used to watch and send as its --name without ever declaring the thread, so a fresh name
raised UnregisteredThreadError on its first reply and nobody could @mention it (Tristan,
2026-11-18). These run the real `ModelAgent` against an installed scratch bus; only the HTTP
endpoint is replaced, because the pod is not the thing under test.
"""

from __future__ import annotations

import asyncio

import pytest
from dataclasses import dataclass, replace
from pathlib import Path

from agent_comms.cli_bridges.model_agent_client import (
    ChatCompletion,
    ChatEndpoint,
    EmptyContentReply,
    HttpFailureReply,
    ModelAgent,
    ModelReply,
    _completion_from,
)
from agent_comms.comms import Comms
from agent_comms.execution.provider_records import ParsedThinking, TextContent
from agent_comms.transcript.transcript_events import AssistantTranscript, ThinkingTranscript
from agent_comms.messaging.agent_inbox import TakenInputStart
from agent_comms.messaging.messages import InfoKind
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.thread_status import StoppedThreadStatus
from agent_comms.registry.threads import ExternalThread, ForkThread, NativeThread

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


def test_register_keeps_the_thread_and_joins_a_missing_channel(comms: Comms) -> None:
    declare_test_bus(comms, channels=("model_test", "other"))
    asyncio.run(agent_on(comms)._register(comms))
    first = comms.registry.require("rl2")
    asyncio.run(agent_on(comms, frozenset({"other"}))._register(comms))
    assert comms.registry.require("rl2").incarnation == first.incarnation
    assert comms.registry.require("rl2").tags == {"model_test", "other"}


def test_a_name_held_by_another_kind_of_thread_stops_the_bridge(comms: Comms) -> None:
    declare_test_bus(comms, channels=("model_test",), threads=(
        NativeThread(name="rl2", tags=frozenset(), worktree=Checkout(Path("/tmp/wt1"))),))
    with pytest.raises(SystemExit):
        asyncio.run(agent_on(comms)._register(comms))
    assert isinstance(comms.registry.require("rl2"), NativeThread)


def test_takeover_replaces_a_stopped_thread_with_the_endpoint(comms: Comms) -> None:
    declare_test_bus(comms, channels=("model_test",))
    comms.registry.declare(
        NativeThread(name="rl2", tags=frozenset(), worktree=Checkout(Path("/tmp/wt1"))),
        StoppedThreadStatus())
    asyncio.run(replace(agent_on(comms), takeover=True)._register(comms))
    thread = comms.registry.require("rl2")
    assert isinstance(thread, ExternalThread)
    assert "model_test" in thread.tags


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


def test_a_channel_mention_is_answered_in_the_channel_by_a_connected_endpoint(
        comms: Comms) -> None:
    """The live failure: `@rl2` in #model_test was refused ("does not deliver to @rl2") and,
    once delivered, would have been answered by DM. The endpoint must be in the channel's
    audience, show connected with its process, and answer in the channel."""
    declare_test_bus(comms, channels=("model_test",), threads=(
        NativeThread(name="tristan", tags=frozenset({"model_test"}),
                     worktree=Checkout(Path("/tmp/wt1"))),
    ))

    async def round_trip() -> tuple[str, str, bool]:
        serving = asyncio.create_task(agent_on(comms).run())
        try:
            while not (comms.registry.snapshot().taken("rl2")
                       and comms.registry.require("rl2").process_alive):
                if serving.done():
                    serving.result()
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.2)
            connected = comms.registry.require("rl2").process_alive
            comms.messaging.send("tristan", "#model_test", "@rl2 are you there", InfoKind)
            async for message in comms.watch("tristan", after=TakenInputStart()):
                if message.sender == "rl2":
                    return message.target, message.body, connected
            raise AssertionError("watch ended without a reply")
        finally:
            serving.cancel()

    target, body, connected = asyncio.run(asyncio.wait_for(round_trip(), timeout=30))
    assert connected
    assert target == "#model_test"
    assert body.startswith("echo: @rl2 are you there")


def test_a_message_sent_while_the_bridge_was_down_is_answered_after_the_restart(
        comms: Comms) -> None:
    """The live failure: a replace=true restart skipped every DM sent during the gap, because a
    fresh bridge started at the inbox tail. Replies now name what they answer, and a restart
    resumes after the newest answered message."""
    declare_test_bus(comms, channels=("model_test",), threads=(
        NativeThread(name="tristan", tags=frozenset({"model_test"}),
                     worktree=Checkout(Path("/tmp/wt1"))),
    ))

    async def serve_until_replied(send_after_start: str | None, after: int) -> str:
        serving = asyncio.create_task(agent_on(comms).run())
        try:
            while not (comms.registry.snapshot().taken("rl2")
                       and comms.registry.require("rl2").process_alive):
                if serving.done():
                    serving.result()
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.2)  # the agent starts its watch
            if send_after_start is not None:
                after = comms.messaging.send(
                    "tristan", "rl2", send_after_start, InfoKind).message.reference.seq
            async for message in comms.watch("tristan", after=TakenInputStart()):
                if message.sender == "rl2" and message.reference.seq > after:
                    return message.body
            raise AssertionError("watch ended without a reply")
        finally:
            serving.cancel()
            await asyncio.gather(serving, return_exceptions=True)

    async def scenario() -> tuple[str, str]:
        before = await serve_until_replied("first", 0)
        gap = comms.messaging.send("tristan", "rl2", "sent during the gap", InfoKind)
        after = await serve_until_replied(None, gap.message.reference.seq)
        return before, after

    before, after = asyncio.run(asyncio.wait_for(scenario(), timeout=60))
    assert before.startswith("echo: first")
    assert after.startswith("echo: sent during the gap")


def test_a_stopped_fork_thread_is_not_an_external_endpoint_until_takeover(comms: Comms) -> None:
    """ForkThread inherits ExternalThread via SubagentThread, so isinstance(current, ExternalThread)
    used to treat a stopped fork as an endpoint instead of refusing or taking it over."""
    declare_test_bus(comms, channels=("model_test",))
    comms.registry.declare(
        ForkThread(name="rl2", tags=frozenset(), worktree=Checkout(Path("/tmp/wt1"))),
        StoppedThreadStatus(),
    )
    with pytest.raises(SystemExit):
        asyncio.run(agent_on(comms)._register(comms))
    assert type(comms.registry.require("rl2")) is ForkThread

    asyncio.run(replace(agent_on(comms), takeover=True)._register(comms))
    thread = comms.registry.require("rl2")
    assert type(thread) is ExternalThread
    assert "model_test" in thread.tags


def test_model_reply_family_owns_history_logging_and_rejection_sampling(comms: Comms) -> None:
    @dataclass(frozen=True, slots=True)
    class ScriptedEndpoint(ChatEndpoint):
        replies: tuple[ModelReply, ...] = ()
        calls: list[int] = None  # type: ignore[assignment]

        def call(self, messages: list[dict[str, str]]) -> ModelReply:
            index = len(self.calls)
            self.calls.append(index)
            return self.replies[index]

    async def run_best(agent: ModelAgent) -> tuple[ModelReply, str]:
        return await agent._best_reply(asyncio.get_running_loop(), [])

    # Failure settles immediately without sampling further candidates.
    failure_calls: list[int] = []
    failure_agent = ModelAgent(
        name="rl2",
        endpoint=ScriptedEndpoint(
            url="http://unused", model="rl2",
            replies=(HttpFailureReply(500, "boom"), ChatCompletion("later", "stop", 1)),
            calls=failure_calls,
        ),
        root=comms.root,
        rejection_k=3,
    )
    reply, rendered = asyncio.run(run_best(failure_agent))
    assert isinstance(reply, HttpFailureReply)
    assert failure_calls == [0]
    assert reply.log_summary(rendered) == reply.display()
    history: list[dict[str, str]] = []
    reply.record_in(history)
    assert history == []

    # Clean completion records in history and logs char count.
    clean_calls: list[int] = []
    clean_agent = ModelAgent(
        name="rl2",
        endpoint=ScriptedEndpoint(
            url="http://unused", model="rl2",
            replies=(ChatCompletion("Clean plain reply.", "stop", 4),),
            calls=clean_calls,
        ),
        root=comms.root,
        rejection_k=3,
    )
    reply, rendered = asyncio.run(run_best(clean_agent))
    assert isinstance(reply, ChatCompletion)
    assert clean_calls == [0]
    assert reply.log_summary(rendered) == f"{len(rendered)} chars"
    reply.record_in(history)
    assert history == [{"role": "assistant", "content": "Clean plain reply."}]

    # Rejected candidates compare structural_reward and append the sampling footer.
    rejected_calls: list[int] = []
    rejected_agent = ModelAgent(
        name="rl2",
        endpoint=ScriptedEndpoint(
            url="http://unused", model="rl2",
            replies=(
                ChatCompletion("<tool_call>bad</tool_call>", "stop", 5),
                ChatCompletion("<tool_call>still bad</tool_call>", "stop", 5),
            ),
            calls=rejected_calls,
        ),
        root=comms.root,
        rejection_k=2,
    )
    reply, rendered = asyncio.run(run_best(rejected_agent))
    assert isinstance(reply, ChatCompletion)
    assert rejected_calls == [0, 1]
    assert "rejection-sampled 2×" in rendered


def test_completion_decodes_reasoning_content_and_inline_think_without_history_leak() -> None:
    # Separate OpenAI reasoning_content (LoopedServer / llama-server --reasoning-format).
    reply = _completion_from({
        "choices": [{
            "message": {
                "content": "Final answer.",
                "reasoning_content": "Certainty:\n- DEDUCED: checked",
            },
            "finish_reason": "stop",
        }],
        "usage": {"completion_tokens": 42},
    })
    assert isinstance(reply, ChatCompletion)
    assert reply.thinking == "Certainty:\n- DEDUCED: checked"
    assert reply.content == "Final answer."
    rendered = reply.display()
    assert "> _Thinking:_\n> Certainty:\n> - DEDUCED: checked" in rendered
    assert "Final answer." in rendered
    assert "<think>" not in rendered
    history: list[dict[str, str]] = []
    reply.record_in(history)
    assert history == [{"role": "assistant", "content": "Final answer."}]

    # Inline leading <think>...</think> decoded once at the boundary.
    inline = _completion_from({
        "choices": [{
            "message": {"content": "<think>step 1\nstep 2</think>\n\nVisible reply."},
            "finish_reason": "stop",
        }],
        "usage": {"completion_tokens": 18},
    })
    assert isinstance(inline, ChatCompletion)
    assert inline.thinking == "step 1\nstep 2"
    assert inline.content == "Visible reply."
    assert "<think>" not in inline.display()

    # Truncated thought (finish_reason=length, no answer yet) preserves thinking instead of EmptyContentReply.
    truncated = _completion_from({
        "choices": [{
            "message": {"content": "<think>still working out the proof"},
            "finish_reason": "length",
        }],
        "usage": {"completion_tokens": 1024},
    })
    assert isinstance(truncated, ChatCompletion)
    assert truncated.thinking == "still working out the proof"
    assert truncated.content == ""
    history_after: list[dict[str, str]] = []
    truncated.record_in(history_after)
    assert history_after == []

    # Truly empty content and reasoning still returns EmptyContentReply.
    empty = _completion_from({"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]})
    assert isinstance(empty, EmptyContentReply)

    # Prose merely discussing <think> stays intact.
    prose = ParsedThinking.parse("Our parser strips <think>...</think> blocks.")
    assert prose.thinking == ""
    assert prose.visible == "Our parser strips <think>...</think> blocks."


def test_native_text_content_splits_leading_inline_think_into_thinking_transcript() -> None:
    events = TextContent("<think>checked file</think>\nDone.").assistant_transcript(7, 100.0)
    assert len(events) == 2
    assert isinstance(events[0], ThinkingTranscript) and events[0].text == "checked file"
    assert isinstance(events[1], AssistantTranscript) and events[1].text == "Done."

    plain = TextContent("Mentions <think> in prose.").assistant_transcript(8, 101.0)
    assert len(plain) == 1
    assert isinstance(plain[0], AssistantTranscript) and plain[0].text == "Mentions <think> in prose."
