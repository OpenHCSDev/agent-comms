"""A UI's Core on another machine, reached through the connector's tools/call."""

from __future__ import annotations

import asyncio
import gzip
import http.client
import io
import json
import socket
import ssl
import threading
import time
import urllib.error
from dataclasses import dataclass, replace
from functools import cached_property
from multiprocessing.connection import Connection
from typing import ClassVar

import aiohttp
from pydantic import JsonValue
from yarl import URL

from agent_comms.execution.child_process import RelayEnding
from agent_comms.transport.sync_answer import AwaitsWork
from agent_comms.coordination.errors import CoordinationReadUnavailable
from agent_comms.nominal.field_codec import FieldCodec, RpcCallId
from agent_comms.transport.connector_profile import (
    OBSERVE_PATH,
    EndpointRoute,
    HttpsMcpEndpoint,
    UiConnectionProfile,
    UiCredential,
)
from agent_comms.transport.mcp_wire import (
    CancelledMessage,
    JsonRpcId,
    StreamedLine,
    ToolsCallMessage,
    ToolsCallReply,
    ToolsListMessage,
    ToolsListReply,
)
from agent_comms.transport.progress import WireExportCancelled
from agent_comms.transport.request import (
    ConnectionDropped,
    CoreAccess,
    ObservedRequest,
    RequestPoster,
    ToolRequest,
)
from agent_comms.transport.tool_result import ToolResult
from agent_comms.ui_model.observation_wire import (
    STREAM_CONNECT_TIMEOUT,
    WEBSOCKET_CLIENT_TIMEOUT,
    WEBSOCKET_HEARTBEAT_SECONDS,
    WEBSOCKET_MAX_MESSAGE_BYTES,
    EncodedConnection,
    ObservationPeer,
    ObservationSource,
    ObserverDied,
    RelayConnection,
    relay_frames,
)


@dataclass(frozen=True)
class ConnectorClosed(RelayEnding):
    def describe(self) -> str:
        return "the connector closed the observation"


@dataclass(frozen=True)
class ObservationRetired(RelayEnding):
    def describe(self) -> str:
        return "the UI retired the observation"


@dataclass(frozen=True)
class ConnectorUnreachable(RelayEnding):
    """The connector refused the observation or could not be reached."""

    error: BaseException

    def describe(self) -> str:
        return f"{type(self.error).__name__}: {self.error}"


@dataclass(frozen=True)
class ServiceFailed(RelayEnding):
    """Core's observation service failed and sent its traceback."""

    traceback: str

    def describe(self) -> str:
        return self.traceback


class ConnectorObserverClosed(ObserverDied, CoordinationReadUnavailable):
    """The remote source ended: show its cause, but defer its pending reads.

    The observation owner still reports the failure and reconnects. Pending
    page workers retain their drawn source instead of raising the same outage
    again; a service failure and a closed socket keep their distinct endings.
    """

    def __init__(self, url: URL, ending: RelayEnding):
        self.ending = ending
        super().__init__(f"The comms observation at {url} closed:\n{ending.describe()}")


class ConnectorObservationPeer(ObservationPeer):
    """A Core across the connector serves this UI's observation. Frames relay between the
    UI's ViewerEnd and the connector's /observe socket from a thread with its own event
    loop, so the UI's loop is never borrowed; the frames are declared records both ways."""

    heartbeat_seconds: ClassVar[float] = WEBSOCKET_HEARTBEAT_SECONDS

    def __init__(self, access: ConnectorClient, codec: type[EncodedConnection]) -> None:
        self.url = access.endpoint.url.with_path(OBSERVE_PATH)
        # The profile's route owns how its endpoint is reached, for this socket as
        # much as for the calls: aiohttp resolves the name itself otherwise.
        self.route = access.route
        viewer, relay = socket.socketpair()
        self.connection = access.viewer_end(viewer.detach(), codec)
        self.ending: RelayEnding = ConnectorClosed()
        # The relay's loop and task exist before its thread starts, so retiring the peer can
        # cancel it at any point, connection setup included.
        self.loop = asyncio.new_event_loop()
        self.lifetime = threading.Lock()
        self.task = self.loop.create_task(
            self.carry(RelayConnection(relay.detach()), access))
        self.thread = threading.Thread(
            target=self.run, name="agent-comms-remote-observation", daemon=True)
        self.thread.start()

    def run(self) -> None:
        try:
            self.loop.run_until_complete(self.task)
        except asyncio.CancelledError:
            # retire cancels the relay: the UI retired this observation.
            self.ending = ObservationRetired()
        finally:
            with self.lifetime:
                # The relay closed its local side, so no worker thread stays blocked on it;
                # they all finish before the loop does.
                self.loop.run_until_complete(self.loop.shutdown_default_executor())
                self.loop.close()

    async def carry(self, local: Connection, access: ConnectorClient) -> None:
        try:
            async with self.route.socket_session() as session, session.ws_connect(
                self.url, headers={"Authorization": f"Bearer {access.credential.token}"},
                ssl=access.tls, heartbeat=self.heartbeat_seconds,
                timeout=WEBSOCKET_CLIENT_TIMEOUT,
                max_msg_size=WEBSOCKET_MAX_MESSAGE_BYTES,
            ) as remote:
                self.ending = await relay_frames(local, remote)
            access.reset_connections()
        except (OSError, aiohttp.ClientError) as error:
            # Connecting is the boundary: the connector refused or could not be reached.
            self.ending = ConnectorUnreachable(error)
            access.reset_connections()
        finally:
            local.close()

    def join(self) -> None:
        self.thread.join()

    def await_end(self, seconds: float) -> None:
        self.thread.join(seconds)

    def retire(self) -> None:
        """End the relay now, even while the connector is still connecting, then wait."""
        with self.lifetime:
            if not self.loop.is_closed():
                self.loop.call_soon_threadsafe(self.task.cancel)
        self.join()

    def service_failed(self, traceback: str) -> ObserverDied:
        return ConnectorObserverClosed(self.url, ServiceFailed(traceback))

    def connection_closed(self) -> ObserverDied:
        """carry records the relay's end before it closes the local side, so the viewer's
        end-of-stream always finds that reason recorded."""
        return ConnectorObserverClosed(self.url, self.ending)


