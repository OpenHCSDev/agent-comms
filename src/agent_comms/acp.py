"""OpenHCS agent communications — ACP server.

Implements a minimal Agent Client Protocol agent over stdio so that ACP
clients (Toad, Zed, VS Code) can attach to the coordination wire natively.
Pure standard library.

Protocol shape (see agentclientprotocol.com):

- JSON-RPC 2.0, one message per line on stdin/stdout.
- ``initialize`` -> respond with protocol version and agent capabilities.
- ``session/new`` -> declare a thread for the session, return sessionId.
- ``session/prompt`` -> declare a message in the bus for that thread, drain
  the thread's inbox back as ``session/update`` notifications (agent message
  chunks), respond with ``stopReason: endTurn``.

The server does not own semantics; it delegates everything to Comms
operations. Unregistered references surface as JSON-RPC errors.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .operations import Comms, wire

PROTOCOL_VERSION = 1

JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603

LineReader = Iterable[str]


class AcpServer:
    """One ACP agent bound to one Comms wire."""

    def __init__(
        self,
        comms: Comms,
        reader: LineReader | None = None,
        writer: Any | None = None,
    ):
        self._comms = comms
        self._reader = reader if reader is not None else sys.stdin
        self._writer = writer if writer is not None else sys.stdout
        self._sessions: dict[str, str] = {}
        self._next_session = 0

    # ─── Wire protocol ────────────────────────────────────────────────────────

    def serve(self) -> None:
        """Read requests until EOF; write exactly one response per request."""
        for line in self._reader:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                self._write(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": JSONRPC_PARSE_ERROR, "message": "Parse error"},
                    }
                )
                continue
            if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                self._write(
                    {
                        "jsonrpc": "2.0",
                        "id": message.get("id") if isinstance(message, dict) else None,
                        "error": {
                            "code": JSONRPC_INVALID_REQUEST,
                            "message": "Not a JSON-RPC 2.0 request",
                        },
                    }
                )
                continue
            response = self.handle(message)
            if response is not None:
                self._write(response)

    def _write(self, payload: Mapping[str, Any]) -> None:
        self._writer.write(json.dumps(payload) + "\n")
        self._writer.flush()

    # ─── Dispatch ─────────────────────────────────────────────────────────────

    def handle(self, message: Mapping[str, Any]) -> dict[str, Any] | None:
        method = str(message.get("method") or "")
        request_id = message.get("id")
        params = message.get("params") or {}

        handler = {
            "initialize": self._on_initialize,
            "session/new": self._on_session_new,
            "session/prompt": self._on_session_prompt,
        }.get(method)

        if handler is None:
            # Notifications (no id) never get responses; unknown requests do.
            if request_id is None:
                return None
            return self._error(request_id, JSONRPC_METHOD_NOT_FOUND, f"Method not found: {method}")

        try:
            result = handler(params)
        except (ValueError, KeyError) as exc:
            if request_id is None:
                return None
            return self._error(request_id, JSONRPC_INVALID_PARAMS, str(exc))
        except Exception as exc:  # noqa: BLE001 - surface to client, keep serving
            if request_id is None:
                return None
            return self._error(request_id, JSONRPC_INTERNAL_ERROR, str(exc))

        if request_id is None:
            return None
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    # ─── ACP methods ──────────────────────────────────────────────────────────

    def _on_initialize(self, params: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "agentCapabilities": {
                "loadSession": False,
                "promptCapabilities": {"audio": False, "embeddedContext": False},
            },
            "authMethods": [],
        }

    def _on_session_new(self, params: Mapping[str, Any]) -> dict[str, Any]:
        cwd = params.get("cwd") or Path.cwd().as_posix()
        thread_name = params.get("thread") or params.get("agentId") or "acp-session"
        from .declarations import Thread

        thread = Thread(
            name=thread_name,
            tags=frozenset({"acp"}),
            worktree=cwd,
            parent=params.get("parent"),
            task=params.get("task"),
        )
        self._comms.register(thread)
        self._next_session += 1
        session_id = f"s{self._next_session}"
        self._sessions[session_id] = thread_name
        return {"sessionId": session_id}

    def _on_session_prompt(self, params: Mapping[str, Any]) -> dict[str, Any]:
        session_id = params.get("sessionId")
        if session_id not in self._sessions:
            raise ValueError(f"Unknown sessionId: {session_id!r}")
        thread_name = self._sessions[session_id]
        self._comms.registry.require(thread_name)
        from .declarations import ThreadStatus

        if self._comms.registry.status(thread_name) is ThreadStatus.STOPPED:
            raise ValueError(f"Thread {thread_name!r} is stopped; refusing prompt.")

        prompt_text = self._prompt_text(params.get("prompt"))
        self._comms.send(
            sender=thread_name,
            target="broadcast",
            body=prompt_text,
        )

        for message in self._comms.inbox(thread_name):
            self._write(
                {
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": session_id,
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {
                                "type": "text",
                                "text": f"[{message.sender}] {message.body}",
                            },
                        },
                    },
                }
            )
        self._comms.acknowledge(thread_name)
        return {"stopReason": "endTurn"}

    @staticmethod
    def _prompt_text(prompt: Any) -> str:
        if prompt is None:
            return ""
        if isinstance(prompt, str):
            return prompt
        if isinstance(prompt, Sequence):
            chunks = []
            for block in prompt:
                if isinstance(block, Mapping):
                    chunks.append(str(block.get("text", "")))
            return "\n".join(chunk for chunk in chunks if chunk)
        return str(prompt)


def main() -> int:
    comms = wire()
    AcpServer(comms).serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
