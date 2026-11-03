"""A UI's Core on another machine, reached through the connector's tools/call."""

from __future__ import annotations

import asyncio
import json
import socket
import ssl
import threading
import urllib.request
from multiprocessing.connection import Connection

from yarl import URL

from .connector import ToolsCallMessage, ToolsCallReply
from .connector_profile import HttpsMcpEndpoint, UiConnectionProfile, UiCredential
from .core_access import CoreAccess
from .errors import RelationViolationError
from .nominal.field_codec import FieldCodec
from .tool_caller import ToolCaller
from .tool_request import ToolRequest
from .tool_result import ToolResult
from .ui_model.observation import ObservationPeer, ObservationSource, ObserverDied, RelayConnection, ViewerEnd, relay_frames


class ConnectorObserverClosed(ObserverDied):
    """The connector ended a remote UI's observation; there is no local process to describe."""

    def __init__(self, url: URL, error: str | None):
        self.pid, self.exit_status, self.error = 0, None, error
        super(ObserverDied, self).__init__(
            f"The comms observation at {url} closed" + (f":\n{error}" if error else ""))


class ConnectorObservationPeer(ObservationPeer):
    """A Core across the connector serves this UI's observation. Frames relay between the
    UI's ViewerEnd and the connector's /observe socket from a thread with its own event
    loop, so the UI's loop is never borrowed; the frames are declared records both ways."""

    def __init__(self, access: ConnectorClient) -> None:
        self.url = access.endpoint.url.with_path("/observe")
        viewer, relay = socket.socketpair()
        self.connection = ViewerEnd(viewer.detach())
        self.ending = "the connector closed the observation"
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
            self.ending = "the UI retired the observation"
        finally:
            with self.lifetime:
                # The relay closed its local side, so no worker thread stays blocked on it;
                # they all finish before the loop does.
                self.loop.run_until_complete(self.loop.shutdown_default_executor())
                self.loop.close()

    async def carry(self, local: Connection, access: ConnectorClient) -> None:
        import aiohttp

        try:
            async with aiohttp.ClientSession() as session, session.ws_connect(
                self.url, headers={"Authorization": f"Bearer {access.credential.token}"},
                ssl=access.tls(),
            ) as remote:
                self.ending = await relay_frames(local, remote)
        except (OSError, aiohttp.ClientError) as error:
            # Connecting is the boundary: the connector refused or could not be reached.
            self.ending = f"{type(error).__name__}: {error}"
        finally:
            local.close()

    def join(self, timeout: float | None = None) -> None:
        self.thread.join(timeout)

    def retire(self) -> None:
        """End the relay now, even while the connector is still connecting, then wait."""
        with self.lifetime:
            if not self.loop.is_closed():
                self.loop.call_soon_threadsafe(self.task.cancel)
        self.join()

    def service_failed(self, traceback: str) -> ObserverDied:
        return ConnectorObserverClosed(self.url, traceback)

    def connection_closed(self) -> ObserverDied:
        """carry records the relay's end before it closes the local side, so the viewer's
        end-of-stream always finds that reason recorded."""
        return ConnectorObserverClosed(self.url, self.ending)


class ConnectorClient(ObservationSource):
    """Reaching one connector: its endpoint, the credential it accepts, and the TLS trust
    both the calls and the observation use."""

    endpoint: HttpsMcpEndpoint
    credential: UiCredential

    def tls(self) -> ssl.SSLContext:
        """One context for every connection to this connector, built when connecting, so the
        trust the owner configures (``SSL_CERT_FILE`` included) applies to calls and
        observation alike."""
        return ssl.create_default_context()

    def observation_peer(self) -> ConnectorObservationPeer:
        return ConnectorObservationPeer(self)


class RemoteCoreAccess(UiConnectionProfile, CoreAccess, ConnectorClient):
    """The owner's UI profile is the remote access: its endpoint and credential are all a
    call needs, and the server still decides scope, expiry and revocation."""

    timeout_seconds = 30

    def call[R: ToolResult](self, request: ToolRequest[ToolCaller, R]) -> R:
        envelope = ToolsCallMessage(jsonrpc="2.0", id=1, params=request.called())
        posted = urllib.request.Request(
            str(self.endpoint.url),
            data=FieldCodec.compact(FieldCodec.encode(envelope)).encode(),
            headers={
                "Authorization": f"Bearer {self.credential.token}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(posted, timeout=self.timeout_seconds, context=self.tls()) as response:
            reply = FieldCodec.decode(ToolsCallReply, json.load(response))
        if reply.result.is_error:
            raise RelationViolationError(" ".join(content.text for content in reply.result.content))
        return FieldCodec.decode(request.result_type(), reply.result.structured_content)