class ConnectorClient(UiConnectionProfile, ObservationSource):
    """Reaching one connector: its endpoint, the credential it accepts, the route this machine
    reaches it by, and the TLS trust both the calls and the observation use."""

    def viewer_end(self, handle: int, codec: type[EncodedConnection]) -> Connection:
        """The caller owns how received observation records are decoded."""
        return codec(handle)

    @cached_property
    def tls(self) -> ssl.SSLContext:
        """One context for every connection to this connector, built when connecting, so the
        trust the owner configures (``SSL_CERT_FILE`` included) applies to calls and
        observation alike."""
        return ssl.create_default_context()

    def observation_peer(self, codec: type[EncodedConnection]) -> ConnectorObservationPeer:
        return ConnectorObservationPeer(self, codec)

    def reset_connections(self) -> None:
        """Drop kept transport connections so the next request dials fresh."""


class ConnectionEpoch:
    """Monotonic generation shared across a client's threads and synced into read workers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.value = 0

    def bump(self) -> int:
        with self._lock:
            self.value += 1
            return self.value

    def advance_to(self, target: int) -> None:
        with self._lock:
            if target > self.value:
                self.value = target


class ThreadConnection(threading.local):
    """This thread's HTTPS connection to one connector, kept open between its calls; a closed,
    reset, or idle one reopens on its next request."""

    def __init__(
        self,
        profile: UiConnectionProfile,
        context: ssl.SSLContext,
        timeout: float,
        epoch: ConnectionEpoch,
    ) -> None:
        # The profile's route owns how its endpoint is reached: a tailnet-only endpoint is
        # dialed at the address its holder reached it on (connector_profile.EndpointRoute).
        self.connection = profile.connection(context, timeout)
        self.generation = epoch.value
        self.last_used = 0.0

    def prepare(self, generation: int, idle_seconds: float, now: float) -> http.client.HTTPSConnection:
        """Close this thread's socket before reusing it if a reset or idle window elapsed."""
        idle = self.last_used > 0.0 and (now - self.last_used) >= idle_seconds
        if self.generation != generation or idle:
            self.connection.close()
            self.generation = generation
            self.last_used = 0.0
        return self.connection

    def mark_used(self, now: float) -> None:
        self.last_used = now


