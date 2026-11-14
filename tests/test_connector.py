"""The claude.ai connector: OAuth in front of an MCP transport over the ToolRequest family."""


import asyncio
import base64
import errno
import hashlib
import json
import logging
import os
import re
import secrets
import site
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from aiohttp import ClientSession, CookieJar, web
from aiohttp.test_utils import TestClient, TestServer
from yarl import URL

from agent_comms.comms import Comms, wire
from agent_comms.execution.transcript_sources import EarlierThan, NativeTranscript
from agent_comms.coordination.coordinator import Coordination
from agent_comms.coordination.errors import TranscriptChanged
from agent_comms.coordination.service_passes import ServicePassStore
from agent_comms.execution.launch_root import LaunchRoot
from agent_comms.execution.session_file import RegistrySessionLocation
from agent_comms.execution.watch_passes import WatchPass
from agent_comms.host_services.connector import ConnectorService
from agent_comms.installer.backup import StoreBackup
from agent_comms.messaging.bus import Bus
from agent_comms.messaging.bus_identity import BusIdentityStore
from agent_comms.messaging.bus_read import UiRead
from agent_comms.messaging.channel_targets import CommandTarget, ThreadTarget
from agent_comms.messaging.send_result import MessagePublished
from agent_comms.nominal.field_codec import FieldCodec, MalformedRecord, RecordProblems, RootId
from agent_comms.projects.checkouts import Checkout
from agent_comms.projects.git_errors import FailedGitCommand
from agent_comms.registry.thread_identity import UserThreadRole
from agent_comms.registry.threads import NativeThread
from agent_comms.storage.locked_store import DocumentChange, RegistryFile
from agent_comms.storage.maintenance_barrier import (
    InstallingPhase,
    MaintenanceBarrier,
    MaintenanceHeldError,
    MaintenanceOperator,
    PausedPhase,
    ReadyPhase,
)
from agent_comms.storage.store_files import ExclusiveLock, StoreGuard, _store_lock
from agent_comms.team.standing_rules import ServicePassFailing
from agent_comms.transport.connector import (
    ApprovalAudience,
    ApprovalLink,
    AuthorizationCodeGrant,
    BoundedRoute,
    Connector,
    ConnectorSendTool,
    ConnectorVerb,
    GrantUiVerb,
    LongLivedRoute,
    RevokeVerb,
    RunningRequest,
    ServeVerb,
)
from agent_comms.transport.connector_caller import (
    CALLER,
    GrantRefusedError,
    ReadOnlyConnectorCaller,
    ScopedUiCaller,
    UiCaller,
    UiReadCaller,
)
from agent_comms.transport.connector_profile import (
    OBSERVE_PATH,
    HttpsMcpEndpoint,
    UiConnectionProfile,
    UiGrantId,
    UiGrantSummary,
)
from agent_comms.transport.connector_store import (
    CALLBACK,
    CODE_SECONDS,
    AuthorizationQuery,
    ConnectorCredentialsFile,
    ConnectorGrants,
    ConnectorStore,
    ExpiredProfile,
    HeldUiProfiles,
    IssuedCode,
    OpenableProfile,
    ReadServiceAccess,
    TrustedBrowser,
    UiAccess,
    UiGrantAccess,
    UiReadAccess,
    UnreadableProfile,
    digest,
)
from agent_comms.transport.mcp import (
    PROTOCOL_VERSION,
    SESSION_HEADER,
    CancelledMessage,
    InitializeResult,
    NoProgress,
    ProgressToken,
    StreamedLine,
    ToolCallMeta,
    ToolsCallMessage,
    ToolsCallReply,
    ToolsCallResult,
)
from agent_comms.transport.progress import WireExportObserver
from agent_comms.transport.request import ToolCall, ToolRequest
from agent_comms.transport.tool_caller import HumanCaller, NoThreadCaller, ThreadCaller
from agent_comms.transport.tool_request import ReadTool
from agent_comms.transport.tools.channels import ChannelListing, CommsChannelsTool
from agent_comms.transport.tools.reads import TranscriptPageRead
from agent_comms.transport.tools.send import CommsSendTool
from agent_comms.transport.tools.served import (
    CommsPinChannelTool,
    ServedCommsUserSendTool,
    invoke_tool,
)
from agent_comms.transport.tools.sidebar import (
    CommsUserSendTool,
    ServedRoot,
)
from agent_comms_hooks import ToolRefusedError
from bus_record_fixture import prepare_native_comms
from conftest import transcript_records
from connector_readiness import ConnectorReadiness

LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


def test_serve_requires_explicit_root(tmp_path):
    with pytest.raises(RecordProblems) as missing:
        ConnectorVerb.parse(["serve"])
    assert set(missing.value.problems) == {"root"}
    command = ConnectorVerb.parse(["serve", "--root", str(tmp_path)])
    assert isinstance(command, ServeVerb)
    assert command.root == tmp_path


def test_real_connector_refusals_and_replaced_source(tmp_path, source_programs):
    """The actual serve entrypoint, curl transport and production maintenance thread."""

    project = Path(__file__).resolve().parents[1]
    environment = {**os.environ, "PYTHONPATH": str(project / "src"),
                   "PATH": os.pathsep.join((str(source_programs.prefix / "bin"), os.environ["PATH"]))}
    # The proof creates the root it serves, so it runs under the serving runtime.
    proof = subprocess.run(
        [str(source_programs.prefix / "bin/python"), "-X", "faulthandler",
         str(project / "tests/verify_connector_live.py"),
         str(tmp_path / "live-connector")],
        env=environment, capture_output=True, text=True, timeout=60,
    )
    assert proof.returncode == 0, proof.stdout + proof.stderr


