"""A UI's Core on another machine, reached through the connector's tools/call."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import urllib.request
from multiprocessing.connection import Connection

from yarl import URL

from .connector import ToolsCallMessage, ToolsCallReply
from .connector_profile import BearerSecret, UiConnectionProfile
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

    def __init__(self, access: RemoteCoreAccess) -> None:
        self.url = access.endpoint.url.with_path("/observe")
        viewer, relay = socket.socketpair()
        self.connection = ViewerEnd(viewer.detach())
        self.ending = "the connector closed the observation"
        self.thread = threading.Thread(
            target=asyncio.run, args=(self.carry(RelayConnection(relay.detach()), access.credential.token),),
            name="agent-comms-remote-observation", daemon=True)
        self.thread.start()

    async def carry(self, local: Connection, token: BearerSecret) -> None:
        import aiohttp

        try:
            async with aiohttp.ClientSession() as session, session.ws_connect(
                self.url, headers={"Authorization": f"Bearer {token}"},
            ) as remote:
                self.ending = await relay_frames(local, remote)
        except (OSError, aiohttp.ClientError) as error:
            # Connecting is the boundary: the connector refused or could not be reached.
            self.ending = f"{type(error).__name__}: {error}"
        finally:
            local.close()

    def join(self, timeout: float | None = None) -> None:
        self.thread.join(timeout)

    def died(self, error: str | None) -> ObserverDied:
        return ConnectorObserverClosed(self.url, error or self.ending)


class RemoteCoreAccess(UiConnectionProfile, CoreAccess, ObservationSource):
    """The owner's UI profile is the remote access: its endpoint and credential are all a
    call needs, and the server still decides scope, expiry and revocation."""

    timeout_seconds = 30

    def observation_peer(self) -> ConnectorObservationPeer:
        return ConnectorObservationPeer(self)

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
        with urllib.request.urlopen(posted, timeout=self.timeout_seconds) as response:
            reply = FieldCodec.decode(ToolsCallReply, json.load(response))
        if reply.result.is_error:
            raise RelationViolationError(" ".join(content.text for content in reply.result.content))
        return FieldCodec.decode(request.result_type(), reply.result.structured_content)