class RemoteCoreAccess(
    CoreAccess, ConnectorClient, RequestPoster, AwaitsWork
):
    """The owner's UI profile is the remote access: its endpoint and credential are all a
    call needs, and the server still decides scope, expiry and revocation."""

    timeout_seconds = 30
    keepalive_idle_seconds: ClassVar[float] = 60.0

    def call[R: ToolResult](self, request: ToolRequest[R]) -> R:
        return FieldCodec.decode(request.result_type(), request.posted(self))

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.credential.token}",
            "Content-Type": "application/json",
            "Accept-Encoding": "gzip",
        }

    @cached_property
    def connection_epoch(self) -> ConnectionEpoch:
        return ConnectionEpoch()

    @cached_property
    def connections(self) -> ThreadConnection:
        return ThreadConnection(self, self.tls, self.timeout_seconds, self.connection_epoch)

    def reset_connections(self) -> None:
        """Invalidate kept HTTPS connections across every thread so the next call dials fresh."""
        self.connection_epoch.bump()

    def post[R: ToolResult](self, request: ToolRequest[R]) -> dict[str, JsonValue]:
        """One posted tools/call and its reply; a dropped connection is the request's to answer."""
        try:
            try:
                return self.exchange(request)
            except ConnectionDropped as dropped:
                return request.dropped(self, dropped)
        except OSError as error:
            # This boundary knows the transport failed, not whether a tool ran.
            # The request owns that distinction; only reads can be deferred.
            return request.unavailable(error)

    def exchange[R: ToolResult](
        self, request: ToolRequest[R],
    ) -> dict[str, JsonValue]:
        """One round trip on this thread's kept connection, so a call pays no new handshake."""
        envelope = ToolsCallMessage(jsonrpc="2.0", id=1, params=request.called())
        return FieldCodec.decode(ToolsCallReply, json.loads(self.round_trip(envelope))).structured()

    def round_trip(self, envelope: object) -> bytes:
        """One JSON-RPC message posted on this thread's kept connection, and its reply's bytes.
        A dropped connection may be a restarted connector, which may serve other tools."""
        body = FieldCodec.compact(FieldCodec.encode(envelope)).encode()
        thread_connection = self.connections
        connection = thread_connection.prepare(
            self.connection_epoch.value, self.keepalive_idle_seconds, time.monotonic(),
        )
        try:
            connection.request("POST", self.endpoint.url.raw_path_qs, body, self.headers)
            response = connection.getresponse()
            payload = response.read()
        except (OSError, http.client.HTTPException) as error:
            connection.close()
            thread_connection.last_used = 0.0
            self.forget_served()
            raise ConnectionDropped(
                f"The connection to {self.endpoint.url.host} dropped: {error}") from error
        thread_connection.mark_used(time.monotonic())
        if response.headers.get("Content-Encoding") == "gzip":
            payload = gzip.decompress(payload)
        if response.status >= 400:
            raise urllib.error.HTTPError(
                str(self.endpoint.url), response.status, response.reason, response.headers,
                io.BytesIO(payload))
        return payload

    @cached_property
    def served_tools(self) -> frozenset[str]:
        """The names of the tools this connector exposes to this caller, listed once per
        connection: an older bus lists only the tools it had."""
        reply = FieldCodec.decode(ToolsListReply, json.loads(
            self.round_trip(ToolsListMessage(jsonrpc="2.0", id=1))))
        return frozenset(tool.name for tool in reply.result.tools)

    def serves(self, request_type: type[ToolRequest]) -> bool:
        return request_type.declared_name in self.served_tools

    def forget_served(self) -> None:
        self.__dict__.pop("served_tools", None)

    def post_observed[R: ToolResult](self, request: ObservedRequest[R]) -> dict[str, JsonValue]:
        """A call the client asked progress for: its events reach the request's observer, and a
        cancel there reaches the running call on Core."""
        return self.run_awaited(self.streamed(request))

    async def streamed[R: ToolResult](
        self, request: ObservedRequest[R],
    ) -> dict[str, JsonValue]:
        call_id = RpcCallId.mint()
        called = request.called()
        envelope = ToolsCallMessage(jsonrpc="2.0", id=call_id, params=replace(
            called, meta={**called.meta, "progressToken": call_id}))
        async with self.route.socket_session(timeout=STREAM_CONNECT_TIMEOUT, ssl=self.tls) as session:
            cancelled = asyncio.Event()
            watcher = asyncio.create_task(self.cancel_when_stopped(session, request, call_id, cancelled))
            try:
                async with session.post(
                    str(self.endpoint.url), data=FieldCodec.compact(FieldCodec.encode(envelope)),
                    headers={**self.headers, "Accept": "text/event-stream"},
                ) as response:
                    replies: list[ToolsCallReply] = []
                    async for line in response.content:
                        StreamedLine(line).delivered(request.observer, replies)
            finally:
                watcher.cancel()
        (reply,) = replies
        if cancelled.is_set():
            raise WireExportCancelled("Wire export cancelled")
        return reply.structured()

    async def cancel_when_stopped[R: ToolResult](
        self, session: aiohttp.ClientSession, request: ObservedRequest[R], call_id: JsonRpcId,
        cancelled: asyncio.Event,
    ) -> None:
        """The observer's stop reaches the running call on Core as one cancel."""
        await request.observer.stopped()
        cancelled.set()
        notice = CancelledMessage(jsonrpc="2.0", params={"requestId": call_id})
        async with session.post(str(self.endpoint.url), data=FieldCodec.compact(
                FieldCodec.encode(notice)), headers=self.headers):
            return