@pytest.mark.parametrize("registered", [True, False])
def test_connector_round_trip_reads_without_a_director_thread(comms, tmp_path, registered):

    original = comms.registry.declare(NativeThread("claude-ai", frozenset(), Checkout(comms.root)))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    readiness = ConnectorReadiness()
    environment = readiness.environment({**os.environ, "HOME": str(tmp_path / "home"), "PYTHONUNBUFFERED": "1",
        "PYTHONPATH": os.pathsep.join((str(Path(__file__).resolve().parents[1] / "src"),
                                       *site.getsitepackages()))})
    command = "from agent_comms.transport.connector import main; raise SystemExit(main())"
    before = comms.bus.bus.revision()
    server = subprocess.Popen(
        [sys.executable, "-c", command, "serve", "--root", comms.root, "--port", str(port)],
        env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        readiness.wait(server, port)
        if not registered:
            comms.registry.unregister("claude-ai")
            comms.registry.archive("claude-ai")
            comms.threads.delete(original.name)
        proof = ConnectorService(
            launch=LaunchRoot.from_path(comms.root),
            store=ConnectorStore(ConnectorStore.path_in(
                tmp_path / "home" / ".local/state/agent-comms/connector")),
            port=port,
        )
        proof.serves()
        assert server.poll() is None
        assert proof.store.read().access == {}
    finally:
        server.terminate()
        server.wait(timeout=30)
    assert comms.bus.bus.revision() == before


@pytest.fixture
def store(tmp_path: Path) -> ConnectorStore:
    return ConnectorStore.at(tmp_path)


def test_service_read_grant_is_temporary_and_cannot_publish(store: ConnectorStore, comms: Comms):
    with store.granted("connector health proof", ReadServiceAccess) as token:
        caller = store.read().access[digest(token)].caller()
        assert isinstance(caller, ReadOnlyConnectorCaller)
        assert CommsChannelsTool.declared_name in caller.exposed()
        with pytest.raises(ToolRefusedError, match="declared reads only"):
            caller.invoke(CommsSendTool(target="worker", body="must not publish"), comms)
    assert store.read().access == {}


@pytest.fixture
def connector(store: ConnectorStore, root: Path, comms: Comms):
    for created_at, name in enumerate(("worker", "claude-ai"), start=1):
        comms.registry.declare(NativeThread(name, frozenset(), Checkout(root), created_at=created_at))
    return Connector(store, "http://127.0.0.1:18766", root)


@pytest.fixture
def copied_connector(connector, tmp_path):
    async def copy():
        copied_root = tmp_path / ".agent-comms"
        await asyncio.to_thread(StoreBackup.copy_tree, connector.root, copied_root)
        return Connector(ConnectorStore.at(tmp_path / "copied-auth"), connector.issuer, copied_root)
    return copy


@pytest.fixture
async def client(connector):
    async with TestServer(connector.application()) as server:
        cookies = CookieJar(unsafe=True, treat_as_secure_origin=server.make_url("/"))
        async with TestClient(server, cookie_jar=cookies) as client:
            yield client


@dataclass(frozen=True)
class ClientLogin:
    query: AuthorizationQuery
    verifier: str

    @classmethod
    async def register(cls, client: TestClient):
        registered = await (await client.post(
            "/register", json={"redirect_uris": [CALLBACK], "client_name": "claude.ai"}
        )).json()
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        return cls(AuthorizationQuery(
            "code", registered["client_id"], CALLBACK, challenge.decode().rstrip("="), "S256", "s"
        ), verifier)

    async def redeem(self, client: TestClient, code: str) -> str:
        grant = AuthorizationCodeGrant(
            code=code, code_verifier=self.verifier, client_id=self.query.client_id,
            redirect_uri=self.query.redirect_uri,
        )
        issued = await client.post("/token", data=FieldCodec.encode(grant))
        assert issued.status == 200
        return (await issued.json())["access_token"]


FUNNEL = {ApprovalAudience.funnel_header: "?1"}
"""Tailscale's mark on a request that came in from the internet. A test that reads the
link out of the owner's inbox is testing that arrival: a browser the tailnet admitted
reads the link from the page and sends nothing (`ApprovalAudience`)."""


async def approved_token(client: TestClient, comms: Comms) -> str:
    """Register, confirm the real owner-message link, and redeem its PKCE-bound code.

    Arriving with tailscale's Funnel header is what makes the link travel over comms: a
    browser the tailnet already admitted reads it from the page instead, and never sends
    one (`ApprovalAudience`).
    """
    login = await ClientLogin.register(client)
    parameters = FieldCodec.encode(login.query)
    assert (await client.head("/authorize", params=parameters, allow_redirects=False)).status == 405
    waiting = await client.get(
        "/authorize", params=parameters, allow_redirects=False, headers=FUNNEL
    )
    assert waiting.status == 200
    owner = comms.messaging.user_identity(Checkout(comms.root))
    notice = comms.bus.inbox(owner.name).page.messages[-1]
    link = urlsplit(notice.body.splitlines()[-1])
    assert (await client.head(link.path, allow_redirects=False)).status == 405
    preview = await client.get(link.path, allow_redirects=False)
    assert preview.status == 200 and not preview.cookies
    redirected = await client.post(link.path, allow_redirects=False)
    assert redirected.status == 302
    cookie = redirected.cookies[TrustedBrowser.cookie_name]
    assert cookie["secure"] and cookie["httponly"] and cookie["samesite"] == "Lax"
    assert cookie["path"] == "/" and not cookie["domain"]
    assert redirected.headers["Cache-Control"] == "no-store"
    code = parse_qs(urlsplit(redirected.headers["Location"]).query)["code"][0]
    return await login.redeem(client, code)


async def test_a_request_without_an_approved_token_is_refused(client, store):
    for headers in ({}, {"Authorization": "Bearer guessed"}):
        refused = await client.post("/mcp", json=LIST, headers=headers)
        assert refused.status == 401
        assert "oauth-protected-resource" in refused.headers["WWW-Authenticate"]
    assert (await client.get("/.well-known/oauth-authorization-server")).status == 200


async def test_a_refused_call_answers_its_own_request_id(client, comms):
    """A client waits forever on a call whose refusal carries a null id."""
    token = await approved_token(client, comms)
    call = {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": "comms_channels", "arguments": {}, "undeclared": {}}}
    refused = await (await client.post(
        "/mcp", json=call, headers={"Authorization": f"Bearer {token}"})).json()
    assert refused["id"] == 7
    assert "undeclared" in refused["error"]["message"]


async def test_each_refusal_carries_its_json_rpc_code(client, comms):
    """An undeclared method is not an invalid request; a client tells them apart by code."""
    token = await approved_token(client, comms)
    headers = {"Authorization": f"Bearer {token}"}
    refusals = (
        ({"jsonrpc": "2.0", "id": 4, "method": "server/discover"}, -32601),
        ({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {}}, -32600),
    )
    for call, code in refusals:
        refused = await (await client.post("/mcp", json=call, headers=headers)).json()
        assert (refused["id"], refused["error"]["code"]) == (call["id"], code)
    garbled = await client.post("/mcp", data="{not json", headers=headers)
    assert (await garbled.json())["error"]["code"] == -32700


async def test_a_tool_reply_is_exactly_the_json_rpc_response_shape(client, comms):
    token = await approved_token(client, comms)
    reply = await (await client.post("/mcp", headers={"Authorization": f"Bearer {token}"}, json={
        "jsonrpc": "2.0", "id": 9, "method": "tools/call",
        "params": {"name": "comms_channels", "arguments": {}}})).json()
    assert set(reply) == {"jsonrpc", "id", "result"}


@pytest.mark.parametrize("arguments", ({"kind": "undeclared"}, {"unexpected": True}))
async def test_expected_tool_argument_refusal_keeps_its_mcp_reply(client, comms, arguments):
    token = await approved_token(client, comms)
    response = await client.post("/mcp", headers={"Authorization": f"Bearer {token}"}, json={
        "jsonrpc": "2.0", "id": 31, "method": "tools/call",
        "params": {"name": CommsChannelsTool.declared_name, "arguments": arguments},
    })
    reply = await response.json()
    assert response.status == 200
    assert reply["id"] == 31
    assert reply["result"]["isError"] is True
    assert json.loads(reply["result"]["content"][0]["text"])["error"]


async def tool_call(client: TestClient, token: str, call_id: int, name: str, arguments: dict):
    response = await client.post("/mcp", headers={"Authorization": f"Bearer {token}"}, json={
        "jsonrpc": "2.0", "id": call_id, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })
    return response.status, await response.json()


def bind_worker_session(comms: Comms, path: Path) -> None:
    worker = comms.registry.require("worker")
    comms.registry.declare(replace(
        worker, session_file=RegistrySessionLocation.from_path(str(path), comms.root)))


def saved_session(path: Path, *entries: bytes) -> Path:
    header = json.dumps(transcript_records.session(id="session").to_wire()).encode()
    path.write_bytes(b"".join((header + b"\n", *entries)))
    return path


async def test_an_unreadable_transcript_is_a_failed_call_and_the_connector_keeps_serving(
    client, comms, tmp_path,
):
    """A corrupt complete native entry escaped tools/call as an HTTP 500 with no id."""
    path = saved_session(tmp_path / "corrupt.jsonl", b'{"type":"message"\n')
    bind_worker_session(comms, path)
    with pytest.raises(MalformedRecord) as unreadable:
        tuple(NativeTranscript(path).tail())
    token = await approved_token(client, comms)
    status, reply = await tool_call(
        client, token, 41, TranscriptPageRead.declared_name, {"thread": "worker"})
    assert (status, reply["id"]) == (200, 41)
    # The domain's own MalformedRecord, with its JSONDecodeError cause, is the failed result.
    assert reply["result"] == FieldCodec.encode(ToolsCallResult.failed(unreadable.value))
    assert reply["result"]["isError"] is True and "structuredContent" not in reply["result"]
    assert "JSONDecodeError" in reply["result"]["content"][0]["text"]
    status, served = await tool_call(client, token, 42, CommsChannelsTool.declared_name, {})
    assert (status, served["id"], served["result"]["isError"]) == (200, 42, False)


async def test_a_stale_transcript_cursor_is_a_failed_call_with_its_coordination_cause(
    client, comms, tmp_path,
):
    message = transcript_records.message({"role": "assistant", "content": [{"type": "text", "text": "held"}]})
    bind_worker_session(comms, saved_session(
        tmp_path / "session.jsonl", json.dumps(message).encode() + b"\n"))
    page = comms.transcripts.capture_page_read("worker").read()
    bind_worker_session(comms, saved_session(tmp_path / "other.jsonl"))
    stale = TranscriptPageRead(thread="worker", position=EarlierThan(page.before))
    with pytest.raises(TranscriptChanged) as changed:
        UiRead.serving(stale).read(comms)
    token = await approved_token(client, comms)
    status, reply = await tool_call(
        client, token, 43, stale.declared_name, FieldCodec.encode_fields(stale))
    assert (status, reply["id"]) == (200, 43)
    assert reply["result"] == FieldCodec.encode(ToolsCallResult.failed(changed.value))
    assert "structuredContent" not in reply["result"]


async def test_request_log_names_the_route_and_never_its_values(client, store, root, comms, caplog):
    """Serve actual GET/POST approval secrets, with aiohttp's raw access log off."""
    login = await ClientLogin.register(client)
    assert (await client.get(
        "/authorize", params=FieldCodec.encode(login.query), allow_redirects=False, headers=FUNNEL
    )).status == 200
    owner = comms.messaging.user_identity(Checkout(comms.root))
    link = urlsplit(comms.bus.inbox(owner.name).page.messages[-1].body.splitlines()[-1])
    secret = link.path.rsplit("/", 1)[-1]
    runner = web.AppRunner(Connector(store, "http://127.0.0.1:18766", root).application(),
                           access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    origin = f"http://127.0.0.1:{runner.addresses[0][1]}"
    try:
        with caplog.at_level(logging.INFO):
            async with ClientSession() as session:
                await session.get(f"{origin}/authorize", params={"state": "one-use-value"})
                preview = await session.get(
                    f"{origin}{link.path}", params={"code": "one-use-value"},
                    headers={"Authorization": "Bearer private-token",
                             "Cookie": f"{TrustedBrowser.cookie_name}=private-cookie"},
                )
                assert preview.status == 200
                confirmed = await session.post(f"{origin}{link.path}", allow_redirects=False)
                assert confirmed.status == 302
                code = parse_qs(urlsplit(confirmed.headers["Location"]).query)["code"][0]
                cookie = confirmed.cookies[TrustedBrowser.cookie_name].value
                unmatched = await session.get(
                    f"{origin}/missing/{secret}", params={"code": "one-use-value"}
                )
                assert unmatched.status == 404
    finally:
        await runner.cleanup()
    lines = [record.getMessage() for record in caplog.records]
    assert [line.rsplit(" ", 1)[0] for line in lines] == [
        "GET /authorize 400", f"GET {ApprovalLink.route} 200",
        f"POST {ApprovalLink.route} 302", "GET unmatched 404",
    ], lines
    assert all(re.fullmatch(r"\d+\.\d{3}s", line.rsplit(" ", 1)[1]) for line in lines)
    for value in (secret, code, cookie, "one-use-value", "private-token", "private-cookie"):
        assert all(value not in line for line in lines), lines


@dataclass
class PublishingWatch(WatchPass[None], declared_name=None):
    calls: int = 0
    completed: threading.Event = field(default_factory=threading.Event)

    def observe(self, comms: Comms, prepared: None) -> None:
        pass

    def pass_body(self, comms: Comms, observed: None) -> None:
        self.calls += 1
        comms.messaging.send("claude-ai", "worker", "One watch pass")
        self.completed.set()


async def test_watch_pass_waits_for_install_then_runs_once(comms, copied_connector, caplog):
    original = comms.messaging.send("worker", "claude-ai", "Original stays unchanged").message
    connector = await copied_connector()
    copied = wire(connector.root)
    barrier = MaintenanceBarrier(copied.registry.store.path)
    held = barrier.begin(MaintenanceOperator.current())
    held = barrier.advance(held, PausedPhase)
    held = barrier.advance(held, InstallingPhase)
    opened = threading.Event()

    @contextmanager
    def opened_wire():
        with connector.service_wire() as service:
            opened.set()
            yield service

    watch = PublishingWatch()
    passes = ServicePassStore.at(connector.root)
    pending = asyncio.create_task(Coordination.watch_worker(watch, opened_wire, passes))
    try:
        try:
            assert await asyncio.to_thread(opened.wait, 3)
            assert not await asyncio.to_thread(watch.completed.wait, .05)
            assert watch.calls == 0
            assert copied.bus.bus.revision() == original.seq
            assert not [record for record in caplog.records if record.levelno >= logging.ERROR]
        finally:
            barrier.advance(held, ReadyPhase)
        assert await asyncio.to_thread(watch.completed.wait, 3)
    finally:
        pending.cancel()
        with suppress(asyncio.CancelledError):
            await pending
    assert watch.calls == 1
    assert [message.body for message in copied.bus.inbox("worker").page.messages] == ["One watch pass"]
    assert passes.read().failing == {}
    assert comms.bus.bus.revision() == original.seq
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]


async def test_watch_held_by_install_stops_when_cancelled(copied_connector):
    """The install holds maintenance until the connector has stopped, so a watch waiting out
    the hold must still stop when the connector does; systemd killed it at 90 s before."""
    connector = await copied_connector()
    barrier = MaintenanceBarrier(wire(connector.root).registry.store.path)
    held = barrier.advance(barrier.advance(
        barrier.begin(MaintenanceOperator.current()), PausedPhase), InstallingPhase)
    watch = PublishingWatch()
    opened = threading.Event()

    @contextmanager
    def opened_wire():
        with connector.service_wire() as service:
            opened.set()
            yield service

    pending = asyncio.create_task(Coordination.watch_worker(
        watch, opened_wire, ServicePassStore.at(connector.root)))
    try:
        assert await asyncio.to_thread(opened.wait, 3)
        await asyncio.sleep(.1)
        pending.cancel()
        with suppress(asyncio.CancelledError):
            await asyncio.wait_for(pending, 3)
        assert pending.cancelled()
        assert watch.calls == 0
    finally:
        barrier.advance(held, ReadyPhase)


def test_fresh_connector_publication_refuses_before_entering_held_stores(connector, comms):
    """A fresh external mutation refuses before caller lookup or publication."""
    before = comms.bus.bus.revision()
    barrier = MaintenanceBarrier(comms.registry.store.path)
    held = barrier.advance(barrier.advance(
        barrier.begin(MaintenanceOperator.current()), PausedPhase), InstallingPhase)
    try:
        with pytest.raises(MaintenanceHeldError):
            connector.invoke(
                ConnectorSendTool, {"to": "worker", "body": "Held send"}, CALLER, WireExportObserver())
    finally:
        barrier.advance(held, ReadyPhase)
    assert comms.bus.bus.revision() == before


def test_publishing_tool_held_mid_call_refuses_at_once(comms):
    """The native call bypasses fresh connector ingress; its actual store still refuses."""
    for name in (CALLER.name, "worker"):
        comms.registry.declare(NativeThread(name, frozenset(), Checkout(comms.root)))
    before = comms.bus.bus.revision()
    barrier = MaintenanceBarrier(comms.registry.store.path)
    held = barrier.advance(barrier.advance(
        barrier.begin(MaintenanceOperator.current()), PausedPhase), InstallingPhase)
    try:
        with pytest.raises(MaintenanceHeldError):
            invoke_tool(comms, "comms_send", {"to": "worker", "body": "Held send"}, caller=CALLER)
    finally:
        barrier.advance(held, ReadyPhase)
    assert comms.bus.bus.revision() == before


class FailingWatch(PublishingWatch, declared_name=None):

    def pass_body(self, comms: Comms, observed: None) -> None:
        raise RuntimeError("A real watch failure")


async def test_watch_pass_logs_real_failures_with_tracebacks(connector, caplog):
    logged = asyncio.Event()

    class ObservedError(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.levelno >= logging.ERROR:
                logged.set()

    handler = ObservedError()
    logger = logging.getLogger()
    logger.addHandler(handler)
    passes = ServicePassStore.at(connector.root)
    pending = asyncio.create_task(
        Coordination.watch_worker(FailingWatch(), connector.service_wire, passes))
    try:
        await asyncio.wait_for(logged.wait(), 3)
        while not passes.read().failing:
            await asyncio.sleep(.01)
    finally:
        pending.cancel()
        with suppress(asyncio.CancelledError):
            await pending
        logger.removeHandler(handler)
    error, = (record for record in caplog.records if record.levelno >= logging.ERROR)
    assert error.exc_info[0] is RuntimeError
    assert str(error.exc_info[1]) == "A real watch failure"
    assert error.exc_info[2] is not None
    failing, = ServicePassFailing.violations(wire(connector.root))
    assert failing.watch == "FailingWatch"
    stored = passes.read().failing["FailingWatch"]
    assert (failing.since, failing.error) == (stored.since, stored.error)
    passes.passed("FailingWatch")
    assert passes.read().failing == {}
    assert ServicePassFailing.violations(wire(connector.root)) == ()


async def test_registration_accepts_only_the_claude_callback(client):
    refused = await client.post(
        "/register", json={"redirect_uris": ["https://elsewhere.example/cb"]}
    )
    assert refused.status == 400


async def test_connector_lists_every_tool(client, store, comms):
    token = await approved_token(client, comms)
    headers = {"Authorization": f"Bearer {token}"}
    # A real client cannot reach tools/list without a valid initialization reply.
    # An optional discovery probe may fail, but its JSON-RPC error needs a code.
    discovery = await (await client.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "id": "discover", "method": "server/discover",
    })).json()
    assert discovery["id"] == "discover"
    assert discovery["error"]["code"] == -32601
    initialized = await (await client.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "id": "initialize", "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "connector-client", "version": "1"},
        },
    })).json()
    assert initialized == {
        "jsonrpc": "2.0", "id": "initialize", "result": {
            "protocolVersion": "2025-06-18", "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "agent-comms", "version": "1"},
            # The bus it serves, then how to share it: test_a_connector_names_its_bus_at_the
            # _handshake owns what that first part says.
            "instructions": InitializeResult.announcing(
                BusIdentityStore.serving(comms.root.resolve()).announced(str(comms.root.resolve())),
            ).instructions,
        },
    }
    ready = await client.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "method": "notifications/initialized",
    })
    assert ready.status == 202 and await ready.read() == b""
    ping = await (await client.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "id": "ping", "method": "ping",
    })).json()
    assert ping == {"jsonrpc": "2.0", "id": "ping", "result": {}}
    listed = (await (await client.post("/mcp", json=LIST, headers=headers)).json())["result"]["tools"]
    family = CALLER.exposed()
    assert [tool["name"] for tool in listed] == list(family)
    assert [tool["annotations"]["readOnlyHint"] for tool in listed] == [
        issubclass(tool, ReadTool) for tool in family.values()]
    # Every write tool says, from the one write marker, that claude-ai may act on its authorization.
    writes = [tool["description"] for tool in listed if not tool["annotations"]["readOnlyHint"]]
    assert writes and all(description.endswith(ToolRequest.bus_effect) for description in writes)
    assert all("claude-ai to call this without asking" in description for description in writes)


