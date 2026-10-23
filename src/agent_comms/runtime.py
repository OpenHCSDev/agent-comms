"""Local attachment to a thread's single executing ACP owner.

Opening another UI subscribes to the owner; it never launches another backend
or changes the registry PID. The socket is scoped to the wire and owner PID.
"""

from __future__ import annotations

from functools import partial
from .coordinator import Coordination

import asyncio
import json
import logging
import os
import secrets
import socket
from collections.abc import Coroutine
from contextlib import ExitStack, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel

from acp.exceptions import RequestError
from acp.schema import (
    DeniedOutcome,
    PermissionOption,
    RequestPermissionResponse,
    ToolCallUpdate,
)

from .jsonl_stream import JsonlStreamReader
from .controller import ACP_PERMISSION_TIMEOUT_SECONDS, UNBOUND_CONTROLLER, Controller
from .field_codec import MalformedRecord
from .owner_protocol import (
    ClientPermissionResponse,
    IncompatibleOwnerError,
    OwnerFailure,
    OwnerLine,
    OwnerPermissionRequest,
    OwnerReady,
    OwnerUpdate,
    UnannouncedOwner,
)
from .runtime_requests import RuntimeRequest, SubscribeRuntimeRequest
from .registry_document import RegistryEntry
from .child_process import ProcessIdentity
from .thread_identity import OwnerIdentity
from .errors import RelationViolationError, UnregisteredThreadError
from .coordination_errors import CoordinationError, IdentityConflict
from .native_failures import NativeRequestFailure
from .private_path import PrivateSocketRole

if TYPE_CHECKING:
    from .acp import CommsAgent
    from .selected_write_authority import AcpSelectedWriteAuthority
    from .turn_runner import TurnRunner


def socket_path(root: Path, pid: int) -> Path:
    return root / "runtime" / f"{pid}.sock"


_UNBOUND_PUBLICATION_CLIENT = object()
_LOG = logging.getLogger(__name__)


class SubscriberClosed(ConnectionError):
    """The attached reader left; the normal end of a subscription, not a failed request."""


class SocketClient(Controller):
    def __init__(self, writer: asyncio.StreamWriter):
        self.writer = writer
        transport = writer.transport
        low_water, high_water = transport.get_write_buffer_limits()
        # Reserve one burst from the socket's actual configured send resource.
        # A brief reader pause must not be confused with an abandoned observer.
        # Kernel/transport overrides remain the budget owners; no timer or queue.
        send_bytes = writer.get_extra_info("socket").getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF)
        transport.set_write_buffer_limits(high=max(high_water, send_bytes), low=low_water)
        self.token = secrets.token_hex(32)
        self.pending: dict[str, asyncio.Future[RequestPermissionResponse]] = {}

    def require_current(self, authority: AcpSelectedWriteAuthority, session_id: str) -> None:
        if not authority.agent._runtime.is_controller(session_id, self):
            raise IdentityConflict("Selected write controller disconnected")

    async def request_ui_permission(
        self, runner: TurnRunner, session_id: str,
        request: tuple[ToolCallUpdate, list[PermissionOption]],
    ) -> dict[str, object] | None:
        tool_call, options = request
        reply = await runner.runtime.request_permission(
            session_id,
            self,
            tool_call.model_dump(by_alias=True, exclude_none=True),
            [option.model_dump(by_alias=True, exclude_none=True) for option in options],
        )
        if reply is None:
            return None
        return reply.outcome.model_dump(by_alias=True, exclude_none=True)

    def departed(self, runtime: RuntimeServer, session_id: str) -> bool:
        return not runtime.is_controller(session_id, self)

    async def send(self, line: OwnerLine) -> None:
        """Bound this subscriber's output without borrowing the owner turn."""
        if self.writer.is_closing():
            raise SubscriberClosed("Runtime subscriber is closed")
        record = line.wire()
        transport = self.writer.transport
        low_water, high_water = transport.get_write_buffer_limits()
        # One original saved snapshot must fit. Retain the transport's existing
        # configured budget when it is larger; no second output queue is owned.
        transport.set_write_buffer_limits(high=max(high_water, len(record)), low=low_water)
        try:
            self.writer.write(record)
        except (BrokenPipeError, ConnectionResetError) as error:
            raise SubscriberClosed("Runtime subscriber disconnected") from error
        await asyncio.sleep(0)
        _, high_water = transport.get_write_buffer_limits()
        if transport.get_write_buffer_size() > high_water:
            transport.abort()
            self.disconnected()
            raise SubscriberClosed(
                "Runtime subscriber stopped reading; attach to canonical history again"
            )

    async def permission(
        self, tool_call: dict[str, object], options: list[dict[str, object]]
    ) -> RequestPermissionResponse | None:
        if len(self.pending) >= 4:
            return None
        request_id = secrets.token_hex(16)
        future: asyncio.Future[RequestPermissionResponse] = (
            asyncio.get_running_loop().create_future()
        )
        self.pending[request_id] = future
        task = asyncio.current_task()
        try:
            await self.send(
                OwnerPermissionRequest(id=request_id, tool_call=tool_call, options=options)
            )
            # Cancellation can race completion of a fast socket handoff.
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
                future.set_result(
                    RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
                )
        self.pending.clear()

    async def session_update(self, *, session_id: str, update: BaseModel) -> None:
        await self.send(OwnerUpdate.of(update))


