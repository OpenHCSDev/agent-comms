"""Local attachment to a thread's single executing ACP owner.

Opening another UI subscribes to the owner; it never launches another backend
or changes the registry PID. The socket is scoped to the wire and owner PID.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import tempfile
from contextlib import suppress
from contextvars import ContextVar
from pathlib import Path
from typing import Any, cast

from acp.schema import RequestPermissionResponse


def socket_path(root: Path, pid: int) -> Path:
    path = root / "runtime" / f"{pid}.sock"
    if len(os.fsencode(path)) < 100:
        return path
    # Darwin's temporary paths routinely exceed sockaddr_un.sun_path.
    digest = hashlib.sha256(os.fsencode(root.resolve())).hexdigest()[:24]
    return Path(tempfile.gettempdir()) / f"ac-{digest}-{pid}.sock"


UNBOUND_CONTROLLER = object()
ACP_PERMISSION_TIMEOUT_SECONDS = 14.0


class SocketClient:
    def __init__(self, writer: asyncio.StreamWriter):
        self.writer = writer
        self.token = secrets.token_hex(32)
        self.pending: dict[str, asyncio.Future[dict[str, Any]]] = {}

    async def permission(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        if len(self.pending) >= 4:
            return None
        request_id = secrets.token_hex(16)
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        task = asyncio.current_task()
        try:
            self.writer.write(
                (
                    json.dumps(
                        {
                            "permissionRequest": {
                                "id": request_id,
                                **payload,
                            }
                        }
                    )
                    + "\n"
                ).encode()
            )
            await asyncio.wait_for(self.writer.drain(), timeout=2)
            # Cancellation can race wait_for's completion of a fast drain.
            # A pending owner cancel must not become a 15-second dialog wait.
            if task is not None and task.cancelling():
                raise asyncio.CancelledError
            reply = await asyncio.wait_for(future, timeout=15)
            if task is not None and task.cancelling():
                raise asyncio.CancelledError
            return reply
        except (OSError, ConnectionError, TimeoutError):
            if task is not None and task.cancelling():
                raise asyncio.CancelledError from None
            return None
        finally:
            self.pending.pop(request_id, None)
            if not future.done():
                future.cancel()

    def disconnected(self) -> None:
        for future in self.pending.values():
            if not future.done():
                # Connection loss is denial, not cancellation of the *owner*
                # turn task. Explicit ACP cancellation must still propagate.
                future.set_result({"outcome": "cancelled"})
        self.pending.clear()

    async def session_update(self, *, session_id: str, update: Any) -> None:
        self.writer.write(
            (
                json.dumps({"update": update.model_dump(by_alias=True, exclude_none=True)}) + "\n"
            ).encode()
        )
        await self.writer.drain()


class OwnerIdentityChangedError(RuntimeError):
    """A saved attachment must not follow a reused thread name."""


def _owner_error(error: Exception) -> dict[str, Any]:
    from acp.exceptions import RequestError

    if isinstance(error, RequestError):
        data = error.data if isinstance(error.data, dict) else {}
        reason = data.get("details") or data.get("reason")
        # Keep the string for already-running proxies while preserving the
        # original JSON-RPC error for clients which understand this envelope.
        return {
            "error": reason if isinstance(reason, str) and reason else str(error),
            "rpcError": error.to_error_obj(),
        }
    return {"error": str(error)}


def _raise_owner_error(data: dict[str, Any]) -> None:
    if "error" not in data:
        return
    from acp.exceptions import RequestError

    rpc = data.get("rpcError")
    if (
        isinstance(rpc, dict)
        and type(rpc.get("code")) is int
        and isinstance(rpc.get("message"), str)
    ):
        raise RequestError(rpc["code"], rpc["message"], rpc.get("data"))
    raise RuntimeError(data["error"])


class RuntimeServer:
    def __init__(self, agent: Any):
        self.agent = agent
        self.server: asyncio.Server | None = None
        self.clients: dict[str, set[SocketClient]] = {}
        # Set only while handling a prompt on this particular attachment.
        # Never publish this bearer to ACP metadata, the transcript or other sockets.
        self.controller: ContextVar[SocketClient | None | object] = ContextVar(
            "runtime_permission_controller", default=UNBOUND_CONTROLLER
        )
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

    def is_controller(self, session_id: str, controller: SocketClient) -> bool:
        return controller in self.clients.get(session_id, ()) and not controller.writer.is_closing()

    async def request_permission(
        self, session_id: str, controller: SocketClient, payload: dict[str, Any]
    ) -> dict[str, Any] | None:
        if not self.is_controller(session_id, controller):
            return None
        reply = await controller.permission(payload)
        return reply if self.is_controller(session_id, controller) else None

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        client = SocketClient(writer)
        session_id = None
        try:
            request = json.loads(await reader.readline())
            owner = self.agent._comms.registry.require(request["thread"])
            name = owner.name
            if owner.pid != os.getpid() or not self.agent._comms.registry.status(name).running:
                raise RuntimeError("This process no longer owns the thread.")
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
                config_options = await self.agent._config_options(name)
                metadata = self.agent._session_metadata(name)
                writer.write(
                    (
                        json.dumps(
                            {
                                "controllerToken": client.token,
                                "ready": {
                                    **metadata,
                                    "configOptions": [
                                        option.model_dump(by_alias=True, exclude_none=True)
                                        for option in config_options
                                    ],
                                },
                            }
                        )
                        + "\n"
                    ).encode()
                )
                await writer.drain()
                while line := await reader.readline():
                    response = json.loads(line)
                    receipt = (
                        response.get("permissionResponse") if isinstance(response, dict) else None
                    )
                    if not isinstance(receipt, dict):
                        continue
                    reply_id = receipt.get("id")
                    pending = client.pending.get(reply_id) if isinstance(reply_id, str) else None
                    if pending is not None and not pending.done():
                        pending.set_result(receipt)
            elif action == "prompt":
                controller = next(
                    (
                        subscriber
                        for subscriber in self.clients.get(session_id, ())
                        if subscriber.token == request.get("controllerToken")
                        and not subscriber.writer.is_closing()
                    ),
                    None,
                )
                context = self.controller.set(controller)
                try:
                    result = await self.agent.prompt(
                        session_id, request["prompt"], field_meta=request.get("meta") or {}
                    )
                finally:
                    self.controller.reset(context)
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
            elif action == "set_config_option":
                result = await self.agent.set_config_option(
                    request["config_id"], session_id, request["value"]
                )
                writer.write(
                    (
                        json.dumps({"result": result.model_dump(by_alias=True, exclude_none=True)})
                        + "\n"
                    ).encode()
                )
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
            elif action == "input_dispositions":
                include_history = request.get("include_history", False)
                if type(include_history) is not bool:
                    raise ValueError("include_history must be a boolean.")
                result = self.agent._comms.input_delivery(
                    name,
                    include_history=include_history,
                    awaiting_keys=self.agent.awaiting_input_keys(session_id),
                )
                writer.write((json.dumps({"result": result}) + "\n").encode())
                await writer.drain()
            elif action == "dismiss_historical_inputs":
                result = self.agent._comms.dismiss_historical_inputs(
                    name, awaiting_keys=self.agent.awaiting_input_keys(session_id)
                )
                await self.agent.emit_input_delivery_changed(session_id)
                writer.write((json.dumps({"result": result}) + "\n").encode())
                await writer.drain()
            elif action == "goal_history":
                from dataclasses import asdict

                goal_id = request.get("goal_id")
                if goal_id is not None and not isinstance(goal_id, str):
                    raise ValueError("Goal identity must be a string.")
                history = self.agent._comms.goal_history(name, goal_id=goal_id)
                writer.write(
                    (
                        json.dumps({"result": {"history": [asdict(row) for row in history]}}) + "\n"
                    ).encode()
                )
                await writer.drain()
            elif action in {"goal_snapshot", "edit_goal", "update_goal"}:
                from dataclasses import asdict

                if action != "goal_snapshot":
                    goal_id = request.get("goal_id")
                    revision = request.get("expected_revision")
                    if type(goal_id) is not str or type(revision) is not int:
                        raise ValueError("A goal identity and revision are required for updating.")
                    if action == "edit_goal":
                        text = request.get("text")
                        if not isinstance(text, str) or not text.strip():
                            raise ValueError("A goal requires text.")
                        await self.agent.edit_goal(session_id, goal_id, revision, text)
                    else:
                        await self.agent.update_goal(
                            session_id, request.get("status"), goal_id, revision
                        )
                goal, execution = self.agent._comms.goal_snapshot(name)
                writer.write(
                    (
                        json.dumps(
                            {
                                "result": {
                                    "goal": asdict(goal) if goal is not None else None,
                                    "goalExecution": (
                                        asdict(execution) if execution is not None else None
                                    ),
                                }
                            }
                        )
                        + "\n"
                    ).encode()
                )
                await writer.drain()
            elif action == "retry_goal":
                goal_id = request.get("goal_id")
                revision = request.get("expected_revision")
                if type(goal_id) is not str or type(revision) is not int:
                    raise ValueError("A goal identity and revision are required for retry.")
                from dataclasses import asdict

                goal = await self.agent.retry_goal(session_id, goal_id, revision)
                writer.write((json.dumps({"result": {"goal": asdict(goal)}}) + "\n").encode())
                await writer.drain()
            elif action == "set_goal":
                text = request.get("text")
                if not isinstance(text, str) or not text.strip():
                    raise ValueError("A goal requires text.")
                from dataclasses import asdict

                goal = await self.agent.set_goal(session_id, text)
                writer.write((json.dumps({"result": {"goal": asdict(goal)}}) + "\n").encode())
                await writer.drain()
        except (Exception, asyncio.CancelledError) as error:
            if not isinstance(error, asyncio.CancelledError):
                try:
                    writer.write((json.dumps(_owner_error(error)) + "\n").encode())
                    await writer.drain()
                except (ConnectionError, OSError):
                    pass
        finally:
            if session_id is not None:
                self.clients.get(session_id, set()).discard(client)
            client.disconnected()
            writer.close()

    async def close(self) -> None:
        for clients in self.clients.values():
            for client in clients:
                client.disconnected()
                client.writer.close()
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.path.unlink(missing_ok=True)


def _present_cursor_session(metadata: dict[str, Any], session_id: str) -> dict[str, Any]:
    """Map trusted owner cursor metadata to this ACP attachment's session ID.

    A permanent alias can be the client's sessionId while the owner process
    emits its canonical sessionId. Rewrite only this presentation coordinate;
    never alter the owner epoch, revision, status, or native proof fields.
    """
    agent_meta = metadata.get("agentComms")
    if not isinstance(agent_meta, dict):
        field_meta = metadata.get("_meta")
        agent_meta = field_meta.get("agentComms") if isinstance(field_meta, dict) else None
    if isinstance(agent_meta, dict):
        cursor = agent_meta.get("privateNativeCursor")
        if isinstance(cursor, dict) and cursor.get("version") == 1:
            scope = cursor.get("scope")
            if isinstance(scope, dict) and isinstance(scope.get("sessionId"), str):
                scope["sessionId"] = session_id
        # A canonical owner socket also serves permanent aliases. Change only
        # the attachment coordinate; leave exact IDs, epochs and revisions.
        for key in ("queueBinding", "queueState", "inputStarted"):
            value = agent_meta.get(key)
            if not isinstance(value, dict) or value.get("version") != 1:
                continue
            scope = value if key == "queueBinding" else value.get("scope")
            if isinstance(scope, dict) and isinstance(scope.get("sessionId"), str):
                scope["sessionId"] = session_id
    return metadata


class RuntimeProxy:
    def __init__(self, agent: Any, session_id: str, path: Path):
        self.agent = agent
        self.session_id = session_id
        self.path = path
        comms = getattr(agent, "_comms", None)
        if comms is None:
            root = getattr(agent, "_coordination_root", None)
            if root is None:
                raise ValueError("Runtime proxy needs a coordination root.")
            from .operations import wire

            comms = wire(root)
        self._comms = comms
        self.writer: asyncio.StreamWriter | None = None
        self.task: asyncio.Task[None] | None = None
        self._closed = False
        self._controller_token: str | None = None
        self._permission_tasks: dict[str, asyncio.Task[None]] = {}
        try:
            thread = comms.registry.require(session_id)
        except ValueError:
            self._identity: float | None = None
        else:
            self._identity = thread.created_at

    def _owner_path(self) -> Path:
        snapshot = self._comms.registry.snapshot()
        canonical = snapshot.aliases.get(self.session_id, self.session_id)
        thread = snapshot.threads.get(canonical)
        if thread is None:
            raise RuntimeError(f"Thread {self.session_id!r} is not registered.")
        identity = thread.created_at
        if self._identity is None:
            self._identity = identity
        elif identity != self._identity:
            raise OwnerIdentityChangedError("Thread identity changed; open a new attachment.")
        if thread.pid <= 0 or not snapshot.statuses[canonical].running:
            raise ConnectionError(f"Thread {self.session_id!r} has no running owner.")
        return socket_path(self._comms.root, thread.pid)

    async def _connect_current(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        # A failed connect has sent no request bytes. Resolve again because a
        # replacement owner may have registered while its socket was starting.
        deadline = asyncio.get_running_loop().time() + 5
        while True:
            try:
                path = self._owner_path()
                reader, writer = await asyncio.open_unix_connection(path, limit=8 * 1024 * 1024)
            except (FileNotFoundError, ConnectionRefusedError, ConnectionError):
                if asyncio.get_running_loop().time() >= deadline:
                    raise
                await asyncio.sleep(0.05)
                continue
            try:
                if self._owner_path() == path:
                    self.path = path
                    return reader, writer
            except BaseException:
                writer.close()
                raise
            writer.close()
            if asyncio.get_running_loop().time() >= deadline:
                raise ConnectionError("Thread owner changed before attachment.")
            await asyncio.sleep(0.05)

    async def subscribe(self) -> dict[str, Any]:
        reader, metadata = await self._subscribe_once()
        self.task = asyncio.create_task(self.forward(reader))
        return metadata

    async def _subscribe_once(self) -> tuple[asyncio.StreamReader, dict[str, Any]]:
        reader, writer = await self._connect_current()
        self.writer = writer
        try:
            writer.write(
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
            await writer.drain()
            while line := await reader.readline():
                data = json.loads(line)
                _raise_owner_error(data)
                if "ready" in data:
                    token = data.get("controllerToken")
                    # Older detached owners remain attachable for ordinary
                    # messages, but can never become a permission controller.
                    self._controller_token = (
                        token if isinstance(token, str) and len(token) == 64 else None
                    )
                    metadata = cast(dict[str, Any], data["ready"])
                    return reader, _present_cursor_session(metadata, self.session_id)
                await self.update(data)
            raise RuntimeError("Thread owner disconnected during attachment")
        except BaseException:
            writer.close()
            raise

    async def update(self, data: dict[str, Any]) -> None:
        if "update" in data and self.agent._client is not None:
            await self.agent._client.session_update(
                session_id=self.session_id,
                update=_present_cursor_session(data["update"], self.session_id),
            )
        if "permissionRequest" in data:
            request = data["permissionRequest"]
            if (
                not isinstance(request, dict)
                or not isinstance(request.get("id"), str)
                or len(self._permission_tasks) >= 4
            ):
                return
            request_id = request["id"]
            if request_id in self._permission_tasks:
                return
            token = self._controller_token

            async def ask() -> None:
                outcome: dict[str, Any] = {"outcome": "cancelled"}
                try:
                    if self.agent._client is not None and token == self._controller_token:
                        reply = await asyncio.wait_for(
                            self.agent._client.request_permission(
                                session_id=self.session_id,
                                tool_call=request["toolCall"],
                                options=request["options"],
                            ),
                            timeout=ACP_PERMISSION_TIMEOUT_SECONDS,
                        )
                        outcome = RequestPermissionResponse.model_validate(reply).model_dump(
                            by_alias=True, exclude_none=True
                        )["outcome"]
                except (Exception, asyncio.CancelledError):
                    # The owner receives a denial, not client exception text.
                    pass
                finally:
                    if (
                        token == self._controller_token
                        and self.writer is not None
                        and not self.writer.is_closing()
                    ):
                        self.writer.write(
                            (
                                json.dumps(
                                    {
                                        "permissionResponse": {
                                            "id": request_id,
                                            **outcome,
                                        }
                                    }
                                )
                                + "\n"
                            ).encode()
                        )
                        with suppress(OSError, ConnectionError, TimeoutError):
                            await asyncio.wait_for(self.writer.drain(), timeout=1)
                    self._permission_tasks.pop(request_id, None)

            self._permission_tasks[request_id] = asyncio.create_task(ask())

    async def forward(self, reader: asyncio.StreamReader) -> None:
        while not self._closed:
            try:
                while line := await reader.readline():
                    await self.update(json.loads(line))
            except (OSError, ConnectionError):
                pass  # A reset subscription is safe to establish again.
            except ValueError:
                return
            self._controller_token = None
            for permission in self._permission_tasks.values():
                permission.cancel()
            self._permission_tasks.clear()
            if self.writer is not None:
                self.writer.close()
            while not self._closed:
                try:
                    reader, metadata = await self._subscribe_once()
                    image_support = getattr(self.agent, "_proxy_image_support", None)
                    if isinstance(image_support, dict):
                        image_support[self.session_id] = (
                            metadata.get("agentComms", {}).get("imagePrompts") is True
                        )
                    if "configOptions" in metadata and self.agent._client is not None:
                        await self.agent._client.session_update(
                            session_id=self.session_id,
                            update={
                                "sessionUpdate": "config_option_update",
                                "configOptions": metadata["configOptions"],
                            },
                        )
                    break
                except OwnerIdentityChangedError:
                    return
                except (OSError, RuntimeError):
                    await asyncio.sleep(0.1)

    async def request(self, action: str, **kwargs: Any) -> dict[str, Any]:
        reader, writer = await self._connect_current()
        try:
            writer.write(
                (
                    json.dumps(
                        {
                            "action": action,
                            "thread": self.session_id,
                            **(
                                {"controllerToken": self._controller_token}
                                if action == "prompt"
                                else {}
                            ),
                            **kwargs,
                        }
                    )
                    + "\n"
                ).encode()
            )
            await writer.drain()
            line = await reader.readline()
            if not line:
                raise RuntimeError("Thread owner disconnected after request; outcome unknown.")
            data = json.loads(line)
            _raise_owner_error(data)
            return cast(dict[str, Any], data["result"])
        finally:
            writer.close()

    async def close(self) -> None:
        self._closed = True
        self._controller_token = None
        for permission in self._permission_tasks.values():
            permission.cancel()
        self._permission_tasks.clear()
        if self.writer is not None:
            self.writer.close()
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
