"""The connector's serving proof, shared by its CLI and systemd lifecycle."""

from __future__ import annotations

import json
import sys
import urllib.request
from dataclasses import dataclass, field

from . import ServiceNotServingError, SystemdUserService
from ..active_route import ActiveRoute
from ..comms import client_wire
from ..connector import CALLER, LOOPBACK, PORT, ToolCall, ToolsCallMessage, ToolsCallReply
from ..connector_store import ConnectorStore
from ..field_codec import FieldCodec, MalformedRecord
from ..errors import RelationViolationError, ToolRefusedError
from ..launch_root import LaunchRoot
from ..tools import CommsChannelsTool


@dataclass(frozen=True)
class RoundTripResult:
    round_trip: str


@dataclass(frozen=True, kw_only=True)
class ConnectorService:
    """A configured endpoint; proving it needs no invented systemd process identity."""

    launch: LaunchRoot = field(default_factory=ActiveRoute.published_launch)
    store: ConnectorStore = field(default_factory=ConnectorStore.owned)
    port: int = PORT

    @property
    def result(self) -> RoundTripResult:
        return RoundTripResult(f"{CommsChannelsTool.declared_name} read alike on port {self.port}")

    def serves(self) -> None:
        read = ToolCall.of(CommsChannelsTool)
        call = ToolsCallMessage(jsonrpc="2.0", id=0, params=read)
        try:
            with self.store.granted("upgrade round trip") as token:
                request = urllib.request.Request(
                    f"http://{LOOPBACK}:{self.port}/mcp",
                    data=json.dumps(FieldCodec.encode(call)).encode(),
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                )
                with urllib.request.urlopen(request, timeout=60) as response:
                    reply = FieldCodec.decode(ToolsCallReply, json.load(response)).result
            if reply.is_error:
                raise ServiceNotServingError(
                    f"Connector round trip returned an error: {' '.join(item.text for item in reply.content)}"
                )
            with client_wire(self.launch.path) as comms:
                if reply.structured_content != CommsChannelsTool.invoke(comms, {}, caller=CALLER):
                    raise ServiceNotServingError(
                        f"Connector on port {self.port} reads another bus than {self.launch.path}"
                    )
        except (MalformedRecord, json.JSONDecodeError, OSError,
                RelationViolationError, ToolRefusedError) as error:
            raise ServiceNotServingError(f"Connector round trip failed: {error}") from error
        print(f"connector round trip: {self.result.round_trip}", file=sys.stderr)


@dataclass(frozen=True)
class SystemdConnectorService(ConnectorService, SystemdUserService):
    """Systemd adds process lifetime to the same configured endpoint and serving proof."""

    unit = "agent-comms-connector.service"
