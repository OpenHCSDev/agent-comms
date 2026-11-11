"""agent-comms-connector: the claude.ai connector, a Streamable HTTP MCP server beside Core.

The server is a transport over the ToolRequest family: ``tools/list`` reports every member
and whether it changes the bus; ``tools/call`` runs ``invoke()`` as the caller identified
by its bearer grant.
OAuth stands in front of every MCP request. An unknown browser waits for the owner's
single-use approval link sent on comms. Its page requires a POST confirmation and then
remembers that browser on this issuer;
subsequent sign-ins need no repeated step. Grant storage retains only secret digests under
``~/.local/state/agent-comms/connector/``.

Usage:
  agent-comms-connector serve --root ROOT [--port PORT] [--issuer URL]
  agent-comms-connector round_trip ROOT [--port PORT]
  agent-comms-connector install-service --root ROOT [--port PORT] [--issuer URL]
  agent-comms-connector revoke
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import json
import logging
import os
import secrets
import signal
import socket
import sys
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, contextmanager, nullcontext, suppress
from dataclasses import dataclass, field, replace
from functools import cached_property, partial
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, ClassVar, Self, cast

from aiohttp import web
from aiohttp.typedefs import Handler
from aiohttp.web_urldispatcher import AbstractResource, ResourceRoute, SystemRoute
from yarl import URL

from agent_comms.cli_bridges.claude_transcript import WATCH_REPLACED
from agent_comms.cli_bridges.inbox_watch import InboxWatch, OpenWatch, WatchRoute, WatchTickets
from agent_comms.cli_bridges.open_viewer import OpenObservation, OpenViewer
from agent_comms.cli_verbs.declared_verb import DeclaredVerb
from agent_comms.comms import Comms, client_wire, wire
from agent_comms.coordination.coordinator import Coordination
from agent_comms.coordination.service_passes import ServicePassStore
from agent_comms.execution.child_process import (
    GitChildren,
    HelperStart,
    OwnedGitOpening,
    SpawnedHelper,
    join_retirement,
)
from agent_comms.execution.launch_root import started
from agent_comms.execution.watch_passes import Watch
from agent_comms.host_services import HostService
from agent_comms.installer.active_route import CommsRoute, resolve_comms_route
from agent_comms.messaging.bus_identity import BusIdentityStore
from agent_comms.messaging.channel_targets import NamedTarget
from agent_comms.messaging.messages import QuestionKind
from agent_comms.messaging.send_result import MessagePublished
from agent_comms.messaging.task_sources import OwnerRequestAttachment
from agent_comms.nominal.declared_family import DeclaredFamily, UnknownFamilyMember
from agent_comms.nominal.field_codec import (
    Argument,
    FieldCodec,
    GitCommitText,
    MalformedRecord,
    PathText,
    WireTransient,
    boundary_decoder,
)
from agent_comms.planning.command import Command
from agent_comms.planning.owner_requests import NewestOpenRequest, RequestSelection
from agent_comms.projects.checkouts import Checkout
from agent_comms.projects.closed_pull_requests import ClosedPullRequestWatch
from agent_comms.registry.registry_store import RegistryStore
from agent_comms.registry.service_sender import ServiceSender
from agent_comms.storage.locked_store import DocumentChange
from agent_comms.team.standing import UnownedOrderWatch
from agent_comms.team.team_heartbeat import TeamHeartbeat
from agent_comms.transport.connector_caller import (
    CALLER,
    ConnectorCaller,
    ConnectorTool,
    GrantRefusedError,
)
from agent_comms.transport.connector_profile import (
    MCP_PATH,
    OBSERVE_PATH,
    HttpsMcpEndpoint,
    UiConnectionProfile,
    UiGrantId,
    UiGrantSummary,
)
from agent_comms.transport.connector_store import (
    CODE_SECONDS,
    ApprovalQueueFull,
    ApprovalRefused,
    AuthorizationQuery,
    AuthorizationRequest,
    ClaudeCallback,
    ConnectorGrants,
    ConnectorStore,
    HeldUiProfiles,
    IssuedTokens,
    RegisteredClient,
    TrustedBrowser,
    UiGrantAccess,
    UiReadAccess,
    digest,
)
from agent_comms.transport.mcp import (
    AnsweredCall,
    InvalidRequestError,
    JsonRpcId,
    McpError,
    McpErrorReply,
    McpMessage,
    McpServer,
    McpSession,
    MethodNotFoundError,
    ParseError,
    declared,
)
from agent_comms.transport.progress import CancellableWireExport, WireExportObserver
from agent_comms.transport.request import tool_field
from agent_comms.transport.served_endpoints import OfferedByTailnet, ServedEndpoints
from agent_comms.transport.tool_caller import ThreadCaller
from agent_comms.transport.tool_request import ConnectorService, NoConnectorService, ServedRequest
from agent_comms.transport.tool_result import ToolResult
from agent_comms.transport.tools.orders import CommsHeadVerdictTool, HeadVerdictTool
from agent_comms.transport.tools.send import CommsSendTool, SendTool
from agent_comms.ui_model.observation import RelayConnection, relay_frames, serve_encoded

"""A JSON-RPC request id, as the client chose it."""


LOOPBACK = "127.0.0.1"
PORT = 18766


class SystemdSite(web.TCPSite):
    """The listening site notifies its supervisor only after binding succeeds."""

    notify_variable: ClassVar[str] = "NOTIFY_SOCKET"  # systemd's supervisor socket

    async def start(self) -> None:
        await super().start()
        address = os.environ[self.notify_variable]
        target = "\0" + address[1:] if address.startswith("@") else address
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notify:
            notify.connect(target)
            notify.sendall(b"READY=1")


@dataclass(frozen=True, kw_only=True)
class ConnectorSendTool(SendTool, ConnectorTool, declared_name=CommsSendTool.declared_name):
    request: Annotated[
        RequestSelection,
        *tool_field("Owner request ID; omitted selects the recipient's newest open request"),
    ] = NewestOpenRequest()

    def attachment(self, comms: Comms, target: NamedTarget) -> OwnerRequestAttachment:
        requests = self.request.for_message(comms.goals.todos, target, comms.registry)
        return OwnerRequestAttachment(tuple(request.id for request in requests))


@dataclass(frozen=True, kw_only=True)
class ConnectorWatchTool(ConnectorTool, declared_name="comms_watch"):
    label = "Watch Inbox"
    description = (
        "Get a one-use wss:// URL that pushes your inbox, so a message wakes this session. Open it "
        "at once by running agent-comms-inbox-watch URL under your Monitor tool, with the longest "
        "timeout; from an agent-comms checkout, uv run --locked --no-dev agent-comms-inbox-watch URL. "
        "Reading your inbox is not this: only a running watch wakes you. Each message "
        "arrives as an event naming a file to read, and your thread shows as bridged while it runs. "
        "The URL expires in a minute. When the Monitor expires or the watch says it ended, call "
        "this again. Never post the URL anywhere."
    )
    service: Annotated[ConnectorService, WireTransient()] = field(
        default_factory=NoConnectorService, compare=False)

    def served_by(self, service: ConnectorService) -> Self:
        return replace(self, service=service)

    def apply(self, comms: Comms, caller: ThreadCaller) -> ToolResult:
        return self.service.watch_url(caller.identity(comms).name)


@dataclass(frozen=True, kw_only=True)
class ConnectorHeadVerdictTool(HeadVerdictTool, ConnectorTool,
                               declared_name=CommsHeadVerdictTool.declared_name):
    """Director and native callers share the same persisted verdict and publication path."""


@dataclass
class OAuthError(Exception):
    """An OAuth refusal owns its RFC 6749 response as well as its exception text."""

    error: str
    error_description: str

    def __post_init__(self) -> None:
        super().__init__(self.error_description)

    def response(self) -> web.Response:
        return web.json_response(FieldCodec.encode(self), status=400)


@dataclass(frozen=True)
class ApprovalLink:
    """The approval route's payload, decoded once at the HTTP boundary."""

    request_id: str
    secret: str = field(repr=False)
    route: ClassVar[str] = "/approve/{request_id}/{secret}"


