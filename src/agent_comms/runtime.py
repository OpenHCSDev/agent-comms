"""Local attachment to a thread's single executing ACP owner.

Opening another UI subscribes to the owner; it never launches another backend
or changes the registry PID. The socket is scoped to the wire and owner PID.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, cast


def socket_path(root: Path, pid: int) -> Path:
    path = root / "runtime" / f"{pid}.sock"
    if len(os.fsencode(path)) < 100:
        return path
    # Darwin's temporary paths routinely exceed sockaddr_un.sun_path.
    digest = hashlib.sha256(os.fsencode(root.resolve())).hexdigest()[:24]
    return Path(tempfile.gettempdir()) / f"ac-{digest}-{pid}.sock"


class SocketClient:
    def __init__(self, writer: asyncio.StreamWriter):
        self.writer = writer

    async def session_update(self, *, session_id: str, update: Any) -> None:
        self.writer.write(
            (
                json.dumps({"update": update.model_dump(by_alias=True, exclude_none=True)}) + "\n"
            ).encode()
        )
        await self.writer.drain()


class RuntimeServer:
    def __init__(self, agent: Any):
        self.agent = agent
        self.server: asyncio.Server | None = None
        self.clients: dict[str, set[SocketClient]] = {}
        self.path = socket_path(agent._comms.root, os.getpid())

    async def start(self) -> None:
        if self.server is not None or os.name == "nt":
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.server = await asyncio.start_unix_server(
            self.handle, path=self.path, limit=8 * 1024 * 1024
        )

    async def session_update(self, *, session_id: str, update: Any) -> None:
        if self.agent._client is not None:
            await self.agent._client.session_update(session_id=session_id, update=update)
        for client in tuple(self.clients.get(session_id, ())):
            try:
                await client.session_update(session_id=session_id, update=update)
            except (ConnectionError, OSError):
                self.clients[session_id].discard(client)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        client = SocketClient(writer)
        session_id = None
        try:
            request = json.loads(await reader.readline())
            name = self.agent._comms.registry.require(request["thread"]).name
            session_id = next(
                key
                for key, value in self.agent._sessions.items()
                if self.agent._comms.registry.canonical_name(value) == name
            )
            action = request["action"]
            if action == "subscribe":
                self.clients.setdefault(session_id, set()).add(client)
                await self.agent.emit_session_identity(session_id, name, client=client)
                await self.agent._replay_transcript(
                    session_id,
                    name,
                    client=client,
                    snapshots=request.get("transcriptSnapshots") is True,
                    diffs=request.get("transcriptDiffs") is True,
                )
                await self.agent.replay_turn_state(session_id, client=client)
                await self.agent.replay_unknown_inputs(session_id, client=client)
                writer.write(
                    (json.dumps({"ready": self.agent._session_metadata(name)}) + "\n").encode()
                )
                await writer.drain()
                await reader.read()
            elif action == "prompt":
                result = await self.agent.prompt(
                    session_id, request["prompt"], field_meta=request.get("meta") or {}
                )
                writer.write(
                    (
                        json.dumps({"result": result.model_dump(by_alias=True, exclude_none=True)})
                        + "\n"
                    ).encode()
                )
                await writer.drain()
            elif action == "cancel":
                await self.agent.cancel(session_id)
                writer.write(b'{"result": {}}\n')
                await writer.drain()
            elif action == "compact":
                handler = getattr(self.agent, "compact_context", None)
                if handler is None:
                    from .manual_compaction_bridge import compact_context

                    result = await compact_context(
                        self.agent, session_id, request.get("instructions")
                    )
                else:
                    result = await handler(session_id, request.get("instructions"))
                writer.write((json.dumps({"result": result}) + "\n").encode())
                await writer.drain()
        except (Exception, asyncio.CancelledError) as error:
            if not isinstance(error, asyncio.CancelledError):
                try:
                    writer.write((json.dumps({"error": str(error)}) + "\n").encode())
                    await writer.drain()
                except (ConnectionError, OSError):
                    pass
        finally:
            if session_id is not None:
                self.clients.get(session_id, set()).discard(client)
            writer.close()

    async def close(self) -> None:
        for clients in self.clients.values():
            for client in clients:
                client.writer.close()
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.path.unlink(missing_ok=True)


class RuntimeProxy:
    def __init__(self, agent: Any, session_id: str, path: Path):
        self.agent = agent
        self.session_id = session_id
        self.path = path
        self.writer: asyncio.StreamWriter | None = None
        self.task: asyncio.Task[None] | None = None

    async def subscribe(self) -> dict[str, Any]:
        # The registry ownership change precedes socket startup by a few event
        # loop ticks. Retry attachment, never claim a second owner in that gap.
        deadline = asyncio.get_running_loop().time() + 2
        while True:
            try:
                reader, self.writer = await asyncio.open_unix_connection(
                    self.path, limit=8 * 1024 * 1024
                )
                break
            except (FileNotFoundError, ConnectionRefusedError):
                if asyncio.get_running_loop().time() >= deadline:
                    raise
                await asyncio.sleep(0.05)
        self.writer.write(
            (
                json.dumps(
                    {
                        "action": "subscribe",
                        "thread": self.session_id,
                        "transcriptSnapshots": self.agent._transcript_snapshots,
                        "transcriptDiffs": self.agent._transcript_diffs,
                    }
                )
                + "\n"
            ).encode()
        )
        await self.writer.drain()
        while line := await reader.readline():
            data = json.loads(line)
            if "error" in data:
                raise RuntimeError(data["error"])
            if "ready" in data:
                self.task = asyncio.create_task(self.forward(reader))
                return cast(dict[str, Any], data["ready"])
            await self.update(data)
        raise RuntimeError("Thread owner disconnected during attachment")

    async def update(self, data: dict[str, Any]) -> None:
        if "update" in data and self.agent._client is not None:
            await self.agent._client.session_update(
                session_id=self.session_id, update=data["update"]
            )

    async def forward(self, reader: asyncio.StreamReader) -> None:
        while line := await reader.readline():
            await self.update(json.loads(line))

    async def request(self, action: str, **kwargs: Any) -> dict[str, Any]:
        reader, writer = await asyncio.open_unix_connection(self.path, limit=8 * 1024 * 1024)
        try:
            writer.write(
                (
                    json.dumps({"action": action, "thread": self.session_id, **kwargs}) + "\n"
                ).encode()
            )
            await writer.drain()
            data = json.loads(await reader.readline())
            if "error" in data:
                raise RuntimeError(data["error"])
            return cast(dict[str, Any], data["result"])
        finally:
            writer.close()

    async def close(self) -> None:
        if self.writer is not None:
            self.writer.close()
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