class OwnerIdentityChangedError(RuntimeError, ConnectionError):
    """A saved attachment lost its root or thread identity and cannot reconnect."""


class OwnerReplyLostError(RuntimeError, ConnectionError):
    """The owner disconnected after dispatch; a mutating request remains uncertain."""


class SessionClients(dict[str, set[SocketClient]]):
    """The subscriber collection owns removal and the last-disconnect event."""

    def __init__(self, runtime: RuntimeServer):
        super().__init__()
        self.runtime = runtime

    def disconnect(self, client: SocketClient) -> None:
        for session_id, members in self.items():
            if client in members:
                members.discard(client)
                if not members:
                    del self[session_id]
                    self.runtime.agent.sessions.config.stop_observing_configuration(session_id)
                return


class RuntimeServer:
    def __init__(self, agent: CommsAgent):
        self.agent = agent
        self.server: asyncio.Server | None = None
        self.clients = SessionClients(self)
        # Set only while handling a prompt on this particular attachment.
        # Never publish this bearer to ACP metadata, the transcript or other sockets.
        self.controller: ContextVar[Controller | None] = ContextVar(
            "runtime_permission_controller", default=UNBOUND_CONTROLLER
        )
        self.path = socket_path(agent._comms.root, os.getpid())
        self._resources = ExitStack()

    def background[T](self, work: Coroutine[Any, Any, T]) -> asyncio.Task[T]:
        """Launch owner work without inheriting an attached human controller.

        Nobody awaits most owner work, so its failure is logged here; an
        awaiter still receives the exception.
        """
        token = self.controller.set(UNBOUND_CONTROLLER)
        try:
            task = asyncio.create_task(work, name=work.__qualname__)
        finally:
            self.controller.reset(token)
        task.add_done_callback(self._report_failure)
        return task

    @staticmethod
    def _report_failure[T](task: asyncio.Task[T]) -> None:
        if not task.cancelled() and (error := task.exception()) is not None:
            _LOG.error("Owner work %s failed", task.get_name(), exc_info=error)

    async def start(self) -> None:
        if self.server is not None or os.name == "nt":
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            address = self._resources.enter_context(PrivateSocketRole.address(self.path))
            self.server = await asyncio.start_unix_server(
                self.handle, path=address, limit=8 * 1024 * 1024
            )
        except BaseException:
            self._resources.close()
            raise

    async def session_update(
        self,
        *,
        session_id: str,
        update: BaseModel,
        _expected_client: Any = _UNBOUND_PUBLICATION_CLIENT,
        _expected_thread: str | None = None,
        _expected_sockets: frozenset[SocketClient] | None = None,
    ) -> bool:
        """Return whether any local transport accepted the update.

        A publisher may pass a bound client/thread snapshot. Recheck at
        the transport entry and before each socket handoff; ACP-only binding
        changes do not take the registry's kernel identity fence. A post-send
        change cannot undo delivery and is checked by the outbox observer.
        """

        def bound() -> bool:
            return _expected_client is _UNBOUND_PUBLICATION_CLIENT or (
                self.agent.sessions.client is _expected_client
                and self.agent.sessions.bindings.get(session_id) == _expected_thread
            )

        def sockets_bound() -> bool:
            return _expected_sockets is None or self.clients.get(session_id, set()).issubset(
                _expected_sockets
            )

        if not bound() or not sockets_bound():
            return False
        delivered = False
        client = self.agent.sessions.client
        if client is not None:
            await client.session_update(session_id=session_id, update=update)
            delivered = True
        # Do not re-resolve the recipient set after an awaited handoff. A
        # socket reattach has a new identity even with the same session ID.
        sockets = (
            tuple(self.clients.get(session_id, ()))
            if _expected_sockets is None
            else tuple(_expected_sockets)
        )
        for socket_client in sockets:
            if not bound() or not sockets_bound():
                return delivered
            if socket_client not in self.clients.get(session_id, ()):
                continue
            try:
                await socket_client.session_update(session_id=session_id, update=update)
                delivered = True
            except SubscriberClosed:
                self.clients.disconnect(socket_client)
        return delivered

    def is_controller(self, session_id: str, controller: SocketClient) -> bool:
        return controller in self.clients.get(session_id, ()) and not controller.writer.is_closing()

    async def request_permission(
        self,
        session_id: str,
        controller: SocketClient,
        tool_call: dict[str, object],
        options: list[dict[str, object]],
    ) -> RequestPermissionResponse | None:
        if not self.is_controller(session_id, controller):
            return None
        reply = await controller.permission(tool_call, options)
        return reply if self.is_controller(session_id, controller) else None

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        client = SocketClient(writer)
        session_id = None
        try:
            request = RuntimeRequest.from_wire(json.loads(await reader.readline()))
            context = await request.bind(self, reader, client)
            session_id = context.session_id
            await request.apply(context)
        except SubscriberClosed:
            pass
        except asyncio.CancelledError:
            pass
        except (ValueError, RequestError, CoordinationError, NativeRequestFailure) as error:
            # Reply to this requester, then propagate the original failure to the server task.
            with suppress(SubscriberClosed):
                await client.send(OwnerFailure.of(error))
            raise
        finally:
            self.clients.disconnect(client)
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
        self._resources.close()