async def test_a_session_from_an_earlier_process_initializes_again(client, comms):
    """A client held open across an install keeps its old tool list until it re-lists; the
    id from the earlier process is answered 404, which makes the client initialize again."""
    headers = {"Authorization": f"Bearer {await approved_token(client, comms)}"}
    initialize = {"jsonrpc": "2.0", "id": "initialize", "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "connector-client", "version": "1"},
    }}
    past = {**headers, SESSION_HEADER: "an id an earlier process issued"}
    assert (await client.post("/mcp", headers=past, json=LIST)).status == 404
    assert (await client.post("/mcp", headers=past, json={
        "jsonrpc": "2.0", "method": "notifications/initialized",
    })).status == 404
    started = await client.post("/mcp", headers=past, json=initialize)
    session = started.headers[SESSION_HEADER]
    assert started.status == 200 and session != past[SESSION_HEADER]
    current = {**headers, SESSION_HEADER: session}
    listed = (await (await client.post("/mcp", headers=current, json=LIST)).json())["result"]["tools"]
    assert [tool["name"] for tool in listed] == list(CALLER.exposed())
    # A client that keeps no session is answered as before.
    assert (await client.post("/mcp", headers=headers, json=LIST)).status == 200


async def test_connector_reads_two_newest_first_conversation_pages(client, store, comms):
    comms.registry.declare(NativeThread("teammate", frozenset(), Checkout(comms.root)))
    originals = [comms.messaging.send("worker", "claude-ai", f"history {number}").message
                 for number in range(5)]
    comms.messaging.send("teammate", "claude-ai", "not this conversation")
    headers = {"Authorization": f"Bearer {await approved_token(client, comms)}"}

    async def page(arguments):
        response = await client.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "comms_history", "arguments": arguments},
        })
        assert response.status == 200
        result = (await response.json())["result"]
        assert result["isError"] is False
        return result["structuredContent"]

    first = await page({"peer": "worker", "limit": 2})
    second = await page({"peer": "worker", "limit": 2, "before": first["before"]})
    assert [row["id"] for row in first["dm"]] == [row.message_id for row in originals[4:2:-1]]
    assert [row["id"] for row in second["dm"]] == [row.message_id for row in originals[2:0:-1]]
    assert first["has_older"] and second["has_older"]


