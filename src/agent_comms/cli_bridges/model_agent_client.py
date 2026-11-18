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
import sys
import urllib.error
import urllib.request
from abc import abstractmethod
from dataclasses import dataclass

import os
from pathlib import Path

from agent_comms.comms import Comms
from agent_comms.conversational_slop import ResponseContext, SlopVerdict, verify_all
from agent_comms.execution.launch_root import LaunchRoot
from agent_comms.installer.active_route import resolve_comms_route
from agent_comms.messaging.agent_inbox import TakenInputStart
from agent_comms.messaging.message_reference import AfterMessageStart, InboxStart
from agent_comms.messaging.messages import InfoKind
from agent_comms.nominal.declared_family import DeclaredFamily


class ModelReply(DeclaredFamily, affix="Reply"):
    """Everything a chat/completions call can produce: one completion, or one named failure."""

    @abstractmethod
    def display(self) -> str:
        """The text the sender sees on the bus."""


@dataclass(frozen=True, slots=True)
class ChatCompletion(ModelReply):
    content: str
    finish_reason: str
    completion_tokens: int | None

    def display(self) -> str:
        return (f"{self.content}\n\n---\n"
                f"_{self.completion_tokens or '?'} tokens, finish {self.finish_reason}_")


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
        content = choice["message"]["content"] or ""
        finish_reason = choice.get("finish_reason") or "stop"
        usage = raw.get("usage") or {}
        completion_tokens = usage.get("completion_tokens")
    except (KeyError, IndexError, TypeError) as error:
        return MalformedReply(type(error).__name__)
    if not content:
        return EmptyContentReply(finish_reason)
    return ChatCompletion(content=content, finish_reason=finish_reason,
                          completion_tokens=completion_tokens)


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
                comms.messaging.send(self.name, message.sender, rendered, InfoKind)
                if isinstance(reply, ChatCompletion):
                    history.append({"role": "assistant", "content": reply.content})
                # Success types log a char count; failure types log their reply content
                # inline so the daemon stdout shows WHAT failed without a bus check.
                suffix = (f"{len(rendered)} chars" if isinstance(reply, ChatCompletion)
                          else reply.display())
                print(f"[model-agent] replied to {message.sender} with "
                      f"{type(reply).__name__}: {suffix}", flush=True)

    def _initial_start(self, comms: Comms) -> InboxStart:
        """Where the first inbox read begins.

        Default (skip_backlog): read the current inbox once to find its tail and start past it.
        A daemon connecting fresh should answer messages that arrive next, not replay everything
        the thread has accumulated — the model-agent bridge doesn't mark messages taken, so
        without this every reconnect replays the full inbox and fires the model on history that
        was already handled by whoever was there before.

        With skip_backlog=False, falls back to TakenInputStart() for the classic replay-everything
        behavior.
        """
        if not self.skip_backlog:
            return TakenInputStart()
        page = comms.bus.inbox_at(self.name, comms.registry.snapshot(), after=TakenInputStart())
        if not page.page.messages:
            return TakenInputStart()
        return AfterMessageStart(page.page.messages[-1].reference)

    async def _best_reply(self, loop: asyncio.AbstractEventLoop,
                          messages: list[dict]) -> tuple[ModelReply, str]:
        """Return (reply-member, text-to-send).

        Single-pass (k=1): the reply's display() goes straight through.
        Multi-pass (k>1): generate k candidates, pick the first verifier-clean one. If
        none are clean, pick the highest-structural-reward candidate and append a
        one-line footer naming what fired so the slop is loud on the bus.
        """
        if self.rejection_k <= 1:
            reply = await loop.run_in_executor(None, self.endpoint.call, messages)
            return reply, reply.display()

        best: tuple[ModelReply, SlopVerdict] | None = None
        for attempt in range(self.rejection_k):
            reply = await loop.run_in_executor(None, self.endpoint.call, messages)
            if not isinstance(reply, ChatCompletion):
                # Transport/decode failures don't get rejection-sampled; a pod that is
                # 500ing will 500 every candidate, so return the first non-success
                # immediately with its display() text.
                return reply, reply.display()
            verdict = verify_all(reply.content, self.verifier_context)
            print(f"[model-agent]   candidate {attempt + 1}/{self.rejection_k}: "
                  f"reward={verdict.structural_reward:.2f} triggered={verdict.triggered}",
                  flush=True)
            if verdict.passed:
                return reply, reply.display()
            if best is None or verdict.structural_reward > best[1].structural_reward:
                best = (reply, verdict)

        assert best is not None  # k > 1 means we entered the loop at least once
        reply, verdict = best
        footer = (f"\n\n> _rejection-sampled {self.rejection_k}×, best reward "
                  f"{verdict.structural_reward:.2f}; triggered {verdict.triggered}_")
        return reply, reply.display() + footer


def main() -> None:
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
    )
    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        print("[model-agent] stopped", flush=True)
        sys.exit(0)


if __name__ == "__main__":
    main()