def present_session(metadata: dict[str, object], session_id: str) -> dict[str, object]:
    """Rebind presentation coordinates through their declared owners."""
    from .acp_extension import decode_updates, encode_updates

    return {
        **metadata,
        **encode_updates(*(fact.for_session(session_id) for fact in decode_updates(metadata))),
    }


class RuntimeConnection:
    """A shared headless-client socket connection that checks root, owner and process identity."""

    def __init__(self, comms, session_id: str, path: Path):
        self.session_id = session_id
        self.path = path
        self._comms = comms
        self._closed = False
        self._requests: set[asyncio.Task] = set()
        self._identity = comms.registry.entry(session_id).entry.thread.incarnation
        self._root_identity = comms.root.stat()

    @property
    def controller_token(self):
        return None

    def _owner_entry(self) -> RegistryEntry:
        if self._closed:
            raise ConnectionError("Owner attachment was closed before request dispatch")
        try:
            same_root = os.path.samestat(self._root_identity, self._comms.root.stat())
        except FileNotFoundError:
            same_root = False
        if not same_root:
            raise OwnerIdentityChangedError("Wire root changed; open a new attachment.")
        try:
            entry = self._comms.registry.entry(self.session_id).entry
        except UnregisteredThreadError as error:
            raise OwnerIdentityChangedError(
                "Thread identity changed; open a new attachment."
            ) from error
        if (not entry.matches_name(self._identity.name)
                or self._identity != entry.thread.incarnation):
            raise OwnerIdentityChangedError("Thread identity changed; open a new attachment.")
        return entry

    def _require_connect_owner(self, owner: OwnerIdentity, process: ProcessIdentity) -> None:
        try:
            self._owner_entry().require_owner_process(owner, process)
        except RelationViolationError as error:
            raise ConnectionError(f"Cannot attach {self.session_id!r}: {error}") from error
        if not process.alive():
            raise ConnectionError(
                f"Owner {process.pid} for {self.session_id!r} exited before attachment; "
                f"request not sent. Startup output: {self._comms.root / 'diagnostics'}"
            )

    async def _connect_current(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        # Startup has no elapsed-time expiry. The registered process and lease
        # own its lifetime; caller cancellation also ends this unsent operation.
        entry = await Coordination.run_worker(self._owner_entry)
        try:
            entry.status.require_active()
            process = entry.thread.require_process()
        except RelationViolationError as error:
            raise ConnectionError(f"Cannot attach {self.session_id!r}: {error}") from error
        owner = entry.owner_identity
        path = socket_path(self._comms.root, process.pid)

        while True:
            await Coordination.run_worker(partial(self._require_connect_owner, owner, process))
            try:
                with PrivateSocketRole.address(path) as address:
                    reader, writer = await asyncio.open_unix_connection(address, limit=8 * 1024 * 1024)
            except (FileNotFoundError, ConnectionRefusedError):
                await asyncio.sleep(0.05)
                continue
            try:
                await Coordination.run_worker(partial(self._require_connect_owner, owner, process))
                self.path = path
                return reader, writer
            except BaseException:
                writer.close()
                raise

    async def _read_response(self, reader: asyncio.StreamReader) -> bytes:
        return await JsonlStreamReader(reader).readline()

    async def request(self, action: str, **kwargs: object) -> object:
        if self._closed:
            raise ConnectionError("Owner attachment was closed before request dispatch")
        task = asyncio.current_task()
        assert task is not None
        self._requests.add(task)
        writer = None
        try:
            reader, writer = await self._connect_current()
            if self._closed:
                raise ConnectionError("Owner attachment was closed before request dispatch")
            writer.write(
                (
                    json.dumps(
                        RuntimeRequest.member_named(action).proxy_payload(
                            self.session_id, self.controller_token, kwargs
                        )
                    )
                    + "\n"
                ).encode()
            )
            await writer.drain()
            line = await self._read_response(reader)
            if not line:
                raise OwnerReplyLostError("Thread owner disconnected after request; outcome unknown.")
            return OwnerLine.read(line).reply()
        finally:
            try:
                if writer is not None:
                    writer.close()
                    await writer.wait_closed()
            finally:
                self._requests.discard(task)

    async def close(self):
        self._closed = True
        tasks = tuple(self._requests)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class OwnerReplyConnection(RuntimeConnection):
    """Bound only the live owner's response after a request has been dispatched."""

    REPLY_TIMEOUT: ClassVar[float] = 3

    async def _read_response(self, reader: asyncio.StreamReader) -> bytes:
        async with asyncio.timeout(self.REPLY_TIMEOUT):
            return await super()._read_response(reader)


@dataclass(frozen=True)
class OwnerAttachment:
    """A completed subscription: its stream, the owner's ready line and presented metadata."""

    reader: asyncio.StreamReader
    ready: OwnerReady
    presented: dict[str, object]


class RuntimeProxy(RuntimeConnection):
    def __init__(self, agent: CommsAgent, session_id: str, path: Path):
        super().__init__(agent._comms, session_id, path)
        self.agent = agent
        self.writer: asyncio.StreamWriter | None = None
        self.task: asyncio.Task[None] | None = None
        self._controller_token: str | None = None
        self._permission_tasks: dict[str, asyncio.Task[None]] = {}

    @property
    def controller_token(self):
        return self._controller_token

    async def subscribe(
        self, subscription: type[SubscribeRuntimeRequest] = SubscribeRuntimeRequest,
    ) -> dict[str, object]:
        attachment = await self._subscribe_once(subscription)
        self.task = asyncio.create_task(self.forward(attachment.reader))
        return attachment.presented

    async def _subscribe_once(
        self, subscription: type[SubscribeRuntimeRequest] = SubscribeRuntimeRequest,
    ) -> OwnerAttachment:
        reader, writer = await self._connect_current()
        self.writer = writer
        try:
            writer.write(
                (
                    json.dumps(
                        subscription(
                            thread=self.session_id,
                        ).to_wire()
                    )
                    + "\n"
                ).encode()
            )
            await writer.drain()
            announcement = await reader.readline()
            if not announcement:
                raise RuntimeError("Thread owner disconnected during attachment")
            try:
                first = OwnerLine.read(announcement)
            except MalformedRecord as error:
                # An owner older than the declared lines writes an undeclared first line.
                raise IncompatibleOwnerError(UnannouncedOwner(thread=self.session_id)) from error
            first.announced(self.session_id)
            while line := await reader.readline():
                ready = await OwnerLine.read(line).attach(self)
                if ready is None:
                    continue
                presented = await Coordination.run_worker(
                    partial(present_session, ready.ready, self.session_id)
                )
                if self._closed:
                    raise ConnectionError("Owner attachment closed during subscription")
                self._controller_token = ready.controller_token
                return OwnerAttachment(reader, ready, presented)
            raise RuntimeError("Thread owner disconnected during attachment")
        except BaseException:
            writer.close()
            raise

    async def present(self, line: OwnerUpdate) -> None:
        client = self.agent.sessions.client
        if client is None:
            return
        metadata = await Coordination.run_worker(
            partial(present_session, line.meta, self.session_id)
        )
        if self._closed or self.agent.sessions.client is not client:
            return
        await client.session_update(
            session_id=self.session_id, update={**line.update, "_meta": metadata}
        )

    def ask(self, request: OwnerPermissionRequest) -> None:
        if len(self._permission_tasks) >= 4 or request.id in self._permission_tasks:
            return
        token = self._controller_token

        async def ask() -> None:
            response = ClientPermissionResponse.cancelled(request.id)
            try:
                if self.agent.sessions.client is not None and token == self._controller_token:
                    reply = await asyncio.wait_for(
                        self.agent.sessions.client.request_permission(
                            session_id=self.session_id,
                            tool_call=ToolCallUpdate.model_validate(request.tool_call),
                            options=[
                                PermissionOption.model_validate(option)
                                for option in request.options
                            ],
                        ),
                        timeout=ACP_PERMISSION_TIMEOUT_SECONDS,
                    )
                    response = ClientPermissionResponse.of(
                        request.id, RequestPermissionResponse.model_validate(reply)
                    )
            finally:
                try:
                    if (
                        token == self._controller_token
                        and self.writer is not None
                        and not self.writer.is_closing()
                    ):
                        self.writer.write(response.wire())
                        await asyncio.wait_for(self.writer.drain(), timeout=1)
                finally:
                    self._permission_tasks.pop(request.id, None)

        self._permission_tasks[request.id] = asyncio.create_task(ask())

    async def forward(self, reader: asyncio.StreamReader) -> None:
        while not self._closed:
            try:
                while line := await reader.readline():
                    await OwnerLine.read(line).deliver(self)
            except ConnectionResetError:
                pass  # The transport reports the closed subscription, not a decoder defect.
            except (ValueError, RuntimeError, RequestError):
                # The owner is our component: an undecodable or unexpected line,
                # or the owner's failure line, is a defect, not an end of stream.
                # Record it before this subscription dies.
                _LOG.exception("Owner update stream failed (session=%s)", self.session_id)
                raise
            self._controller_token = None
            await self.close_permissions()
            if self.writer is not None:
                self.writer.close()
            while not self._closed:
                try:
                    attachment = await self._subscribe_once()
                    reader = attachment.reader
                    break
                except OwnerIdentityChangedError:
                    return
                except (FileNotFoundError, ConnectionRefusedError):
                    await asyncio.sleep(0.1)

    async def close_permissions(self) -> None:
        tasks = tuple(self._permission_tasks.values())
        for permission in tasks:
            permission.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def close(self) -> None:
        self._closed = True
        self._controller_token = None
        if self.writer is not None:
            self.writer.close()
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        await self.close_permissions()
        await super().close()
        if self.writer is not None:
            await self.writer.wait_closed()