async def test_connector_channel_history_on_a_copy_leaves_unread_unchanged(
    client, comms, tmp_path, copied_connector,
):

    comms.channels.create_tag("comms")
    for name in ("worker", "claude-ai"):
        comms.channels.update_tags(name, add=frozenset({"comms"}))
    originals = [comms.messaging.send(
        "worker", "#comms", f"@claude-ai channel post {number}").message
        for number in range(55)]
    connector = await copied_connector()
    copied = wire(connector.root)
    target = CommandTarget.named("#comms")
    before = copied.bus.pending_count("claude-ai", target)
    ledger = copied.bus.reads.read()
    async with TestClient(TestServer(connector.application())) as reader:
        headers = {"Authorization": f"Bearer {await approved_token(reader, copied)}"}
        response = await reader.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "comms_history", "arguments": {"peer": "#comms", "limit": 50}},
        })
        assert response.status == 200
        result = (await response.json())["result"]
    after = copied.bus.pending_count("claude-ai", target)
    evidence = {"result": result, "unread_before": before, "unread_after": after}
    (tmp_path / "channel-history.json").write_text(json.dumps(evidence, indent=2))
    assert result["isError"] is False, result
    page = result["structuredContent"]
    assert page["channel"] == "#comms"
    assert [row["id"] for row in page["messages"]] == [row.message_id for row in originals[:4:-1]]
    assert len(page["messages"]) == 50 and page["has_older"]
    # Joining the channel also posts membership messages; the read must leave
    # those unread too, not only the 55 posts made above.
    assert before >= len(originals)
    assert after == before
    assert copied.bus.reads.read() == ledger
    assert comms.bus.pending_count("claude-ai", target) == before


async def test_connector_thread_tools_act_as_claude_ai(client, store, comms):
    headers = {"Authorization": f"Bearer {await approved_token(client, comms)}"}
    calls = ({"name": "comms_set_goal", "arguments": {"text": "Ship the connector"}},
             {"name": "comms_send", "arguments": {
                 "to": "worker", "body": "Owner said go"}})
    for call in calls:
        reply = await (await client.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": call})).json()
        assert reply["result"]["isError"] is False, reply
    assert comms.registry.require("claude-ai").goal.text == "Ship the connector"
    assert [(message.sender, message.body) for message in comms.bus.inbox("worker").page.messages] == [
        ("claude-ai", "Owner said go")]


async def test_a_call_returns_what_the_tool_returns_in_process(client, store, root, comms):
    token = await approved_token(client, comms)
    called = await (
        await client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "comms_threads", "arguments": {}},
            },
        )
    ).json()
    in_process = invoke_tool(wire(root), "comms_threads", {}, caller=CALLER)
    assert called["result"]["structuredContent"] == in_process


@pytest.mark.parametrize("phases", [(), (PausedPhase,), (PausedPhase, InstallingPhase)])
async def test_maintenance_refuses_before_locked_stores_on_a_copied_bus(
    comms, tmp_path, phases, monkeypatch, copied_connector,
):
    original = comms.messaging.send("worker", "claude-ai", "Ready for your message").message
    connector = await copied_connector()
    copied_root = connector.root
    monkeypatch.setenv("HOME", str(tmp_path))
    async with TestClient(TestServer(connector.application())) as reader:
        headers = {"Authorization": f"Bearer {await approved_token(reader, wire(copied_root))}"}
        copied_bus = Bus(Bus.path_in(copied_root))
        before = copied_bus.revision()
        barrier = MaintenanceBarrier(RegistryFile.path_in(copied_root))
        held = barrier.begin(MaintenanceOperator.current())
        for phase in phases:
            held = barrier.advance(held, phase)
        with _store_lock(copied_bus.path, ExclusiveLock(), StoreGuard.contention), _store_lock(barrier.registry_path, ExclusiveLock(), StoreGuard.contention):
            started = time.monotonic()
            response = await reader.post("/mcp", headers=headers, timeout=2, json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "comms_send", "arguments": {
                    "to": "worker", "body": "Please read this message"}},
            })
            reply = await response.json()
            elapsed = time.monotonic() - started
            assert elapsed < 1, (elapsed, reply)
            assert response.status == 200
            assert reply["id"] == 1
            result = reply["result"]
            assert result["isError"] is True
            refusal = json.loads(result["content"][0]["text"])
            assert refusal == {
                "type": "held", "error": "Maintenance holds ingress: the bus is under maintenance"}
            assert barrier.current_unlocked() == held
            assert copied_bus.revision() == before
        # Reads borrow their actual stores, not the mutation admission capability.
        response = await reader.post("/mcp", headers=headers, timeout=2, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": CommsChannelsTool.declared_name, "arguments": {}},
        })
        reply = await response.json()
        assert response.status == 200
        assert reply["id"] == 2
        assert reply["result"]["isError"] is False, reply
        assert "channels" in reply["result"]["structuredContent"]
        assert barrier.read() == held
        assert copied_bus.revision() == before
    assert comms.bus.bus.revision() == original.seq
    assert comms.owners.maintenance.read().turn_pending is False


async def test_approval_previews_do_not_grant_browser_trust(client, store, comms):
    login = await ClientLogin.register(client)
    assert (await client.get(
        "/authorize", params=FieldCodec.encode(login.query), allow_redirects=False,
        headers=FUNNEL,
    )).status == 200
    owner = comms.messaging.user_identity(Checkout(comms.root))
    link = urlsplit(comms.bus.inbox(owner.name).page.messages[-1].body.splitlines()[-1])
    before = store.path.read_bytes()
    for _ in range(2):
        preview = await client.get(link.path, allow_redirects=False)
        assert preview.status == 200 and not preview.cookies
        page = await preview.text()
        assert page.count("<button ") == 1 and '<form method="post">' in page
        policy = preview.headers["Content-Security-Policy"]
        assert "frame-ancestors 'none'" in policy and login.query.redirect_uri in policy
        assert store.path.read_bytes() == before
        assert not store.read().trusted_browsers and not store.read().codes
    approved = await client.post(link.path, allow_redirects=False)
    assert approved.status == 302 and TrustedBrowser.cookie_name in approved.cookies
    assert not store.read().requests and store.read().trusted_browsers
    assert (await client.post(link.path, allow_redirects=False)).status == 400
    code = parse_qs(urlsplit(approved.headers["Location"]).query)["code"][0]
    token = await login.redeem(client, code)
    assert store.read().caller_for(token) == CALLER


