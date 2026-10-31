"""Local attachment to a thread's single executing ACP owner.

Opening another UI subscribes to the owner; it never launches another backend
or changes the registry PID. The socket is scoped to the wire and owner PID.
"""

from __future__ import annotations

from functools import partial

from .acp_extension import (
    decode_updates,
    encode_updates,
)
from .coordinator import Coordination

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING


from .coordination_errors import Refused
from acp.schema import (
    PermissionOption,
    RequestPermissionResponse,
    SessionNotification,
    ToolCallUpdate,
)

from .controller import (
    ACP_PERMISSION_TIMEOUT_SECONDS,
)
from .nominal.field_codec import AcpSessionId, FieldCodec, MalformedRecord
from .owner_protocol import (
    IncompatibleOwnerError,
    OwnerLine,
    OwnerPermissionRequest,
    OwnerReady,
    OwnerUpdate,
    UnannouncedOwner,
)
from .owner_connection import NoControllerToken, OwnerConnection, OwnerIdentityChangedError
from agent_comms.runtime_server import RuntimeRequest
from .runtime_requests import SubscribeRuntimeRequest
from .thread_identity import ThreadIncarnation

from .runtime_server import ClientPermissionResponse

if TYPE_CHECKING:
    from .acp import CommsAgent


_LOG = logging.getLogger(__name__)


def present_session(metadata: dict[str, object], session_id: AcpSessionId) -> dict[str, object]:
    """Rebind presentation coordinates through their declared owners."""
    return {
        **metadata,
        **encode_updates(*(fact.for_session(session_id) for fact in decode_updates(metadata))),
    }


class RuntimeConnection(OwnerConnection):
    """Owner requests select declared behavior independently of ACP attachment identity."""

    @property
    def controller_token(self) -> str | NoControllerToken:
        return NoControllerToken()

    def request_payload(self, action: str, parameters: dict[str, object]) -> dict[str, object]:
        return RuntimeRequest.member_named(action).proxy_payload(
            self._identity.name, self.controller_token, parameters,
        )


@dataclass(frozen=True)
class OwnerAttachment:
    """A completed subscription: its stream, the owner's ready line and presented metadata."""

    reader: asyncio.StreamReader
    ready: OwnerReady
    presented: dict[str, object]


class RuntimeProxy(RuntimeConnection):
    def __init__(
        self, agent: CommsAgent, identity: ThreadIncarnation,
        session_id: AcpSessionId, path: Path,
    ):
        super().__init__(agent._comms, identity, path)
        self.session_id = session_id
        self.agent = agent
        self.writer: asyncio.StreamWriter | None = None
        self.task: asyncio.Task[None] | None = None
        self._controller_token: str | NoControllerToken = NoControllerToken()
        self._permission_tasks: dict[str, asyncio.Task[None]] = {}

    @property
    def controller_token(self) -> str | NoControllerToken:
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
                    json.dumps(FieldCodec.encode(subscription(thread=self._identity.name)))
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
                raise IncompatibleOwnerError(UnannouncedOwner(thread=self._identity.name)) from error
            first.announced(self._identity.name)
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
        notification = SessionNotification.model_validate({
            "session_id": self.session_id, "update": {**line.update, "_meta": metadata},
        })
        await client.publish_update(
            session_id=self.session_id, update=notification.update
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
            except (ValueError, RuntimeError, Refused):
                # The owner is our component: an undecodable or unexpected line,
                # or the owner's failure line, is a defect, not an end of stream.
                # Record it before this subscription dies.
                _LOG.exception("Owner update stream failed (session=%s)", self.session_id)
                raise
            self._controller_token = NoControllerToken()
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
        self._controller_token = NoControllerToken()
        if self.writer is not None:
            self.writer.close()
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        await self.close_permissions()
        await super().close()
        if self.writer is not None:
            await self.writer.wait_closed()