class ApprovalAudience(DeclaredFamily, affix="Audience"):
    """Who may read a one-use approval link, decided by what reaching this connector proved.

    `transport/served_endpoints.py` already owns that distinction for this machine's URLs:
    reaching a tailnet-only one means tailscale admitted the caller's machine first
    (`TailnetServed.offers_profiles`), while a Funnel URL "proves nothing about its caller".
    Tailscale marks every Funnel request with its own header, so the same rule decides it
    per request, once, at the HTTP boundary.
    """

    funnel_header: ClassVar[str] = "Tailscale-Funnel-Request"

    @classmethod
    def of(cls, request: web.Request) -> ApprovalAudience:
        """Only tailscale sets this header, and only on the way in from the internet; a
        caller cannot clear it, and adding it only gives itself the stricter member."""
        return PublicAudience() if cls.funnel_header in request.headers else TailnetAudience()

    @abstractmethod
    def deliver(self, connector: Connector, client: str, link: str) -> str:
        """Hand the link to whoever may read it and answer with the page that says so."""


@dataclass(frozen=True, slots=True)
class TailnetAudience(ApprovalAudience):
    """Came in over the tailnet, which tailscale admitted before this connector saw it. The
    person at that browser is the owner, so sending them elsewhere to fetch their own link
    protects nobody and is the step that strands them when comms cannot deliver."""

    def deliver(self, connector: Connector, client: str, link: str) -> str:
        return (f"<p>Approve {html.escape(json.dumps(client))} and remember this browser:</p>"
                f'<p><a href="{html.escape(link)}">{html.escape(link)}</a></p>')


@dataclass(frozen=True, slots=True)
class PublicAudience(ApprovalAudience):
    """Came in through Funnel, where anyone who knows the hostname can reach `/authorize`.
    Showing the link here would let a stranger approve their own browser, so only the owner's
    own comms thread reads it."""

    def deliver(self, connector: Connector, client: str, link: str) -> str:
        connector.request_approval(client, link)
        return ("<p>An approval link was sent to the owner on comms.</p>"
                "<p>Open that link and confirm to connect and remember this browser.</p>")


@dataclass(frozen=True)
class ClientRegistration:
    """Dynamic client registration accepts exactly claude.ai's redirect."""

    redirect_uris: tuple[ClaudeCallback]
    client_name: str = "unnamed client"


@dataclass(frozen=True, kw_only=True)
class TokenGrant(DeclaredFamily, affix="Grant"):
    family_discriminator: ClassVar[str] = "grant_type"
    client_id: str
    resource: str = ""

    @classmethod
    def received(cls, form: Mapping[str, object]) -> Self:
        return FieldCodec.decode(cls, dict(form))

    @abstractmethod
    def redeemed(self, grants: ConnectorGrants) -> ConnectorGrants:
        """The grants with this credential spent; refuses a credential it does not hold."""

    def exchange(self, grants: ConnectorGrants) -> DocumentChange[ConnectorGrants, IssuedTokens]:
        return self.redeemed(grants).issued(self.client_id)