async def test_pending_authorizations_are_bounded(client, store, comms):
    login = await ClientLogin.register(client)
    for number in range(ConnectorGrants.waiting_limit):
        query = replace(login.query, state=str(number))
        assert (await client.get(
            "/authorize", params=FieldCodec.encode(query), allow_redirects=False, headers=FUNNEL
        )).status == 200
    owner = comms.messaging.user_identity(Checkout(comms.root))
    before = len(comms.bus.inbox(owner.name).page.messages)
    assert before == ConnectorGrants.waiting_limit
    blocked = replace(login.query, state="overflow")
    for _ in range(2):
        refused = await client.get(
            "/authorize", params=FieldCodec.encode(blocked), allow_redirects=False, headers=FUNNEL
        )
        assert refused.status == 429 and int(refused.headers["Retry-After"]) == CODE_SECONDS
    assert len(store.read().requests) == ConnectorGrants.waiting_limit
    assert blocked.request_id not in store.read().requests
    assert len(comms.bus.inbox(owner.name).page.messages) == before
    repeated = replace(login.query, state="0")
    assert (await client.get(
        "/authorize", params=FieldCodec.encode(repeated), allow_redirects=False, headers=FUNNEL
    )).status == 200
    # A repeat re-issues the secret it minted, so the owner is told the new link; the one
    # already waiting keeps its place and the queue does not grow.
    assert len(comms.bus.inbox(owner.name).page.messages) == before + 1
    assert len(store.read().requests) == ConnectorGrants.waiting_limit
    held = store.read().requests[repeated.request_id]
    store.update(lambda grants: DocumentChange.to(replace(grants, requests=grants.requests | {
        repeated.request_id: replace(held, expires_at=time.time() - 1)
    })))
    assert (await client.get(
        "/authorize", params=FieldCodec.encode(blocked), allow_redirects=False, headers=FUNNEL
    )).status == 200
    assert len(store.read().requests) == ConnectorGrants.waiting_limit
    assert len(comms.bus.inbox(owner.name).page.messages) == before + 2


async def test_owner_browser_approval_lifecycle(client, store, comms):
    """One real HTTP/store/bus journey: owner consent, remembered browser, expiry and revoke."""
    token = await approved_token(client, comms)
    owner = comms.messaging.user_identity(Checkout(comms.root))
    notices = comms.bus.inbox(owner.name).page.messages
    assert len(notices) == 1 and notices[0].target == owner.name
    first_link = urlsplit(notices[0].body.splitlines()[-1])
    assert first_link.netloc == "127.0.0.1:18766"
    assert '"claude.ai"' in notices[0].body
    assert (await client.get(first_link.path, allow_redirects=False)).status == 400
    browser = next(cookie.value for cookie in client.session.cookie_jar
                   if cookie.key == TrustedBrowser.cookie_name)
    grants = store.read()
    assert digest(browser) in grants.trusted_browsers and not grants.requests
    assert browser not in store.path.read_text()
    assert first_link.path.rsplit("/", 1)[-1] not in store.path.read_text()
    assert grants.caller_for(token) == CALLER

    registered = await (await client.post(
        "/register", json={"redirect_uris": [CALLBACK], "client_name": "claude.ai"}
    )).json()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(secrets.token_bytes(48)).digest())
    query = AuthorizationQuery(
        "code", registered["client_id"], CALLBACK, challenge.decode().rstrip("="), "S256", "account-two"
    )
    assert query.client_id != next(iter(grants.clients))
    redirected = await client.get(
        "/authorize", params=FieldCodec.encode(query), allow_redirects=False
    )
    assert redirected.status == 302
    redirected_query = parse_qs(urlsplit(redirected.headers["Location"]).query)
    assert redirected_query["state"] == [query.state] and redirected_query["code"]
    assert len(comms.bus.inbox(owner.name).page.messages) == 1

    client.session.cookie_jar.clear()
    query = replace(query, state="untrusted-browser")
    for _ in range(2):
        waiting = await client.get(
            "/authorize", params=FieldCodec.encode(query), allow_redirects=False, headers=FUNNEL
        )
        assert waiting.status == 200
        assert "sent to the owner on comms" in await waiting.text()
    notices = comms.bus.inbox(owner.name).page.messages
    # Each call issues the secret it minted, so each one tells the owner its own live link;
    # a retry that kept the first record left the browser holding a secret nobody stored.
    assert len(notices) == 3
    link = urlsplit(notices[-1].body.splitlines()[-1])
    assert (await client.get(link.path + "wrong", allow_redirects=False)).status == 400
    expired = replace(store.read().requests[query.request_id], expires_at=time.time() - 1)
    store.update(lambda grants: DocumentChange.to(replace(
        grants, requests={query.request_id: expired},
        codes=grants.codes | {"expired-code": IssuedCode(
            query.client_id, query.code_challenge, expires_at=expired.expires_at
        )},
    )))
    assert (await client.get(link.path, allow_redirects=False)).status == 400
    store.update(lambda grants: grants.issued(query.client_id))
    assert not store.read().requests and "expired-code" not in store.read().codes

    waiting = await client.get("/authorize", params=FieldCodec.encode(query), headers=FUNNEL)
    assert waiting.status == 200
    held = store.read().requests[query.request_id]
    store.update(lambda grants: DocumentChange.to(replace(grants, requests={
        query.request_id: replace(held, expires_at=time.time() - 1)
    })))
    fresh = replace(query, state="fresh-wait")
    assert (await client.get(
        "/authorize", params=FieldCodec.encode(fresh), headers=FUNNEL
    )).status == 200
    assert query.request_id not in store.read().requests
    assert store.read().requests[fresh.request_id].expires_at <= time.time() + CODE_SECONDS

    assert RevokeVerb().apply(store) == 0
    assert not store.read().trusted_browsers
    client.session.cookie_jar.update_cookies({TrustedBrowser.cookie_name: browser})
    assert (await client.get(
        "/authorize", params=FieldCodec.encode(fresh), allow_redirects=False
    )).status == 200
    assert not store.read().codes and not store.read().trusts(browser)
    assert len(comms.bus.inbox(owner.name).page.messages) == 5


def test_revoking_one_thread_leaves_every_other_grant(store):
    """`revoke` wipes the claude.ai login too, so retiring one thread's token needs its own verb."""
    store.thread_granted("kept")
    store.thread_granted("retired")
    assert ConnectorVerb.parse(["revoke-thread", "--thread", "retired"]).apply(store) == 0
    assert store.read().thread_held("kept") and not store.read().thread_held("retired")


async def test_a_revoked_token_is_refused(client, store, comms):
    token = await approved_token(client, comms)
    assert RevokeVerb().apply(store) == 0
    refused = await client.post("/mcp", json=LIST, headers={"Authorization": f"Bearer {token}"})
    assert refused.status == 401


