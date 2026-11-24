"""agent-comms-model-agent: a bus thread's end of an OpenAI-compatible chat endpoint.

Runs on the bus host. Takes one thread name on the bus (what senders @mention and DM) and one
model endpoint; every message the thread receives goes through one chat/completions call, and
the typed reply comes straight back from the thread. No Claude session in the middle.

The transport returns one ``ModelReply`` family member: ``ChatCompletion`` on success, or one of
``HttpFailure``, ``TransportFailure``, ``NonJsonReply``, ``MalformedReply`` or ``EmptyContent``.
Each member owns ``display()``, so the daemon never branches on a kind; a consumer that wants to
know what happened asks the family instead of parsing a bracketed prefix.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from contextlib import ExitStack
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import ClassVar

from agent_comms.comms import Comms
from agent_comms.conversational_slop import ResponseContext, SlopVerdict, verify_all
from agent_comms.errors import RelationViolationError
from agent_comms.execution.child_process import ProcessIdentity, ProcessName
from agent_comms.execution.launch_root import LaunchRoot
from agent_comms.execution.provider_records import ParsedThinking
from agent_comms.installer.active_route import resolve_comms_route
from agent_comms.messaging.agent_inbox import TakenInputStart
from agent_comms.messaging.message_reference import AfterMessageStart, InboxStart
from agent_comms.messaging.messages import InfoKind
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.threads import ExternalThread


MODEL_AGENT_PROCESS = ProcessName("ac-model-agent")


class SamplingOutcome(ABC):
    """State of rejection sampling across candidate completions."""

    settled: ClassVar[bool] = False

    @abstractmethod
    def consider(self, reply: ChatCompletion, verdict: SlopVerdict) -> SamplingOutcome: ...

    @abstractmethod
    def finish(self, attempts: int) -> tuple[ModelReply, str]: ...


@dataclass(frozen=True, slots=True)
class NoCandidate(SamplingOutcome):
    def consider(self, reply: ChatCompletion, verdict: SlopVerdict) -> SamplingOutcome:
        return ScoredCandidate(reply, verdict)

    def finish(self, attempts: int) -> tuple[ModelReply, str]:
        raise RuntimeError("Rejection sampling finished without any candidate")


@dataclass(frozen=True, slots=True)
class ScoredCandidate(SamplingOutcome):
    reply: ChatCompletion
    verdict: SlopVerdict

    def consider(self, reply: ChatCompletion, verdict: SlopVerdict) -> SamplingOutcome:
        if verdict.structural_reward > self.verdict.structural_reward:
            return ScoredCandidate(reply, verdict)
        return self

    def finish(self, attempts: int) -> tuple[ModelReply, str]:
        footer = (f"\n\n> _rejection-sampled {attempts}×, best reward "
                  f"{self.verdict.structural_reward:.2f}; triggered {self.verdict.triggered}_")
        return self.reply, self.reply.display() + footer


@dataclass(frozen=True, slots=True)
class SettledCandidate(SamplingOutcome):
    settled: ClassVar[bool] = True
    reply: ModelReply
    rendered: str

    def consider(self, reply: ChatCompletion, verdict: SlopVerdict) -> SamplingOutcome:
        return self

    def finish(self, attempts: int) -> tuple[ModelReply, str]:
        return self.reply, self.rendered


class ModelReply(DeclaredFamily, affix="Reply"):
    """Everything a chat/completions call can produce: one completion, or one named failure."""

    @abstractmethod
    def display(self) -> str:
        """The text the sender sees on the bus."""

    def record_in(self, history: list[dict[str, str]]) -> None:
        """Only successful completions enter per-sender conversation history."""

    def log_summary(self, rendered: str) -> str:
        """Failure types log their reply content inline so stdout shows what failed."""
        return self.display()

    def sample(
        self,
        attempt: int,
        total: int,
        context: ResponseContext,
        current: SamplingOutcome,
    ) -> SamplingOutcome:
        """Transport/decode failures settle immediately instead of being rejection-sampled."""
        return SettledCandidate(self, self.display())


@dataclass(frozen=True, slots=True)
class ChatCompletion(ModelReply):
    content: str
    finish_reason: str
    completion_tokens: int | None
    thinking: str = ""

    def display(self) -> str:
        parts: list[str] = []
        if self.thinking:
            quoted = "\n".join(
                f"> {line}" if line else ">" for line in self.thinking.splitlines()
            )
            parts.append(f"> _Thinking:_\n{quoted}")
        if self.content:
            parts.append(self.content)
        body = "\n\n".join(parts)
        return (f"{body}\n\n---\n"
                f"_{self.completion_tokens or '?'} tokens, finish {self.finish_reason}_")

    def record_in(self, history: list[dict[str, str]]) -> None:
        if self.content:
            history.append({"role": "assistant", "content": self.content})

    def log_summary(self, rendered: str) -> str:
        return f"{len(rendered)} chars"

    def sample(
        self,
        attempt: int,
        total: int,
        context: ResponseContext,
        current: SamplingOutcome,
    ) -> SamplingOutcome:
        verdict = verify_all(self.content, context)
        print(f"[model-agent]   candidate {attempt + 1}/{total}: "
              f"reward={verdict.structural_reward:.2f} triggered={verdict.triggered}",
              flush=True)
        if verdict.passed:
            return SettledCandidate(self, self.display())
        return current.consider(self, verdict)


@dataclass(frozen=True, slots=True)
class EmptyContentReply(ModelReply):
    finish_reason: str

    def display(self) -> str:
        return f"[model-agent empty content, finish={self.finish_reason}]"


@dataclass(frozen=True, slots=True)
class HttpFailureReply(ModelReply):
    status: int
    detail: str

    def display(self) -> str:
        return f"[model-agent http {self.status}: {self.detail}]"


@dataclass(frozen=True, slots=True)
class TransportFailureReply(ModelReply):
    error_name: str
    detail: str

    def display(self) -> str:
        return f"[model-agent transport error: {self.error_name}: {self.detail}]"


@dataclass(frozen=True, slots=True)
class NonJsonReply(ModelReply):
    detail: str

    def display(self) -> str:
        return f"[model-agent non-JSON reply: {self.detail}]"


@dataclass(frozen=True, slots=True)
class MalformedReply(ModelReply):
    error_name: str

    def display(self) -> str:
        return f"[model-agent malformed reply: {self.error_name}]"


def _completion_from(raw: object) -> ModelReply:
    """Decode an OpenAI chat/completions response body into one ModelReply member.

    The endpoint sends many fields we don't use (model, created, system_fingerprint, prompt and
    total token counts); this is the one place that reads the response shape by name.
    """
    try:
        choice = raw["choices"][0]
        message = choice["message"]
        raw_content = message.get("content") or ""
        raw_reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
        finish_reason = choice.get("finish_reason") or "stop"
        usage = raw.get("usage") or {}
        completion_tokens = usage.get("completion_tokens")
    except (KeyError, IndexError, TypeError, AttributeError) as error:
        return MalformedReply(type(error).__name__)
    parsed = ParsedThinking.parse(raw_content, raw_reasoning)
    if not parsed.visible and not parsed.thinking:
        return EmptyContentReply(finish_reason)
    return ChatCompletion(
        content=parsed.visible,
        finish_reason=finish_reason,
        completion_tokens=completion_tokens,
        thinking=parsed.thinking,
    )


@dataclass(frozen=True, slots=True)
class ChatEndpoint:
    """An OpenAI-compatible chat/completions endpoint, called with one user message per request."""

    url: str
    model: str
    max_tokens: int = 1024
    temperature: float = 0.7
    timeout: float = 90.0

    def call(self, messages: list[dict]) -> ModelReply:
        """Send the full message list and decode one reply.

        `messages` carries the conversation state: a list of `{"role": "user"|"assistant",
        "content": "..."}` dicts in turn order. Ownership of the history is the caller's;
        the endpoint is stateless by design (an HTTP chat completion has no session).
        """
        payload = json.dumps({
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }).encode()
        request = urllib.request.Request(
            self.url, data=payload, headers={
                "Content-Type": "application/json",
                "User-Agent": "agent-comms-model-agent",
            })
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = json.load(response)
        except urllib.error.HTTPError as http_error:
            return HttpFailureReply(
                http_error.code, http_error.read().decode(errors="replace")[:400])
        except (urllib.error.URLError, OSError, TimeoutError) as error:
            return TransportFailureReply(type(error).__name__, str(error))
        except json.JSONDecodeError as error:
            return NonJsonReply(str(error))
        return _completion_from(raw)


@dataclass(frozen=True, slots=True)
class ModelAgent:
    """A bus thread whose replies come from one HTTP chat endpoint.

    When ``rejection_k > 1``, each inbound message is answered by generating that many
    candidates and running each through the structural verifier suite. The first
    verifier-clean candidate wins; if none are clean the candidate with the highest
    structural_reward is sent with a one-line footer naming the triggered verifiers,
    so slop is visible on the bus instead of hidden behind rejection.
    """

    name: str
    endpoint: ChatEndpoint
    root: Path
    rejection_k: int = 1
    skip_backlog: bool = True
    verifier_context: ResponseContext = ResponseContext()
    channels: frozenset[str] = frozenset()
    takeover: bool = False

    async def run(self) -> None:
        # Select the LaunchRoot BEFORE resolving the route: resolve_comms_route(None)
        # falls back to LaunchRoot.current(), which requires a selection already in scope.
        # Per-sender conversation history lives on this run's stack: a dict keyed by
        # sender name, each value a list of the chat-completions messages exchanged with
        # that sender. Without this every turn is a one-shot and the model has no sense
        # of continuity (Tristan, 2026-11-18, live probe).
        with LaunchRoot.from_path(self.root).selected():
            route = resolve_comms_route(self.root)
            comms = Comms(route)
            await self._register(comms)
            with ExitStack() as endpoint:
                # Bound as Delivery.start binds a CLI bridge (cli_bridges/shared.py): connected,
                # with this process, until it leaves. A declared thread with no process is
                # "no bridge" on the bus however fast its replies come back.
                await comms.bus.when_open(lambda: endpoint.enter_context(
                    comms.registry.external_process(
                        self.name, ProcessIdentity.capture(os.getpid()))))
                await self._serve(comms)

    async def _serve(self, comms: Comms) -> None:
        """Answer every message where it was asked: a channel post in its channel, a DM in
        the DM (`Message.exact_reply_target`). The bridge used to DM every answer back, so a
        channel @mention was answered somewhere nobody in the channel looked."""
        start = self._initial_start(comms)
        print(f"[model-agent] watching {self.name!r} -> "
              f"{self.endpoint.url} ({self.endpoint.model}) "
              f"k={self.rejection_k} start={type(start).__name__}", flush=True)
        loop = asyncio.get_running_loop()
        histories: dict[str, list[dict]] = {}
        async for message in comms.watch(self.name, after=start):
            history = histories.setdefault(message.sender, [])
            history.append({"role": "user", "content": message.body})
            print(f"[model-agent] {message.sender} -> {self.name}: "
                  f"{len(message.body)} chars (turn {len(history) // 2 + 1})",
                  flush=True)
            reply, rendered = await self._best_reply(loop, history)
            comms.messaging.send(self.name, message.exact_reply_target, rendered, InfoKind,
                                 answers=(message.reference,))
            reply.record_in(history)
            print(f"[model-agent] replied to {message.sender} with "
                  f"{type(reply).__name__}: {reply.log_summary(rendered)}", flush=True)

    async def _register(self, comms: Comms) -> None:
        """Declare this thread as a CLI endpoint in its channels before watching or sending.

        An unregistered name has no inbox and no sender (Tristan, 2026-11-18: "it shodl
        autoregister"). A name held by another kind of thread, such as a stopped fork, is not an
        endpoint: the channel delivers nothing to it and the bridge cannot hold it, so the bridge
        stops unless it was started to take the name over (comms_serve_model replace=true), and
        then deletes that stopped declaration first; a live one refuses its deletion. An existing
        endpoint keeps its identity and joins any requested channel it is missing."""
        snapshot = comms.registry.snapshot()
        if snapshot.taken(self.name):
            current = comms.registry.require(self.name)
            try:
                thread = current.as_external_endpoint(self.channels)
            except RelationViolationError:
                if not self.takeover:
                    print(f"[model-agent] {self.name!r} is a {type(current).__name__} thread, "
                          "not a CLI endpoint; restart with --takeover "
                          "(comms_serve_model replace=true) to replace it", flush=True)
                    raise SystemExit(2)
                await comms.bus.when_open(partial(comms.threads.delete, self.name))
                print(f"[model-agent] deleted the stopped {type(current).__name__} declaration "
                      f"of {self.name!r} to take the name over", flush=True)
                thread = ExternalThread(self.name, self.channels, Checkout(Path.cwd()))
            else:
                if thread is current:
                    return
        else:
            thread = ExternalThread(self.name, self.channels, Checkout(Path.cwd()))
        await comms.bus.when_open(partial(comms.registry.declare, thread))
        print(f"[model-agent] registered {self.name!r} in {sorted(thread.tags)}", flush=True)

    def _initial_start(self, comms: Comms) -> InboxStart:
        """Where the first inbox read begins: right after the newest message this inbox already
        has an answer to, so whatever arrived while the bridge was down is answered next.

        Every reply names the message it answers (`answers=`), so the bus itself records which
        inbox messages are done; the bridge keeps no cursor of its own. A restart used to start
        at the inbox tail, and every message sent during the restart was silently skipped
        (Tristan, 2026-11-18: "whydoesn't it repond to my dms to it"). An inbox with no
        recorded answer at all (a thread whose earlier replies predate answer links) starts at
        its tail rather than replaying its whole history at the model.

        With skip_backlog=False, every message is replayed from the start.
        """
        if not self.skip_backlog:
            return TakenInputStart()
        snap = comms.registry.snapshot()
        references = []
        start: InboxStart = TakenInputStart()
        while True:
            page = comms.bus.inbox_at(self.name, snap, after=start)
            references.extend(message.reference for message in page.page.messages)
            if not page.page.messages or page.remaining == 0:
                break
            start = AfterMessageStart(page.page.messages[-1].reference)
        if not references:
            return TakenInputStart()
        unanswered = set(comms.bus.bus.unanswered_references(
            tuple(reference.seq for reference in references)))
        answered = [reference for reference in references if reference not in unanswered]
        return AfterMessageStart((answered or references)[-1])

    async def _best_reply(self, loop: asyncio.AbstractEventLoop,
                          messages: list[dict[str, str]]) -> tuple[ModelReply, str]:
        """Return (reply-member, text-to-send).

        Single-pass (k=1): the reply's display() goes straight through.
        Multi-pass (k>1): generate k candidates, pick the first verifier-clean one. If
        none are clean, pick the highest-structural-reward candidate and append a
        one-line footer naming what fired so the slop is loud on the bus.
        """
        if self.rejection_k <= 1:
            reply = await loop.run_in_executor(None, self.endpoint.call, messages)
            return reply, reply.display()

        outcome: SamplingOutcome = NoCandidate()
        for attempt in range(self.rejection_k):
            reply = await loop.run_in_executor(None, self.endpoint.call, messages)
            outcome = reply.sample(attempt, self.rejection_k, self.verifier_context, outcome)
            if outcome.settled:
                break
        return outcome.finish(self.rejection_k)


def main() -> None:
    MODEL_AGENT_PROCESS.publish()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True,
                        help="Thread name on the bus this agent speaks from")
    parser.add_argument("--endpoint", required=True,
                        help="OpenAI-compatible chat/completions URL")
    parser.add_argument("--model", required=True, help="Model id the endpoint expects")
    parser.add_argument("--max-tokens", type=int, default=1024)
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument("--timeout", type=float, default=90.0,
                        help="HTTP request timeout in seconds")
    parser.add_argument("--root", type=Path,
                        default=Path(os.environ.get(LaunchRoot.environment_name,
                                                    Path.home() / ".agent-comms")),
                        help=(f"Comms bus root; defaults to ${LaunchRoot.environment_name} "
                              "or ~/.agent-comms"))
    parser.add_argument("--rejection-k", type=int, default=1,
                        help=("Generate K candidates per inbound message and send the "
                              "first verifier-clean one. k=1 (default) disables sampling."))
    parser.add_argument("--include-backlog", action="store_true",
                        help=("Replay every untaken message in the inbox on connect. Default "
                              "skips the backlog: a fresh daemon connect answers only messages "
                              "that arrive after it starts, since it doesn't own what it forwards."))
    parser.add_argument("--channels", default="",
                        help=("Comma-separated channels the thread joins, e.g. '#model_test'."))
    parser.add_argument("--takeover", action="store_true",
                        help=("Delete a stopped non-endpoint thread holding --name (a fork, a "
                              "stopped native thread) and declare this endpoint in its place."))
    args = parser.parse_args()
    # The bridge declares no tools to the endpoint, so the verifier should fire on
    # any <tool_call> in a candidate. token_budget mirrors max_tokens so a truncated
    # candidate (ran out of tokens) trips LengthRunawayVerifier.
    verifier_context = ResponseContext(
        tools_declared=False,
        token_budget=args.max_tokens,
    )
    agent = ModelAgent(
        name=args.name,
        endpoint=ChatEndpoint(
            url=args.endpoint, model=args.model, max_tokens=args.max_tokens,
            temperature=args.temperature, timeout=args.timeout,
        ),
        root=args.root.expanduser().absolute(),
        rejection_k=max(args.rejection_k, 1),
        skip_backlog=not args.include_backlog,
        verifier_context=verifier_context,
        channels=frozenset(name.strip().lstrip("#") for name in args.channels.split(",")
                           if name.strip().lstrip("#")),
        takeover=args.takeover,
    )
    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        print("[model-agent] stopped", flush=True)
        sys.exit(0)


if __name__ == "__main__":
    main()