@dataclass(frozen=True, kw_only=True)
class AuthorizationCodeGrant(TokenGrant):
    code: str
    code_verifier: str
    redirect_uri: ClaudeCallback

    def redeemed(self, grants: ConnectorGrants) -> ConnectorGrants:
        issued = grants.codes.get(digest(self.code))
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(self.code_verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        if (
            issued is None
            or not issued.live(time.time())
            or (issued.client_id, issued.code_challenge) != (self.client_id, challenge)
        ):
            raise OAuthError("invalid_grant", "The code is unknown, expired or not this client's.")
        return replace(grants, codes={k: v for k, v in grants.codes.items() if v is not issued})


@dataclass(frozen=True, kw_only=True)
class RefreshTokenGrant(TokenGrant):
    refresh_token: str

    def redeemed(self, grants: ConnectorGrants) -> ConnectorGrants:
        refresh = dict(grants.refresh)
        if refresh.pop(digest(self.refresh_token), None) != self.client_id:
            raise OAuthError(
                "invalid_grant", "The refresh token is unknown, revoked or not this client's."
            )
        return replace(grants, refresh=refresh)


@dataclass(frozen=True)
class ResourceMetadata:
    resource: str
    authorization_servers: tuple[str, ...]
    bearer_methods_supported: tuple[str, ...] = ("header",)


@dataclass(frozen=True)
class ServerMetadata:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str
    response_types_supported: tuple[str, ...] = ("code",)
    grant_types_supported: tuple[str, ...] = field(default_factory=TokenGrant.names)
    code_challenge_methods_supported: tuple[str, ...] = ("S256",)
    token_endpoint_auth_methods_supported: tuple[str, ...] = ("none",)


@dataclass(frozen=True, kw_only=True)
class ClientRegistrationReply(ClientRegistration):
    client_id: str
    client_id_issued_at: int
    grant_types: tuple[str, ...] = field(default_factory=TokenGrant.names)
    response_types: tuple[str, ...] = ("code",)
    token_endpoint_auth_method: str = "none"


class RouteBound(ABC):
    """Whether a route staying open this long is a defect or its job.

    Measured on 2026-11-08 from this connector's own access lines: POST /mcp answered
    25,836 requests at p50 0.019s, p99 0.287s, p999 0.888s, and the one hang at 6883.222s.
    GET /observe answered 140 at p50 6.823s, p99 2036.403s, up to 2830.038s, because
    staying open is what it is for. One bound over both would call 140 healthy observes
    overdue, so the route owns this and nothing global can.
    """

    __slots__ = ()

    @abstractmethod
    def overdue(self, waited: float) -> bool: ...

    @abstractmethod
    def answering(self, waited: float) -> AbstractAsyncContextManager[object]:
        """Bounds the wait for an answer, never the work: a call stays in self.calls and
        stop_calls still joins it, so a mutation finishes even when the request gives up."""


class BoundedRoute(RouteBound):
    """A route that answers and returns, bounded at 60s.

    Not a tuned number. Over three days and 31,465 POST /mcp samples, seven ran past five
    seconds, the longest healthy one 29.993s, and exactly one passed sixty: the 6883.222s
    hang. So this fails nothing that has ever worked.
    """

    __slots__ = ()
    seconds: ClassVar[float] = 60.0

    def overdue(self, waited: float) -> bool:
        return waited > self.seconds

    def answering(self, waited: float) -> AbstractAsyncContextManager[object]:
        return asyncio.timeout(max(0.0, self.seconds - waited))


class LongLivedRoute(RouteBound):
    """A route whose request is meant to outlive the exchange: an observation or a watch."""

    __slots__ = ()

    def overdue(self, waited: float) -> bool:
        return False

    def answering(self, waited: float) -> AbstractAsyncContextManager[object]:
        return nullcontext()


BOUNDED_ROUTE = BoundedRoute()
LONG_LIVED_ROUTES: Mapping[str, RouteBound] = MappingProxyType(
    {OBSERVE_PATH: LongLivedRoute(), "/watch/{ticket}": LongLivedRoute()})


class RequestWait(ABC):
    """How long a running task's request has been unanswered.

    A task the loop reports may be a watch, a tool call or a request handler, and only the
    last has a request behind it. The absent member answers for the others, so the report
    asks one question instead of testing for a missing entry.
    """

    __slots__ = ()

    def waited(self, now: float) -> str:
        return ""


class NoRequest(RequestWait):
    """This task answers no request: a watch, a helper or a tool call."""

    __slots__ = ()


NO_REQUEST = NoRequest()


@dataclass(frozen=True, slots=True, kw_only=True)
class RunningRequest(RequestWait):
    """A request this process accepted and has not answered yet.

    log_route already holds the one fact a hang needs and prints it only once the handler
    ends: on 2026-11-08 a POST /mcp ran 6883 seconds and its access line never appeared, so
    nothing said how long it had been stuck. Carrying it here puts the age beside the phase.
    """

    method: str
    template: str
    started: float
    bound: RouteBound = BOUNDED_ROUTE

    def waited(self, now: float) -> str:
        elapsed = now - self.started
        overdue = " OVERDUE" if self.bound.overdue(elapsed) else ""
        return f"{overdue} {elapsed:.3f}s into {self.method} {self.template},"


class ApprovalSender(ServiceSender):
    """Who the connector is when it speaks for itself, the way `installer (service)` does.

    `CALLER` stays the authority a granted token acts as, which is a real agent on this bus.
    Borrowing it to send the owner's sign-in link meant any bus without an agent of that name
    answered with a 500. Kept off `Connector` because its OAuth client-registration route is
    already named `register`, and one class cannot own two facts under one name.
    """

    sender: ClassVar[str] = "connector"


@dataclass(frozen=True)
class Connector(ConnectorService, McpServer):
    """The MCP endpoint and the OAuth authorization server in front of it."""

    heartbeat_seconds: ClassVar[float] = 25.0
    """How often a watch socket is pinged; a client that misses the pong is dropped."""
    stack_frames: ClassVar[int] = 6
    """How deep a still-running task's stack is reported; the innermost frames name its phase."""
    helper_start: ClassVar[HelperStart] = SpawnedHelper()
    """How a remote viewer's observation process starts."""

    store: ConnectorStore
    issuer: str
    root: Path
    watches: Mapping[type[Watch], Watch] = field(
        default_factory=lambda: MappingProxyType({member: member() for member in Watch.members_with(Watch)}),
        init=False, repr=False, compare=False,
    )

    calls: dict[
        asyncio.Task[AnsweredCall], type[ServedRequest] | type[ConnectorTool],
    ] = field(
        default_factory=dict, init=False, repr=False, compare=False,
    )
    running_requests: dict[asyncio.Task[object], RequestWait] = field(
        default_factory=dict, init=False, repr=False, compare=False,
    )
    """Requests accepted and not yet answered, by the task answering each."""
    observed: dict[JsonRpcId, CancellableWireExport] = field(
        default_factory=dict, init=False, repr=False, compare=False,
    )
    git_children: GitChildren = field(default_factory=GitChildren, init=False, repr=False, compare=False)

    watch_tasks: set[asyncio.Task[None]] = field(
        default_factory=set, init=False, repr=False, compare=False,
    )
    open_viewers: list[OpenViewer] = field(
        default_factory=list, init=False, repr=False, compare=False,
    )
    """Every long-lived socket this process is serving, so the stop can end all of them."""

    session: McpSession = field(
        default_factory=McpSession, init=False, repr=False, compare=False,
    )

    def announces(self) -> str:
        """Read at each handshake rather than held: the identity is minted when serving
        starts, and a root prepared under this process should not keep answering that it
        has none."""
        return BusIdentityStore.serving(self.root).announced(str(self.root))

    @cached_property
    def watch_tickets(self) -> WatchTickets:
        return WatchTickets(self.store)

    def watch_url(self, thread: str) -> ToolResult:
        return self.watch_tickets.issued(thread, URL(self.issuer))

    async def inbox_watch(self, request: web.Request) -> web.WebSocketResponse:
        """The ticket in the path is the only credential; log_route logs the route's template.
        A newer socket for a thread replaces the older one, as a re-armed watch does."""
        route = FieldCodec.decode(WatchRoute, dict(request.match_info))
        # Both write the connector's store, so they run off the loop.
        ticket = await asyncio.to_thread(self.watch_tickets.taken, route.ticket)
        thread = ticket.thread
        offer = await asyncio.to_thread(self.watch_tickets.reconnect, thread, URL(self.issuer))
        logging.info("Watch for %s opened on %s", thread, ticket.opened_on)
        # A transcript line holds whatever a tool returned, far past aiohttp's 4 MiB default.
        viewer = web.WebSocketResponse(heartbeat=self.heartbeat_seconds, max_msg_size=64 * 1024 * 1024)
        await viewer.prepare(request)
        watch = OpenWatch(thread=thread, viewer=viewer, handler=request.task)
        replaced = tuple(held for held in self.open_viewers if held.watching(thread))
        self.open_viewers.append(watch)
        for held in replaced:
            await held.viewer.close(code=WATCH_REPLACED, message=b"replaced by a newer watch")
        try:
            await ticket.opened(offer, viewer)
            await InboxWatch(thread, self.root, offer).carried(viewer)
        finally:
            self.open_viewers.remove(watch)
            # The access log says only that the socket ended; this says which side ended it.
            logging.info("Watch for %s ended with close code %s; error %r",
                         thread, viewer.close_code, viewer.exception())
        return viewer

    def waited(self, task: asyncio.Task[object], now: float) -> str:
        """How long this task's request has been unanswered, for a task that is one."""
        return self.running_requests.get(task, NO_REQUEST).waited(now)

    def still_running(self) -> str:
        """Every task this process has not finished, and where each one is sitting.

        The collections below name the work they own, and on 2026-11-08 that was not enough:
        a POST /mcp stayed open 6883 seconds and the stop reported "pending tools: none",
        because the handler that never finished is a request task and neither self.calls nor
        self.watch_tasks holds one. The event loop already tracks every task, so this asks
        the loop rather than adding a third registry.

        Each line reads leaf first, and the leaf is the phase: a completed access line is
        only printed once the handler ends, so that frame is the one fact no journal can
        carry. asyncio.capture_call_graph owns walking a suspended await chain to reach it.
        Task.get_stack does not: on a handler awaiting session claim awaiting a body read it
        returns the handler frame alone, which names the task and not where it is stuck.
        """
        running = []
        now = time.monotonic()
        for task in sorted(asyncio.all_tasks(), key=lambda pending: pending.get_name()):
            if task.done():
                continue
            graph = asyncio.capture_call_graph(task, limit=self.stack_frames)
            where = " <- ".join(
                f"{entry.frame.f_code.co_name}"
                f" at {entry.frame.f_code.co_filename}:{entry.frame.f_lineno}"
                for entry in (graph.call_stack if graph is not None else ())
            )
            running.append(f"  {task.get_name()}:{self.waited(task, now)} "
                           f"{where or 'awaiting, no python frame'}")
        return "\n".join(running) or "  none"

    def log_pending(self, reason: str = "stopping") -> None:
        """The existing task owners name pending work; idle watches may also be listed."""
        logging.info("Connector %s; pending tools: %s; pending watches: %s\nstill running:\n%s",
                     reason,
                     ", ".join(tool.declared_name for task, tool in self.calls.items()
                               if not task.done()) or "none",
                     ", ".join(sorted(task.get_name() for task in self.watch_tasks
                                      if not task.done())) or "none",
                     self.still_running())

    async def call(
        self, tool: type[ServedRequest] | type[ConnectorTool],
        arguments: Mapping[str, object], caller: ConnectorCaller, observer: WireExportObserver,
    ) -> AnsweredCall:
        """Start once; a disconnected HTTP client cannot abandon its write.

        The route's bound lives in log_route, around the whole handler, so a request that
        stalls before ever reaching here is bounded too. The shield is this method's own
        job and stays: when that bound gives up, `pending` keeps running, self.calls still
        holds it and stop_calls still joins it, so a mutation under way finishes.
        """
        if self.git_children.closing.is_set():
            raise web.HTTPServiceUnavailable(text="Connector is stopping")
        with OwnedGitOpening(self.git_children).bound():
            pending = asyncio.create_task(tool.run_async(partial(self.invoke, tool, arguments, caller, observer)))
        self.calls[pending] = tool
        pending.add_done_callback(self.calls.pop)
        return await asyncio.shield(pending)

    async def begin_shutdown(self, app: web.Application) -> None:
        """Close acquisition launches before any shutdown hook can join entered work."""
        retirement = self.git_children.start_shutdown()
        for task in self.watch_tasks:
            task.cancel()
        for held in tuple(self.open_viewers):
            await held.ended()
        await join_retirement(retirement)

    async def stop_calls(self, app: web.Application) -> None:
        """The runner has stopped accepting: drop readers and join declared mutations."""
        pending = tuple(self.calls.items())
        for call, tool in pending:
            tool.cancel_on_shutdown(call)
        await asyncio.gather(*(call for call, tool in pending), return_exceptions=True)

    @property
    def heartbeat(self) -> TeamHeartbeat:
        return cast(TeamHeartbeat, self.watches[TeamHeartbeat])

    @property
    def resource_metadata_url(self) -> str:
        return f"{self.issuer}/.well-known/oauth-protected-resource"

    @contextmanager
    def opened_wire(self) -> Iterator[Comms]:
        """Open data at the served root; each caller checks its own access."""
        with client_wire(self.root) as comms:
            yield comms

    @contextmanager
    def service_wire(self) -> Iterator[Comms]:
        """A service pass selects its explicit root without borrowing a request route.

        Waiting with a client route borrowed would hold up its replacement. The
        pass inherits the bus's hold wait; requests retain client_wire's fast refusal.
        """
        yield wire(self.root)

    def invoke(
        self, tool: type[ServedRequest] | type[ConnectorTool], arguments: Mapping[str, object],
        caller: ConnectorCaller, observer: WireExportObserver,
    ) -> AnsweredCall:
        tool.require_entry_open(self.root)
        with self.opened_wire() as comms:
            acting, rest = caller.acting(arguments)
            caller.prepare_call(comms, acting)
            request = tool.decoded(comms, rest).bound(observer).served_by(self)
            return AnsweredCall(request.replied(comms, acting), request.board(comms, acting))

    def cancel_call(self, request_id: JsonRpcId) -> None:
        """A cancel names a running observed call by its request id; any other call runs on."""
        self.observed.get(request_id, CancellableWireExport()).cancel()

    def application(self) -> web.Application:
        self.store.read()  # Refuse an unreadable store before binding any port.
        app = web.Application(middlewares=[self.log_route])
        app.cleanup_ctx.append(self.watch_orders)
        app.on_shutdown.append(self.begin_shutdown)
        app.on_shutdown.append(self.stop_calls)
        app.add_routes(
            [
                web.get("/.well-known/oauth-protected-resource", self.resource_metadata),
                web.get(f"/.well-known/oauth-protected-resource{MCP_PATH}", self.resource_metadata),
                web.get("/.well-known/oauth-authorization-server", self.server_metadata),
                web.post("/register", self.register),
                web.get("/authorize", self.authorize, allow_head=False),
                web.get(
                    ApprovalLink.route, self.approve,
                    name=self.approve.__name__, allow_head=False,
                ),
                web.post(ApprovalLink.route, self.confirm),
                web.post("/token", self.token),
                web.post(MCP_PATH, self.mcp),
                web.get(OBSERVE_PATH, self.observe),
                web.get("/watch/{ticket}", self.inbox_watch),
            ]
        )
        return app

    @web.middleware
    async def log_route(self, request: web.Request, handler: Handler) -> web.StreamResponse:
        """One line per request naming its route template: paths and queries carry one-use secrets
        and OAuth values, and headers carry bearer tokens, so none of them is logged."""
        match request.match_info.route:
            case ResourceRoute(resource=AbstractResource(canonical=template)):
                pass
            case SystemRoute():
                template = "unmatched"
            case _:
                raise TypeError("Unsupported aiohttp route")
        started = time.monotonic()
        status = web.HTTPInternalServerError.status_code
        answering = asyncio.current_task()
        # This line prints only once the handler ends, so a handler that never ends is
        # invisible here. The same facts go where a stuck one can be asked for them.
        bound = LONG_LIVED_ROUTES.get(template, BOUNDED_ROUTE)
        self.running_requests[answering] = RunningRequest(
            method=request.method, template=template, started=started, bound=bound)
        try:
            # The whole handler, not just the tool call inside it. A request stalls in
            # bearer_caller, request.json or session dispatch long before it reaches
            # call(), so a bound that started there left an unfinished authenticated body
            # waiting forever (codex-latency, 2026-11-08). One scope owns this; call()
            # keeps only its shield, so a tool already running still finishes.
            async with bound.answering(0.0):
                response = await handler(request)
            status = response.status
            return response
        except TimeoutError:
            status = web.HTTPGatewayTimeout.status_code
            logging.warning("%s %s passed its route's bound; still running:\n%s",
                            request.method, template, self.still_running())
            raise web.HTTPGatewayTimeout(
                text=f"{request.method} {template} has not answered within its deadline; "
                     "work already started is shielded and will finish.") from None
        except web.HTTPException as refusal:
            status = refusal.status
            raise
        finally:
            self.running_requests.pop(answering, None)
            elapsed = time.monotonic() - started
            # A request that answers late still answered, so this reports and refuses
            # nothing: the route decides whether its own duration is news.
            logging.log(
                logging.WARNING if bound.overdue(elapsed) else logging.INFO,
                "%s %s %s %.3fs", request.method, template, status, elapsed)

    async def watch_orders(self, app: web.Application) -> AsyncIterator[None]:
        """Core owns both order maintenance and the teammate's changed-rule wakes."""
        passes = ServicePassStore.at(self.root)
        with OwnedGitOpening(self.git_children).bound():
            self.watch_tasks.update(
                asyncio.create_task(Coordination.watch_worker(
                    watch, self.service_wire, passes), name=type(watch).__name__)
                for watch in self.watches.values()
            )
        try:
            yield
        finally:
            for task in self.watch_tasks:
                task.cancel()
            for task in self.watch_tasks:
                with suppress(asyncio.CancelledError):
                    await task
            self.watch_tasks.clear()

    async def resource_metadata(self, request: web.Request) -> web.Response:
        return web.json_response(FieldCodec.encode(
            ResourceMetadata(f"{self.issuer}{MCP_PATH}", (self.issuer,))))

    async def server_metadata(self, request: web.Request) -> web.Response:
        return web.json_response(FieldCodec.encode(ServerMetadata(
            self.issuer, f"{self.issuer}/authorize", f"{self.issuer}/token",
            f"{self.issuer}/register")))

    async def register(self, request: web.Request) -> web.Response:
        try:
            registration = FieldCodec.decode(
                ClientRegistration, declared(ClientRegistration, await request.json())
            )
        except (MalformedRecord, json.JSONDecodeError) as error:
            return OAuthError("invalid_redirect_uri", str(error)).response()
        client_id = secrets.token_urlsafe(16)
        await asyncio.to_thread(
            self.store.update,
            lambda grants: DocumentChange.to(replace(
                grants,
                clients=grants.clients | {client_id: RegisteredClient(registration.client_name)},
            )),
        )
        return web.json_response(FieldCodec.encode(ClientRegistrationReply(
            client_id=client_id, client_id_issued_at=int(time.time()),
            client_name=registration.client_name, redirect_uris=registration.redirect_uris,
        )), status=201)

    async def authorize(self, request: web.Request) -> web.Response:
        """A trusted browser proceeds; an unknown one needs its owner's one-use link."""
        try:
            query = FieldCodec.decode(
                AuthorizationQuery, declared(AuthorizationQuery, request.query)
            )
        except MalformedRecord as error:
            raise web.HTTPBadRequest(text=str(error)) from error
        code, approval = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        browser = request.cookies.get(TrustedBrowser.cookie_name, "")

        def decided(grants: ConnectorGrants) -> DocumentChange[ConnectorGrants, ConnectorGrants]:
            now = time.time()
            grants = grants.unexpired(now)
            if query.client_id not in grants.clients:
                raise web.HTTPBadRequest(text="Unknown client.")
            if grants.trusts(browser):
                return DocumentChange.to(grants.code_issued(query, code, now))
            return DocumentChange.to(grants.waiting(query, digest(approval), now))

        try:
            grants = await asyncio.to_thread(self.store.update, decided)
        except ApprovalQueueFull as refusal:
            raise web.HTTPTooManyRequests(
                text=str(refusal), headers={"Retry-After": str(CODE_SECONDS)}
            ) from refusal
        if grants.trusts(browser):
            raise web.HTTPFound(query.redirect(code), headers={"Cache-Control": "no-store"})
        path = request.app.router[self.approve.__name__].url_for(
            **FieldCodec.encode(ApprovalLink(query.request_id, approval))
        )
        notice = await asyncio.to_thread(
            ApprovalAudience.of(request).deliver,
            self, grants.clients[query.client_id].name, f"{self.issuer}{path}",
        )
        return web.Response(
            content_type="text/html",
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
            text=f"<!doctype html><title>Approve connector</title>{notice}",
        )

    def request_approval(self, client: str, link: str) -> None:
        """Only the durable human identity receives the bearer approval link."""
        with self.opened_wire() as comms:
            owner = comms.messaging.user_identity(Checkout(self.root))
            ApprovalSender.register(comms.registry)
            comms.messaging.send(
                ApprovalSender.sender, owner.name,
                f"Connector sign-in requested by {json.dumps(client)}. "
                f"Open the link and confirm only if you started this connection; it expires in "
                f"{CODE_SECONDS} seconds and remembers this browser.\n{link}",
                QuestionKind,
                origin=ApprovalSender.origin,
            )

    async def approve(self, request: web.Request) -> web.Response:
        """A preview is read-only; only the form's POST can establish browser trust."""
        link = FieldCodec.decode(ApprovalLink, dict(request.match_info))
        grants = await asyncio.to_thread(self.store.read)
        try:
            held = grants.require_approval(link.request_id, link.secret, time.time())
        except ApprovalRefused as refusal:
            raise web.HTTPBadRequest(text=str(refusal)) from refusal
        client = html.escape(grants.clients[held.query.client_id].name)
        return web.Response(
            content_type="text/html",
            headers={
                "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": (
                    f"default-src 'none'; form-action 'self' {held.query.redirect_uri}; "
                    "frame-ancestors 'none'; base-uri 'none'"
                ),
            },
            text=(f"<!doctype html><title>Approve connector</title><p>Connect {client}?</p>"
                  '<form method="post"><button type="submit">Approve and connect</button></form>'),
        )

    async def confirm(self, request: web.Request) -> web.Response:
        """Consume the owner's POST and establish browser trust in the same update."""
        link = FieldCodec.decode(ApprovalLink, dict(request.match_info))
        code, browser = secrets.token_urlsafe(32), secrets.token_urlsafe(32)

        def consumed(grants: ConnectorGrants) -> DocumentChange[ConnectorGrants, AuthorizationQuery]:
            now = time.time()
            held = grants.require_approval(link.request_id, link.secret, now)
            return DocumentChange(replace(
                grants.unexpired(now).code_issued(held.query, code, now),
                trusted_browsers=grants.trusted_browsers | {digest(browser): TrustedBrowser(now)},
            ), held.query)

        try:
            approved = await asyncio.to_thread(self.store.update, consumed)
        except ApprovalRefused as refusal:
            raise web.HTTPBadRequest(text=str(refusal)) from refusal
        response = web.HTTPFound(
            approved.redirect(code),
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )
        response.set_cookie(
            TrustedBrowser.cookie_name, browser, max_age=TrustedBrowser.cookie_seconds,
            secure=True, httponly=True, samesite="Lax", path="/",
        )
        raise response

    async def token(self, request: web.Request) -> web.Response:
        try:
            grant = TokenGrant.received(await request.post())
        except MalformedRecord as error:
            return OAuthError("invalid_request", str(error)).response()
        try:
            exchanged = await asyncio.to_thread(self.store.update, grant.exchange)
        except OAuthError as refusal:
            return refusal.response()
        return web.json_response(
            FieldCodec.encode(exchanged), headers={"Cache-Control": "no-store"}
        )

    @staticmethod
    def refused(data: object, error: McpError) -> web.Response:
        logging.warning("MCP request refused (%s): %s", type(error).__name__, error.message)
        return web.json_response(FieldCodec.encode(McpErrorReply.refusing(data, error)))

    def caller_for(self, token: str) -> ConnectorCaller:
        """Resolve the bearer and its root authority before reading any MCP message."""
        caller = self.store.read().caller_for(token)
        caller.require_root(self.root)
        return caller

    async def bearer_caller(self, request: web.Request) -> ConnectorCaller:
        """The request's bearer grant, or the OAuth challenge every route answers with."""
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        try:
            return await asyncio.to_thread(self.caller_for, token)
        except GrantRefusedError:
            raise web.HTTPUnauthorized(
                headers={
                    "WWW-Authenticate": (f'Bearer resource_metadata="{self.resource_metadata_url}"')
                }
            )

    async def observe(self, request: web.Request) -> web.WebSocketResponse:
        """A UI grant's observation of its root, carried as declared records."""
        caller = await self.bearer_caller(request)
        try:
            caller.require_observation()
        except GrantRefusedError as error:
            raise web.HTTPForbidden(text=str(error))
        viewer = web.WebSocketResponse()
        await viewer.prepare(request)
        observation = OpenObservation(viewer=viewer, handler=request.task)
        self.open_viewers.append(observation)
        try:
            await ObservationRelay(resolve_comms_route(self.root), self.helper_start).carry(viewer)
        finally:
            self.open_viewers.remove(observation)
        return viewer

    async def mcp(self, request: web.Request) -> web.StreamResponse:
        caller = await self.bearer_caller(request)
        try:
            data = await request.json()
        except json.JSONDecodeError as error:
            return self.refused(None, ParseError(str(error)))
        try:
            message = McpMessage.received(data)
        except UnknownFamilyMember as error:
            return self.refused(data, MethodNotFoundError(str(error)))
        except MalformedRecord as error:
            return self.refused(data, InvalidRequestError(str(error)))
        started = time.monotonic()
        response = await self.session.claimed(request).answer(message, self, caller, request)
        logging.info("MCP %s answered in %.3fs", message.log_name, time.monotonic() - started)
        return response


class ObservationRelay:
    """One remote viewer's observation process. Its frames pass through unopened: the
    service end decodes each one as a declared record and refuses anything else."""

    def __init__(self, route: CommsRoute, start: HelperStart) -> None:
        relay, service = socket.socketpair()
        with service:
            self.process = start.context().Process(
                target=serve_encoded, args=(route, service), daemon=False,
                name="agent-comms-remote-observation")
            self.process.start()
        self.connection = RelayConnection(relay.detach())

    async def carry(self, viewer: web.WebSocketResponse) -> None:
        """Relay frames both ways until either side ends, then close the service's connection,
        which ends it, and retire the process."""
        try:
            await relay_frames(self.connection, viewer)
        finally:
            self.connection.close()
            await asyncio.to_thread(self.retire)

    exit_seconds: ClassVar[float] = 5

    def retire(self) -> None:
        """The service exits once its connection closes; one that has not is hung, and is
        killed so no observation process outlives its viewer."""
        self.process.join(self.exit_seconds)
        if self.process.is_alive():
            self.process.kill()
            self.process.join()


@dataclass(frozen=True, kw_only=True)
class ConnectorVerb(
    DeclaredVerb, Command[ConnectorStore, int | str],
    affix="Verb", discovery_package=HostService.__module__,
):
    """One connector command: its options are its fields, and its parser comes from them."""

    program: ClassVar[str] = "agent-comms-connector"

    @abstractmethod
    def apply(self, ctx: ConnectorStore) -> int | str: ...

    @classmethod
    def tool(cls) -> str:
        return cls.program


@dataclass(frozen=True, kw_only=True)
class ConnectorRootOptions(ConnectorVerb, declared_name=None):
    root: Annotated[
        Path,
        PathText,
        Argument(flags=("--root",), options={"help": "Explicit Comms root"}),
    ]


@dataclass(frozen=True, kw_only=True)
class ConnectorOptions(ConnectorRootOptions, declared_name=None):
    """The endpoint's CLI fields and their declared argument spelling."""

    port: Annotated[
        int,
        Argument(
            flags=("--port",), options={"help": "The loopback port it listens on"}
        ),
    ] = PORT
    issuer: Annotated[
        str,
        Argument(
            flags=("--issuer",), options={"help": "Public origin the OAuth metadata names"}
        ),
    ] = f"http://{LOOPBACK}:{PORT}"

    def command_arguments(self) -> tuple[str, ...]:
        return (self.declared_name, *(word for plan in FieldCodec.field_plans(type(self))
                for word in (Argument.declared(plan.annotation).flags_for(plan.name)[0],
                             str(plan.current(self)))))


@dataclass(frozen=True, kw_only=True)
class ServeVerb(ConnectorOptions):
    help = f"Serve MCP on {LOOPBACK}:{PORT}{MCP_PATH}"

    def apply(self, ctx: ConnectorStore) -> int | str:
        # Serving is where a bus's identity is minted: the read that answers it is a read,
        # and this runs once per Core rather than once per client that asks.
        BusIdentityStore.minted_for(self.root)
        connector = Connector(ctx, self.issuer.rstrip("/"), self.root)
        app = connector.application()
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
        asyncio.run(self.serve(app, connector))
        return 0

    async def serve(self, app: web.Application, connector: Connector) -> None:
        """Bind, then report ready, so a restart returns only once the port answers."""
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        stopped = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, stopped.set)
        # A stuck handler has to be named while it is still stuck: the access line prints
        # only once the handler ends. py-spy answers the other half, a handler blocked in a
        # synchronous call, and cannot see a suspended coroutine at all, because it reads
        # thread stacks and an awaiting coroutine sits on none.
        loop.add_signal_handler(signal.SIGUSR1, connector.log_pending, "asked what it is doing")
        try:
            await SystemdSite(runner, LOOPBACK, self.port).start()
            await stopped.wait()
            connector.log_pending()
        finally:
            await runner.cleanup()