def test_holding_a_profile_puts_it_where_a_picker_finds_it(
    store: ConnectorStore, comms: Comms, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A profile handed to this machine becomes one of its openable buses.

    The held name comes from the profile's own endpoint and grant, so nothing a caller
    types can leave the directory, and a file that is not a profile is refused here
    rather than when a picker tries to open its bus.
    """
    prepare_native_comms(comms)
    given = tmp_path / "given" / "box-b.json"
    assert GrantUiVerb.parse([
        "grant-ui", "--root", str(comms.root),
        "--endpoint", "https://box-b.example/mcp", "--profile", str(given), "--seconds", "600",
    ]).apply(store) == 0
    capsys.readouterr()

    monkeypatch.setenv("HOME", str(tmp_path / "connecting"))
    assert ConnectorVerb.parse(["hold-ui", "--profile", str(given)]).apply(store) == 0
    receipt = FieldCodec.decode(UiGrantSummary, FieldCodec.parse(capsys.readouterr().out))
    assert receipt.profile.parent == HeldUiProfiles.directory()
    assert receipt.profile.name.startswith("box-b.example-")
    held = HeldUiProfiles.held(0.0)
    assert [type(entry) for entry in held] == [OpenableProfile]
    assert held[0].profile == UiConnectionProfile.read(given)

    with pytest.raises(FileExistsError):
        ConnectorVerb.parse(["hold-ui", "--profile", str(given)]).apply(store)
    not_a_profile = tmp_path / "given" / "notes.json"
    not_a_profile.write_text("{}")
    not_a_profile.chmod(0o600)
    with pytest.raises(MalformedRecord):
        ConnectorVerb.parse(["hold-ui", "--profile", str(not_a_profile)]).apply(store)
    assert [type(entry) for entry in HeldUiProfiles.held(0.0)] == [OpenableProfile]


def test_the_held_profiles_name_every_bus_this_machine_can_open(
    store: ConnectorStore, comms: Comms, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A UI asks this machine what it can open: the serving bus's own grants cannot say.

    Every file in the held directory is named, including one that will not decode and one
    whose grant has expired, because a profile the owner cannot use is the row they need
    to see rather than a gap in a menu.
    """
    prepare_native_comms(comms)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert HeldUiProfiles.held(0.0) == (), "no directory holds no profiles"
    directory = HeldUiProfiles.directory()
    assert directory.parent == ConnectorCredentialsFile.path().parent
    assert GrantUiVerb.parse([
        "grant-ui", "--root", str(comms.root),
        "--endpoint", "https://box-b.example/mcp", "--profile", str(directory / "box-b.json"),
        "--seconds", "60",
    ]).apply(store) == 0
    unreadable = directory / "notes.txt"
    unreadable.write_text("not a profile")
    unreadable.chmod(0o600)
    issued = UiConnectionProfile.read(directory / "box-b.json").credential.expires_at

    held = HeldUiProfiles.held(issued - 1)
    assert [type(entry) for entry in held] == [OpenableProfile, UnreadableProfile]
    assert held[0].label() == f"box-b.example · {comms.registry.store.privacy().namespace}"
    assert held[1].label().startswith("notes.txt · ")

    expired = HeldUiProfiles.held(issued)
    assert [type(entry) for entry in expired] == [ExpiredProfile, UnreadableProfile]
    assert expired[0].label() == "box-b.example · grant expired"


@pytest.mark.parametrize("grant_type", UiGrantAccess.members_with(UiGrantAccess))
def test_ui_profile_publication_is_private_exclusive_and_independently_revocable(
    store: ConnectorStore, comms: Comms, tmp_path: Path,
    grant_type: type[UiGrantAccess], capsys: pytest.CaptureFixture[str],
) -> None:
    prepare_native_comms(comms)
    destination = tmp_path / "private-ui" / "connection.json"
    command = GrantUiVerb.parse([
        "grant-ui", "--root", str(comms.root),
        "--endpoint", "https://core.example/mcp", "--profile", str(destination),
        "--seconds", "60", "--access", grant_type.declared_name,
    ])
    assert command.apply(store) == 0
    reply = capsys.readouterr().out
    receipt = FieldCodec.decode(UiGrantSummary, FieldCodec.parse(reply))
    profile = UiConnectionProfile.read(destination)
    assert receipt.grant_id == profile.credential.grant_id
    assert receipt.namespace == comms.registry.store.privacy().namespace
    assert profile.credential.token not in reply
    assert destination.stat().st_mode & 0o777 == 0o600
    assert destination.parent.stat().st_mode & 0o777 == 0o700
    assert store.read().caller_for(profile.credential.token) == grant_type.caller_type(receipt.namespace)
    before = destination.read_bytes(), store.read()
    with pytest.raises(FileExistsError):
        command.apply(store)
    assert destination.read_bytes() == before[0]
    assert store.read() == before[1]
    alias = destination.with_name("alias.json")
    alias.symlink_to(destination)
    with pytest.raises(FileExistsError):
        replace(command, profile=alias).apply(store)
    with pytest.raises(OSError) as refused_alias:
        UiConnectionProfile.read(alias)
    assert refused_alias.value.errno == errno.ELOOP
    assert store.read() == before[1] and destination.read_bytes() == before[0]
    independent = store.update(lambda grants: grants.ui_issued(receipt.namespace, 60))
    revoke = ConnectorVerb.parse(["revoke-ui", "--grant", str(receipt.grant_id)])
    assert revoke.apply(store) == 0
    with pytest.raises(GrantRefusedError):
        store.read().caller_for(profile.credential.token)
    assert store.read().caller_for(independent.token) == UiReadCaller(receipt.namespace)
    destination.chmod(0o644)
    with pytest.raises(ValueError):
        UiConnectionProfile.read(destination)


@pytest.mark.parametrize("caller_type", [
    member.caller_type for member in UiGrantAccess.members_with(UiGrantAccess)
])
def test_ui_grant_expiry_scope_and_direct_mutation_admission(
    store: ConnectorStore, comms: Comms, caller_type: type[ScopedUiCaller],
) -> None:
    prepare_native_comms(comms)
    namespace = comms.registry.store.privacy().namespace
    expired = UiReadAccess(UiGrantId.mint(), namespace, expires_at=time.time() - 1)
    with pytest.raises(GrantRefusedError):
        expired.caller()
    foreign = caller_type(RootId.mint())
    with pytest.raises(GrantRefusedError):
        foreign.require_root(comms.root)
    with pytest.raises(GrantRefusedError):
        foreign.prepare_call(comms, foreign)
    with pytest.raises(GrantRefusedError):
        foreign.invoke(CommsChannelsTool(), comms)
    reader = UiReadCaller(namespace)
    reader.require_root(comms.root)
    with pytest.raises(ToolRefusedError, match="declared reads only"):
        reader.invoke(CommsPinChannelTool(name="#any", pinned=True), comms)
    for seconds in (0, UiReadAccess.maximum_seconds + 1):
        with pytest.raises(ValueError):
            store.read().ui_issued(namespace, seconds)
    assert UiReadAccess.maximum_seconds == 7 * 24 * 3600
    week = store.read().ui_issued(namespace, UiReadAccess.maximum_seconds).result
    assert week.expires_at > time.time() + 6 * 24 * 3600


@pytest.mark.parametrize("endpoint", [
    "http://core.example/mcp", "https://secret@core.example/mcp",
    "https://core.example/mcp?token=secret", "https://core.example/mcp#fragment",
])
def test_remote_ui_endpoint_refuses_insecure_or_credential_urls(endpoint: str) -> None:
    with pytest.raises(MalformedRecord):
        HttpsMcpEndpoint(URL(endpoint))


@pytest.mark.parametrize(("grant_type", "can_write"), [
    (UiReadAccess, False), (UiAccess, True),
])
async def test_ui_mcp_uses_granted_capability_without_an_agent(
    store: ConnectorStore, comms: Comms,
    grant_type: type[UiGrantAccess], can_write: bool,
) -> None:
    prepare_native_comms(comms)
    comms.channels.create_tag("remote-ui")
    namespace = comms.registry.store.privacy().namespace
    expected = CommsChannelsTool().apply(comms, NoThreadCaller())
    before = comms.bus.bus.revision(), comms.registry.snapshot()
    connector = Connector(store, "http://127.0.0.1:18766", comms.root)
    credential = store.update(lambda grants: grants.ui_issued(namespace, 60, grant_type))
    headers = {"Authorization": f"Bearer {credential.token}"}
    async with TestServer(connector.application()) as server:
        async with TestClient(server) as client:
            listing = await client.post("/mcp", json=LIST, headers=headers)
            assert listing.status == 200
            call = ToolsCallMessage(
                jsonrpc="2.0", id=2, params=CommsChannelsTool().called())
            response = await client.post("/mcp", json=FieldCodec.encode(call), headers=headers)
            reply = FieldCodec.decode(ToolsCallReply, await response.json()).result
            assert reply.is_error is False
            assert FieldCodec.decode(ChannelListing, reply.structured_content) == expected
            write = CommsPinChannelTool(name="#remote-ui", pinned=True)
            call = ToolsCallMessage(jsonrpc="2.0", id=3, params=ToolCall(
                write.declared_name, FieldCodec.encode_fields(write)))
            response = await client.post("/mcp", json=FieldCodec.encode(call), headers=headers)
            written = FieldCodec.decode(ToolsCallReply, await response.json()).result
            assert written.is_error is not can_write
            selected, = (row.channel for row in CommsChannelsTool().apply(
                comms, NoThreadCaller()).channels if row.channel.name == "#remote-ui")
            assert selected.pinned is can_write
            store.revoke_ui(credential.grant_id)
            revoked = await client.post("/mcp", json=LIST, headers=headers)
            assert revoked.status == 401
    assert comms.bus.bus.revision() == before[0]
    assert comms.registry.snapshot() == before[1]


def test_human_publication_refuses_other_principals_before_changing_state(comms: Comms) -> None:
    prepare_native_comms(comms)
    namespace = comms.registry.store.privacy().namespace
    before = comms.bus.bus.revision(), comms.registry.snapshot()
    request = ServedCommsUserSendTool(target="recipient", body="not human input")
    for caller in (NoThreadCaller(), ThreadCaller("recipient"), CALLER,
                   UiReadCaller(namespace), UiCaller(RootId.mint())):
        with pytest.raises(ToolRefusedError):
            caller.invoke(request, comms)
        assert comms.bus.bus.revision() == before[0]
        assert comms.registry.snapshot() == before[1]


async def test_ui_mcp_publishes_with_the_durable_human_identity(
    store: ConnectorStore, comms: Comms,
) -> None:
    prepare_native_comms(comms)
    comms.registry.declare(NativeThread("recipient", frozenset(), Checkout(comms.root)))
    before = comms.registry.snapshot()
    local = FieldCodec.decode(MessagePublished, FieldCodec.encode(HumanCaller().invoke(
        ServedCommsUserSendTool(target="recipient", body="local human input"), comms).to_wire()))
    assert local.message.matches(comms.bus.inbox("recipient").page.messages[-1])
    namespace = comms.registry.store.privacy().namespace
    credential = store.update(lambda grants: grants.ui_issued(namespace, 60, UiAccess))
    headers = {"Authorization": f"Bearer {credential.token}"}
    connector = Connector(store, "http://127.0.0.1:18766", comms.root)
    async with TestServer(connector.application()) as server:
        async with TestClient(server) as client:
            for number, body in enumerate(("first human input", "second human input"), 2):
                request = CommsUserSendTool(target="recipient", body=body)
                call = ToolsCallMessage(jsonrpc="2.0", id=number, params=ToolCall(
                    request.declared_name, FieldCodec.encode_fields(request)))
                response = await client.post("/mcp", json=FieldCodec.encode(call), headers=headers)
                assert response.status == 200
                reply = FieldCodec.decode(ToolsCallReply, await response.json()).result
                assert reply.is_error is False
                published = FieldCodec.decode(MessagePublished, reply.structured_content)
                original = comms.bus.inbox("recipient").page.messages[-1]
                assert published.id == original.message_id
                assert published.message.matches(original)
                assert original.body == body and original.sender_role is UserThreadRole
    messages = comms.bus.inbox("recipient").page.messages
    assert len(messages) == 3 and {message.sender for message in messages} == {messages[0].sender}
    assert set(comms.registry.snapshot().threads) - set(before.threads) == {messages[0].sender}
    assert comms.registry.require(messages[0].sender).role is UserThreadRole


def test_a_cloud_session_on_the_claude_ai_grant_acts_under_its_own_name(comms):
    """Several remote Claude sessions share claude-ai's grant; each names itself with the fork
    argument and is registered as claude-ai's child under that name, so its work is its own."""
    comms.registry.declare(NativeThread(CALLER.name, frozenset(), Checkout(comms.root)))
    acting, rest = CALLER.acting({"fork": {"name": "claude-cloud-1"}, "thread": "worker"})
    assert rest == {"thread": "worker"}
    CALLER.prepare_call(comms, acting)
    assert comms.registry.require("claude-cloud-1").parent.names() == ("claude-ai",)
    assert "fork" in CALLER.input_schema(CommsSendTool)["properties"]


async def remote_answer(client: TestClient, token: str, request):
    """One request through the real HTTP tools/call, decoded the way RemoteCoreAccess does."""

    envelope = ToolsCallMessage(jsonrpc="2.0", id=3, params=request.called())
    reply = FieldCodec.decode(ToolsCallReply, await (await client.post(
        "/mcp", headers={"Authorization": f"Bearer {token}"},
        json=FieldCodec.encode(envelope))).json())
    assert not reply.result.is_error, reply.result.content
    return FieldCodec.decode(request.result_type(), reply.result.structured_content)


async def test_a_ui_reads_one_conversation_page_the_same_locally_and_remotely(
    client, comms, local_core
):
    """The UI's page request answers in process and through tools/call with one record."""
    from agent_comms.transport.tools.sidebar import CommsConversationPageTool

    comms.registry.declare(NativeThread(CALLER.name, frozenset(), Checkout(comms.root)))
    sent = comms.messaging.send_user_message(
        ThreadTarget("worker"), "page through the boundary", worktree=Checkout(comms.root))
    project = Checkout(comms.root)
    request = CommsConversationPageTool(conversation=ThreadTarget("worker"), project=project)

    local = await local_core.call(request)
    assert sent.message_id in [message.message_id for message in local.messages]
    # The UI's project, not the bus root, is the worktree the read proof records.
    assert local.selection.identity.project_path == project.resolved().path

    remote = await remote_answer(client, await approved_token(client, comms), request)
    assert [m.message_id for m in remote.messages] == [m.message_id for m in local.messages]
    assert remote.selection == local.selection  # the read proof comes back for the read mark


@pytest.mark.parametrize("conversation", [ThreadTarget("worker"), CommandTarget.named("#comms")])
async def test_a_ui_checks_a_conversation_for_changes_the_same_locally_and_remotely(
    client, comms, local_core, conversation
):
    """A revision is what the UI keeps and sends back with its next check, so it must come
    back from the connector as the same record, channel or DM, and settle once nothing
    changed. The latest-sequence bound travels the same way."""
    from agent_comms.transport.tools.sidebar import (
    CommsConversationRevisionTool,
    CommsLatestSequenceTool,
)

    comms.registry.declare(NativeThread(CALLER.name, frozenset({"comms"}), Checkout(comms.root)))
    comms.messaging.send_user_message(
        conversation, "check through the boundary", worktree=Checkout(comms.root))
    token = await approved_token(client, comms)
    first = CommsConversationRevisionTool(conversation=conversation, project=Checkout(comms.root))

    local = await local_core.call(first)
    remote = await remote_answer(client, token, first)
    assert remote == local

    again = replace(first, seen=remote)
    settled = await remote_answer(client, token, again)
    assert settled == await local_core.call(again)
    assert not settled.display_changed_since(remote)

    latest = CommsLatestSequenceTool()
    assert await remote_answer(client, token, latest) == await local_core.call(latest)
    assert (await local_core.call(latest)).cursor.sequence == comms.views.latest_sequence()


async def test_an_inbox_read_after_its_place_the_same_locally_and_remotely(
    client, comms, local_core
):
    """An inbox read crosses the connector with its place and its wait, and
    answers what a local read answers."""
    from agent_comms.messaging.agent_inbox import SecondsWait, TakenInputAfter
    from agent_comms.transport.tools.inbox import CommsInboxTool

    comms.registry.declare(NativeThread(CALLER.name, frozenset(), Checkout(comms.root)))
    first = comms.messaging.send(CALLER.name, "worker", "first").message
    comms.messaging.send(CALLER.name, "worker", "second")
    request = CommsInboxTool(
        thread="worker", after=TakenInputAfter(first.reference), wait=SecondsWait(1))

    local = await local_core.call(request)
    remote = await remote_answer(client, await approved_token(client, comms), request)
    assert [m.body for m in remote.messages] == [m.body for m in local.messages] == ["second"]



def test_every_session_is_told_how_to_take_its_own_name() -> None:
    """The fork argument's description survives its union type, so each session sees it."""
    from agent_comms.transport.connector_caller import ActingFork
    from agent_comms.transport.tools.send import CommsSendTool

    fork = FieldCodec.record_schema(CommsSendTool, ActingFork)["properties"]["fork"]
    assert "Your own name on the bus" in fork["description"]
    assert "distinct name" in InitializeResult.announcing("a bus").instructions


async def test_connector_reports_the_root_its_core_serves(client, comms):
    """A remote UI keys its observation by the served root, read through the same tools/call."""
    from agent_comms.messaging.bus_identity import BusIdentityStore

    minted = BusIdentityStore.minted_for(comms.root.resolve())
    headers = {"Authorization": f"Bearer {await approved_token(client, comms)}"}
    response = await client.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "comms_served_root", "arguments": {}},
    })
    result = (await response.json())["result"]
    assert result["isError"] is False
    answered = FieldCodec.decode(ServedRoot, result["structuredContent"])
    assert answered.root == str(comms.root.resolve())
    # The path is the observation key; the identity is what says whose key it is.
    assert answered.bus.same_bus(minted)


