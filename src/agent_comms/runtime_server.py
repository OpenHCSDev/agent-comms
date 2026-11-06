"""The runtime socket server: it decodes each runtime request, binds it and applies it."""

from __future__ import annotations

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
from typing import Any, Self
from pydantic import BaseModel
from .coordination.errors import Refused
from acp.exceptions import RequestError
from acp.schema import DeniedOutcome, PermissionOption, RequestPermissionResponse, ToolCallUpdate
from .controller import UNBOUND_CONTROLLER, Controller, SessionUpdateSink
from .nominal.field_codec import AcpSessionId, FieldCodec
from .owner_protocol import ClientLine, OwnerFailure, OwnerLine, OwnerPermissionRequest, OwnerUpdate
from .owner_connection import socket_path
from .coordination.errors import CoordinationError, IdentityConflict
from .native_failures import NativeRequestFailure
from .storage.private_path import PrivateSocketRole
from .runtime_request import RuntimeRequest
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .acp import CommsAgent
    from agent_comms.acp import AcpSelectedWriteAuthority
    from .turn_runner import TurnRunner


_LOG = logging.getLogger(__name__)


class UnboundClient(SessionUpdateSink):
    async def publish_update(self, *, session_id: AcpSessionId, update: BaseModel) -> bool:
        raise RuntimeError("An unbound client cannot publish session updates")


_UNBOUND_PUBLICATION_CLIENT = UnboundClient()


class SubscriberClosed(ConnectionError):
    """The attached reader left; the normal end of a subscription, not a failed request."""


@dataclass(frozen=True, kw_only=True)
class ClientPermissionResponse(ClientLine):
    """The controller's decision on one permission request."""

    id: str
    outcome: dict[str, object]

    def __post_init__(self) -> None:
        self.response()

    @classmethod
    def cancelled(cls, request_id: str) -> Self:
        denied = RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        return cls.of(request_id, denied)

    @classmethod
    def of(cls, request_id: str, response: RequestPermissionResponse) -> Self:
        outcome = response.outcome.model_dump(by_alias=True, exclude_none=True)
        return cls(id=request_id, outcome=outcome)

    def response(self) -> RequestPermissionResponse:
        return RequestPermissionResponse.model_validate({"outcome": self.outcome})

    def answer(self, client: SocketClient) -> None:
        pending = client.pending.get(self.id)
        if pending is not None and not pending.done():
            pending.set_result(self.response())


class SocketClient(Controller, SessionUpdateSink):
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

    def require_current(self, authority: AcpSelectedWriteAuthority, session_id: AcpSessionId) -> None:
        if not authority.agent._runtime.is_controller(session_id, self):
            raise IdentityConflict("Selected write controller disconnected")

    async def request_ui_permission(
        self, runner: TurnRunner, session_id: AcpSessionId,
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

    def departed(self, runtime: RuntimeServer, session_id: AcpSessionId) -> bool:
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

    async def publish_update(self, *, session_id: AcpSessionId, update: BaseModel) -> bool:
        await self.send(OwnerUpdate.of(update))
        return True


class SessionClients(dict[AcpSessionId, set[SocketClient]]):
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


class RuntimeServer(SessionUpdateSink):
    def __init__(self, agent: CommsAgent):
        self.agent = agent
        self.server: asyncio.Server | None = None
        self.listener = socket.socket(socket.AF_UNIX)
        self.clients = SessionClients(self)
        # Set only while handling a prompt on this particular attachment.
        # Never publish this bearer to ACP metadata, the transcript or other sockets.
        self.controller: ContextVar[Controller] = ContextVar(
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

    def inherit_listener(self, descriptor: int) -> None:
        """Adopt the launcher's already-bound socket, not another readiness flag."""
        self.listener.close()
        self.listener = socket.socket(fileno=descriptor)

    async def start(self) -> None:
        if self.server is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if not self.listener.getsockname():
                address = self._resources.enter_context(PrivateSocketRole.address(self.path))
                self.listener.bind(str(address))
            self.listener.listen()
            self.listener.setblocking(False)
            self.server = await asyncio.start_unix_server(
                self.handle, sock=self.listener, limit=8 * 1024 * 1024,
                cleanup_socket=False,
            )
        except BaseException:
            self._resources.close()
            raise

    async def publish_update(
        self,
        *,
        session_id: AcpSessionId,
        update: BaseModel,
        _expected_client: SessionUpdateSink | None = _UNBOUND_PUBLICATION_CLIENT,
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
                and self.agent.sessions.binds(session_id, _expected_thread)
            )

        def sockets_bound() -> bool:
            return _expected_sockets is None or self.clients.get(session_id, set()).issubset(
                _expected_sockets
            )

        if not bound() or not sockets_bound():
            return False
        delivered = await self.agent.sessions.client.publish_update(
            session_id=session_id, update=update)
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
                delivered = await socket_client.publish_update(
                    session_id=session_id, update=update,
                ) or delivered
            except SubscriberClosed:
                self.clients.disconnect(socket_client)
        return delivered

    def is_controller(self, session_id: AcpSessionId, controller: SocketClient) -> bool:
        return controller in self.clients.get(session_id, ()) and not controller.writer.is_closing()

    async def request_permission(
        self,
        session_id: AcpSessionId,
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
        try:
            line = await reader.readline()
            if not line:
                return
            request = FieldCodec.decode(RuntimeRequest, json.loads(line))
            with request.holder.selected():
                context = await request.bind(self, reader, client)
                await request.apply(context)
        except SubscriberClosed:
            pass
        except asyncio.CancelledError:
            pass
        except Refused as error:
            # The reply is the whole answer: a correct refusal is not a fault.
            with suppress(SubscriberClosed):
                await client.send(OwnerFailure.refused(error))
        except RequestError as error:
            # A failed turn in its protocol form crosses whole, and the client's own boundary
            # raises it as itself. Closing without a reply would report it as an unknown outcome.
            with suppress(SubscriberClosed):
                await client.send(OwnerFailure.refused(Refused(error.code, str(error), error.data)))
            raise
        except (ValueError, CoordinationError, NativeRequestFailure) as error:
            # The client gets only the text, so the fault stays loud on this side.
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
        self.listener.close()
        self._resources.close()

    def retire_session(self, session_id: AcpSessionId) -> None:
        for client in tuple(self.clients.get(session_id, ())):
            client.disconnected()
            client.writer.close()
            self.clients.disconnect(client)