@dataclass(frozen=True, kw_only=True)
class GrantUiVerb(ConnectorRootOptions, declared_name="grant-ui"):
    help = "Provision a private, expiring UI profile with the selected server authority"

    endpoint: Annotated[
        HttpsMcpEndpoint,
        Argument(flags=("--endpoint",), options={"help": "Public HTTPS MCP resource URL"}),
    ]
    profile: Annotated[
        Path, PathText,
        Argument(flags=("--profile",), options={"help": "New private profile; never overwritten"}),
    ]
    seconds: Annotated[
        int, Argument(flags=("--seconds",), options={
            "help": f"Lifetime, at most {UiGrantAccess.maximum_seconds} seconds"}),
    ] = UiGrantAccess.default_seconds
    access: Annotated[
        type[UiGrantAccess],
        Argument(flags=("--access",), options={"help": "Declared UI grant capability"}),
    ] = UiReadAccess

    def apply(self, ctx: ConnectorStore) -> int | str:
        """The grant is output, printed like round-trip's; a returned string is a refusal."""
        namespace = RegistryStore.at(self.root).privacy().namespace
        profile = ctx.publish_ui_profile(
            namespace, self.endpoint, self.profile, self.seconds, self.access)
        print(FieldCodec.compact(FieldCodec.encode(profile), allow_nan=False))
        return 0