async def test_two_buses_at_one_path_do_not_answer_as_one_bus(tmp_path):
    """Every machine in this fleet runs as the same user with its bus at the same path, so a
    served root is the same answer on all of them. The identity minted at each root is not.

    On 2026-11-07 a session read another machine's bus through a second connector and took
    that bus's thread of its own name for itself; nothing it could ask disagreed.
    """
    from agent_comms.messaging.bus_identity import BusIdentityStore

    one, other = tmp_path / "one", tmp_path / "other"
    for root in (one, other):
        root.mkdir()
        BusIdentityStore.minted_for(root)

    # As the fleet presents them: one path, one user, one hostname, two buses.
    here = ServedRoot("/home/ts/.agent-comms", BusIdentityStore.serving(one))
    there = ServedRoot("/home/ts/.agent-comms", BusIdentityStore.serving(other))

    assert here.root == there.root
    assert not here.bus.same_bus(there.bus)
    assert not there.bus.same_bus(here.bus)
    # And it still recognizes itself: a check that refuses everything is no check.
    assert here.bus.same_bus(ServedRoot(here.root, BusIdentityStore.serving(one)).bus)


async def test_a_connector_names_its_bus_at_the_handshake(client, comms):
    """A client that learns which bus it reached from a tool call learns it after reading
    something. The one thing that differs between two connectors is said before anything."""
    from agent_comms.messaging.bus_identity import BusIdentityStore

    minted = BusIdentityStore.minted_for(comms.root.resolve())
    headers = {"Authorization": f"Bearer {await approved_token(client, comms)}"}
    response = await client.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                   "clientInfo": {"name": "test", "version": "1"}},
    })
    instructions = (await response.json())["result"]["instructions"]
    assert str(comms.root.resolve()) in instructions
    # The token, not the sentence around it: the sentence a bus with no identity answers
    # opens with the same words up to the root, which is how this first passed vacuously.
    named = re.search(r"identified as (\w+)", instructions)
    assert named and minted.holds(named.group(1)), f"No bus is named in: {instructions}"
    assert "distinct name" in instructions


