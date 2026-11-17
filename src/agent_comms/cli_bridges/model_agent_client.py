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

from agent_comms.comms import Comms
from agent_comms.execution.launch_root import LaunchRoot
from agent_comms.installer.active_route import resolve_comms_route
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

    def call(self, body: str) -> ModelReply:
        payload = json.dumps({
            "model": self.model,
            "messages": [{"role": "user", "content": body}],
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }).encode()
        request = urllib.request.Request(
            self.url, data=payload, headers={"Content-Type": "application/json"})
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
    """A bus thread whose replies come from one HTTP chat endpoint."""

    name: str
    endpoint: ChatEndpoint

    async def run(self) -> None:
        route = resolve_comms_route()
        with LaunchRoot.from_path(route.root).selected():
            comms = Comms(route)
            print(f"[model-agent] watching {self.name!r} -> "
                  f"{self.endpoint.url} ({self.endpoint.model})", flush=True)
            loop = asyncio.get_running_loop()
            async for message in comms.watch(self.name):
                print(f"[model-agent] {message.sender} -> {self.name}: "
                      f"{len(message.body)} chars", flush=True)
                reply = await loop.run_in_executor(None, self.endpoint.call, message.body)
                rendered = reply.display()
                comms.messaging.send(self.name, message.sender, rendered, InfoKind)
                print(f"[model-agent] replied to {message.sender} with "
                      f"{type(reply).__name__}: {len(rendered)} chars", flush=True)


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
    args = parser.parse_args()
    agent = ModelAgent(
        name=args.name,
        endpoint=ChatEndpoint(
            url=args.endpoint, model=args.model, max_tokens=args.max_tokens,
            temperature=args.temperature, timeout=args.timeout,
        ),
    )
    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        print("[model-agent] stopped", flush=True)
        sys.exit(0)


if __name__ == "__main__":
    main()