@dataclass(frozen=True, kw_only=True)
class OfferUiVerb(ConnectorVerb, declared_name="offer-ui"):
    help = "Hand a UI profile to a caller that reached this machine's tailnet-only endpoint"

    # No --root: the machine being asked knows which one it serves, and a caller across the
    # tailnet cannot name a path in someone else's home without restating it.
    port: Annotated[
        int, Argument(flags=("--port",), options={
            "help": "The connector port whose served endpoints decide this"}),
    ] = PORT
    seconds: Annotated[
        int, Argument(flags=("--seconds",), options={
            "help": f"Lifetime, at most {UiGrantAccess.maximum_seconds} seconds"}),
    ] = UiGrantAccess.default_seconds
    access: Annotated[
        type[UiGrantAccess],
        Argument(flags=("--access",), options={"help": "Declared UI grant capability"}),
    ] = UiReadAccess

    def apply(self, ctx: ConnectorStore) -> int | str:
        """The profile itself is printed, secret included, which `grant-ui` never does.

        `grant-ui` writes the secret into a private file and prints only a summary, because
        its caller is a person at a terminal with scrollback. This verb's caller is a machine
        that just reached a tailnet-only endpoint of ours and is asking to hold a bus: the
        profile is the answer to that request, and the channel carrying it is theirs. A
        public endpoint refuses here (`ProfileOffer`), because reaching one proves nothing
        about who asked.
        """
        offer = ServedEndpoints.offered(self.port)
        if refusal := offer.refusal():
            return refusal
        match offer:
            case OfferedByTailnet(endpoint=served):
                namespace = RegistryStore.at(CommsRoute.current().root).privacy().namespace
                resource = URL(served.target.url).with_path(MCP_PATH)
                profile = ctx.offer_ui_profile(
                    namespace, HttpsMcpEndpoint(resource), self.seconds, self.access)
                print(FieldCodec.compact(FieldCodec.encode(profile), allow_nan=False))
                return 0
        return offer.refusal()