async def test_a_call_that_asks_for_progress_streams_it_then_its_reply(client, store, comms, tmp_path):
    """MCP progress: a tools/call carrying a progress token is answered as server-sent events,
    the export's progress notices first and its reply last."""
    from agent_comms.messaging.exporting import EverythingScope, FullLimit, JsonlFormat
    from agent_comms.transport.tools.sidebar import CommsExportWireTool

    prepare_native_comms(comms)
    namespace = comms.registry.store.privacy().namespace
    token = store.update(lambda grants: grants.ui_issued(namespace, 60, UiAccess)).token
    destination = tmp_path / "export.jsonl"
    response = await client.post("/mcp", headers={
        "Authorization": f"Bearer {token}", "Accept": "text/event-stream"}, json={
        "jsonrpc": "2.0", "id": "export-1", "method": "tools/call",
        "params": {"name": "comms_export_wire", "_meta": {"progressToken": "export-1"},
                   "arguments": CommsExportWireTool(
                       destination=destination, format=JsonlFormat(), scope=EverythingScope(),
                       limit=FullLimit()).called().arguments}})
    assert response.headers["Content-Type"].startswith("text/event-stream")

    class Recorded(WireExportObserver):
        def __init__(self):
            self.progress = []

        def advanced(self, progress):
            self.progress.append(progress)

    observer, replies = Recorded(), []
    async for line in response.content:
        StreamedLine(line).delivered(observer, replies)
    (reply,) = replies
    assert reply.structured()["destination"] == str(destination)
    assert destination.exists()


@pytest.mark.parametrize("meta, streamed", [
    ({"claudecode/toolUseId": "toolu_1"}, False),
    ({"progressToken": 7, "claudecode/toolUseId": "toolu_1"}, True),
    ({"progressToken": "call-7"}, True),
], ids=["client-key-only", "claude-code", "string-token"])
def test_a_call_meta_reads_its_declared_key_from_an_open_object(meta, streamed):
    """Installing cb375c3c turned every Claude Code call into a 500: its `_meta` carries a numeric
    progressToken and its own claudecode/toolUseId, both of which MCP allows."""

    progress = ToolCallMeta.of(meta).progress
    assert progress == (ProgressToken(meta["progressToken"]) if streamed else NoProgress())


async def test_a_cancel_reaches_the_running_call_it_names(connector):
    """notifications/cancelled stops the observed call with that request id and no other."""
    from agent_comms.transport.progress import CancellableWireExport

    named, other = CancellableWireExport(), CancellableWireExport()
    connector.observed["export-1"] = named
    connector.observed["export-2"] = other
    message = CancelledMessage(jsonrpc="2.0", params={"requestId": "export-1"})
    assert (await message.answer(connector, None, None)).status == 202
    assert named.cancelled() and not other.cancelled()


async def test_an_invalid_order_or_a_failed_git_command_is_a_refusal_not_a_lost_connection(
    client, comms, monkeypatch,
):
    """comms_todo_create raised ValueError, UnrecordedTimeError and FailedGitCommand past the
    handler: HTTP 500, which the client reports as "MCP server connection lost", so the caller
    never learned the field to fix. Invalid input and any CommandFailure answer as refusals."""
    token = await approved_token(client, comms)
    failures = (
        (51, ValueError("The order check directory does not exist."), "does not exist"),
        (52, FailedGitCommand("agent-comms", subprocess.CompletedProcess(
            ("git",), 128, b"", b"fatal: cannot change to 'agent-comms'\n")), "cannot change to"),
    )
    for call_id, error, shown in failures:
        async def raising(self, *args, error=error, **kwargs):
            raise error
        monkeypatch.setattr(Connector, "call", raising)
        status, reply = await tool_call(client, token, call_id, CommsChannelsTool.declared_name, {})
        assert (status, reply["id"], reply["result"]["isError"]) == (200, call_id, True)
        assert shown in reply["result"]["content"][0]["text"]


async def test_a_request_stalled_before_its_tool_call_still_answers(client, comms, monkeypatch):
    """codex-latency, 2026-11-08: the first bound started inside call(), but mcp awaits
    bearer_caller, request.json and session dispatch before reaching it, so an
    authenticated request that never finishes its body waited forever and no deadline
    applied. The bound belongs around the whole handler, which is the scope log_route
    already owns.
    """
    token = await approved_token(client, comms)
    stalled = asyncio.Event()
    authentic = Connector.bearer_caller

    async def stalls_in_the_auth_phase(self, request):
        caller = await authentic(self, request)
        await stalled.wait()  # mcp() awaits this before it ever reaches call()
        return caller

    monkeypatch.setattr(BoundedRoute, "seconds", 0.25)
    monkeypatch.setattr(Connector, "bearer_caller", stalls_in_the_auth_phase)
    try:
        answer = await client.post(
            "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"Authorization": f"Bearer {token}"})
        assert answer.status == web.HTTPGatewayTimeout.status_code, await answer.text()
        assert "deadline" in await answer.text()
    finally:
        stalled.set()


async def test_the_connector_names_work_neither_collection_holds(tmp_path, comms):
    """On 2026-11-08 a POST /mcp stayed open 6883 seconds and the stop reported
    "pending tools: none". Both halves were true: nothing was pending in self.calls, and a
    handler had not finished. A request task is in neither self.calls nor self.watch_tasks,
    so the report named everything except the thing that was stuck, and no journal line could
    fill the gap because an access line is only printed once its handler ends.

    The event loop already tracks every task. This asks the loop, so the one fact needed to
    name a stuck handler's phase, its innermost frame, is in the report.
    """
    connector = Connector(ConnectorStore.at(tmp_path / "auth"),
                          "http://127.0.0.1:18766", comms.root)
    held = asyncio.Event()

    async def stuck_handler() -> None:
        await held.wait()

    async def body_read() -> None:
        await held.wait()

    async def session_claim() -> None:
        await body_read()

    async def stuck_handler_nested() -> None:
        await session_claim()

    task = asyncio.create_task(stuck_handler_nested(), name="a-request-nobody-tracks")
    await asyncio.sleep(0)
    try:
        assert task not in connector.calls, "a request task is not a tool call"
        assert task not in connector.watch_tasks, "a request task is not a watch"

        report = connector.still_running()

        assert "a-request-nobody-tracks" in report, report
        assert "test_connector.py:" in report, "the frame must name where it is sitting"
        # The leaf is the phase, and it is the whole point: Task.get_stack returns
        # stuck_handler_nested alone for this task, which names what is stuck and not where.
        assert task.get_stack(limit=6)[0].f_code.co_name == "stuck_handler_nested"
        assert report.index("body_read") < report.index("session_claim") < report.index(
            "stuck_handler_nested"
        ), f"leaf first, so the phase reads before its callers: {report}"

        # A phase alone cannot tell a slow call from a hang; the age can. log_route owns
        # the start time and prints it only once the handler ends, so a request carries it
        # while it runs and the report reads it back.
        connector.running_requests[task] = RunningRequest(
            method="POST", template="/mcp", started=time.monotonic() - 6883.0)
        timed = connector.still_running()
        assert "POST /mcp" in timed, timed
        assert "688" in timed, f"the report must say how long it has been waiting: {timed}"
        assert "stuck_handler_nested" in timed, timed
        # Every other task answers no request, so nothing claims an age it does not have.
        assert timed.count("into POST /mcp") == 1, timed
        # 6883s on a route whose p999 is 0.888s is news, and the report says so.
        assert "OVERDUE" in timed, timed

        # The same duration on a route whose job is staying open is not news at all:
        # one global bound would have called 140 healthy observes overdue on 2026-11-08.
        connector.running_requests[task] = RunningRequest(
            method="GET", template=OBSERVE_PATH, started=time.monotonic() - 6883.0,
            bound=LongLivedRoute())
        observing = connector.still_running()
        assert "into GET /observe" in observing, observing
        assert "OVERDUE" not in observing, observing
    finally:
        connector.running_requests.pop(task, None)
        held.set()
        await task
