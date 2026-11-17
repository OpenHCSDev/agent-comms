"""agent-comms-model-agent: a bus thread's end of an OpenAI-compatible HTTP chat endpoint.

Runs on the bus host. Takes one thread name on the bus (what senders @mention and DM) and one
model endpoint; every message the thread receives is turned into a one-user-message chat request,
and the completion comes straight back as a reply from the thread. No buffering, no hand forward:
Comms.watch yields each message once and we send one reply, so there is no "me-as-bridge" turn
between the sender and the model.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.error
import urllib.request

from agent_comms.comms import Comms
from agent_comms.execution.launch_root import LaunchRoot
from agent_comms.installer.active_route import resolve_comms_route
from agent_comms.messaging.messages import InfoKind


def _post(endpoint: str, model: str, body: str, *, max_tokens: int, temperature: float,
          timeout: float) -> str:
    """Call the chat endpoint once and return the completion text, or an error string the
    sender can read on the bus so a transport failure is never silent."""
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": body}],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }).encode()
    request = urllib.request.Request(
        endpoint, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            reply = json.load(response)
    except urllib.error.HTTPError as http_error:
        detail = http_error.read().decode(errors="replace")[:400]
        return f"[model-agent http {http_error.code}: {detail}]"
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        return f"[model-agent transport error: {type(error).__name__}: {error}]"
    except json.JSONDecodeError as error:
        return f"[model-agent non-JSON reply: {error}]"
    try:
        choice = reply["choices"][0]
        content = choice["message"]["content"]
        finish = choice.get("finish_reason", "stop")
        usage = reply.get("usage", {}) or {}
        completion_tokens = usage.get("completion_tokens")
    except (KeyError, IndexError, TypeError) as error:
        return f"[model-agent malformed reply: {type(error).__name__}]"
    if not content:
        return f"[model-agent empty content, finish={finish}]"
    footer = f"\n\n---\n_{completion_tokens or '?'} tokens, finish {finish}_"
    return f"{content}{footer}"


async def _run(name: str, endpoint: str, model: str, *, max_tokens: int, temperature: float,
               timeout: float) -> None:
    route = resolve_comms_route()
    with LaunchRoot.from_path(route.root).selected():
        comms = Comms(route)
        print(f"[model-agent] watching {name!r} -> {endpoint} ({model})", flush=True)
        async for message in comms.watch(name):
            sender = message.sender
            body = message.body
            print(f"[model-agent] {sender} -> {name}: {len(body)} chars", flush=True)
            loop = asyncio.get_running_loop()
            reply = await loop.run_in_executor(
                None, _post, endpoint, model, body,
                max_tokens, temperature, timeout,
            )
            comms.messaging.send(name, sender, reply, InfoKind)
            print(f"[model-agent] replied to {sender}: {len(reply)} chars", flush=True)


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
    try:
        asyncio.run(_run(args.name, args.endpoint, args.model,
                         max_tokens=args.max_tokens,
                         temperature=args.temperature,
                         timeout=args.timeout))
    except KeyboardInterrupt:
        print("[model-agent] stopped", flush=True)
        sys.exit(0)


if __name__ == "__main__":
    main()