@dataclass(frozen=True, kw_only=True)
class HoldUiVerb(ConnectorVerb, declared_name="hold-ui"):
    help = "Hold a UI profile this machine was given, so a UI here can open its bus"

    profile: Annotated[
        Path, PathText,
        Argument(flags=("--profile",), options={"help": "The private profile file to hold"}),
    ]

    def apply(self, ctx: ConnectorStore) -> int | str:
        """Decode before holding, so a file that is not a profile is refused where it is
        named rather than when a picker tries to open its bus. The held name is the
        profile's own endpoint and grant, so nothing a caller types can leave the
        directory and two grants for one host do not collide."""
        held = UiConnectionProfile.read(self.profile)
        match held.endpoint.url.host:
            case str() as host:
                destination = HeldUiProfiles.directory() / f"{host}-{held.credential.grant_id}.json"
            case _:
                return f"{held.endpoint.url} names no host to hold it under"
        print(FieldCodec.compact(FieldCodec.encode(UiGrantSummary(
            held.credential.grant_id, held.credential.namespace,
            held.credential.expires_at, held.write_new(destination),
        )), allow_nan=False))
        return 0


@dataclass(frozen=True, kw_only=True)
class RevokeUiVerb(ConnectorVerb, declared_name="revoke-ui"):
    help = "Revoke one UI grant by public ID, leaving other grants untouched"

    grant_id: Annotated[
        UiGrantId, Argument(flags=("--grant",), options={"help": "Public UI grant ID, not its secret"}),
    ]

    def apply(self, ctx: ConnectorStore) -> int | str:
        ctx.revoke_ui(self.grant_id)
        return 0


@dataclass(frozen=True, kw_only=True)
class RevokeThreadVerb(ConnectorVerb, declared_name="revoke-thread"):
    help = "Revoke one thread's token, leaving every other grant untouched"

    thread: Annotated[str, Argument(flags=("--thread",), options={"help": "Bus thread whose token to revoke"})]

    def apply(self, ctx: ConnectorStore) -> int | str:
        ctx.thread_revoked(self.thread)
        return 0


@dataclass(frozen=True, kw_only=True)
class RevokeVerb(ConnectorVerb):
    help = "Revoke every issued token, code and trusted browser"

    def apply(self, ctx: ConnectorStore) -> int | str:
        ctx.update(lambda grants: DocumentChange.to(replace(
            grants, requests={}, codes={}, access={}, refresh={}, trusted_browsers={}
        )))
        return 0


@boundary_decoder
def main(argv: Sequence[str] | None = None) -> int | str:
    arguments = sys.argv[1:] if argv is None else argv
    started()
    return ConnectorVerb.parse(arguments).apply(ConnectorStore.owned())
