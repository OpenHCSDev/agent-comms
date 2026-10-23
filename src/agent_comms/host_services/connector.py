"""The connector's serving proof, shared by its CLI and systemd lifecycle."""

from __future__ import annotations

import json
import sys
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Self

from ..active_route import ActiveRoute
from ..comms import client_wire
from ..connector import (
    CALLER,
    LOOPBACK,
    PORT,
    ConnectorOptions,
    ConnectorVerb,
    ServeVerb,
    ToolCall,
    ToolsCallMessage,
    ToolsCallReply,
)
from ..connector_store import ConnectorStore
from ..errors import RelationViolationError, ToolRefusedError
from ..field_codec import Argument, FieldCodec, MalformedRecord, PathText
from ..launch_root import LaunchRoot
from ..tools import CommsChannelsTool
from . import ServiceNotServingError, SystemdUserService


@dataclass(frozen=True)
class RoundTripResult:
    round_trip: str


@dataclass(frozen=True, kw_only=True)
class ConnectorService:
    """A configured endpoint; proving it needs no invented systemd process identity."""

    launch: LaunchRoot = field(default_factory=ActiveRoute.published_launch)
    store: ConnectorStore = field(default_factory=ConnectorStore.owned)
    port: int = PORT

    def described(self) -> str:
        return f"Connector on port {self.port} for {self.launch.path}"

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
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                )
                with urllib.request.urlopen(request, timeout=60) as response:
                    reply = FieldCodec.decode(ToolsCallReply, json.load(response)).result
            if reply.is_error:
                raise ServiceNotServingError(
                    "Connector round trip returned an error: "
                    f"{' '.join(item.text for item in reply.content)}"
                )
            with client_wire(self.launch.path) as comms:
                if reply.structured_content != CommsChannelsTool.invoke(comms, {}, caller=CALLER):
                    raise ServiceNotServingError(
                        f"Connector on port {self.port} reads another bus than {self.launch.path}"
                    )
        except (
            MalformedRecord,
            json.JSONDecodeError,
            OSError,
            RelationViolationError,
            ToolRefusedError,
        ) as error:
            raise ServiceNotServingError(f"Connector round trip failed: {error}") from error
        print(f"connector round trip: {self.result.round_trip}", file=sys.stderr)


@dataclass(frozen=True)
class SystemdConnectorService(ConnectorService, SystemdUserService):
    """Systemd adds process lifetime to the same configured endpoint and serving proof."""

    program = ConnectorVerb.program
    unit = f"{program}.service"

    @classmethod
    def running(cls) -> tuple[Self, ...]:
        pid = cls._pid_for(cls.unit)
        return () if pid == 0 else (cls.from_process(pid),)

    @classmethod
    def from_process(cls, pid: int) -> Self:
        # Our installed console script's argv is interpreter, script, verb, options.
        endpoint = ServeVerb.parse(cls.running_arguments(pid)[2:])
        return cls(launch=LaunchRoot.from_path(endpoint.root), port=endpoint.port)

    def exec_arguments(self, issuer: str) -> tuple[str, ...]:
        endpoint = ServeVerb(root=self.launch.path, port=self.port, issuer=issuer)
        return endpoint.command_arguments()


@dataclass(frozen=True, kw_only=True)
class InstallServiceVerb(ConnectorOptions, declared_name="install-service"):
    """Write and enable the connector's declared unit without starting it."""

    help = "Install the complete user service unit through the current runtime"

    def apply(self, ctx: ConnectorStore) -> int | str:
        service = SystemdConnectorService(
            launch=LaunchRoot.from_path(self.root),
            store=ctx,
            port=self.port,
        )
        service.install(service.exec_arguments(self.issuer), home=Path.home())
        service.enable()
        print(service.unit_file(Path.home()))
        return 0


@dataclass(frozen=True, kw_only=True)
class RoundTripVerb(ConnectorVerb):
    """The same serving proof required after an install restarts the connector."""

    help = "Read the bus through the running connector service and compare"
    root: Annotated[
        Path, PathText, Argument(flags=("root",), options={"help": "The comms root it proves"})
    ]
    port: Annotated[
        int, Argument(flags=("--port",), options={"help": "The service's loopback port"})
    ] = PORT

    def apply(self, ctx: ConnectorStore) -> int | str:
        service = ConnectorService(
            launch=LaunchRoot.from_path(self.root), store=ctx, port=self.port
        )
        try:
            service.serves()
        except ServiceNotServingError as error:
            return str(error)
        print(json.dumps(FieldCodec.encode(service.result)))
        return 0
