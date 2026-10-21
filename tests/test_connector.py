"""The claude.ai connector: OAuth in front of an MCP transport over the ToolRequest family."""

import asyncio
import base64
import fcntl
import hashlib
import os
import secrets
import json
import logging
import re
import threading
import time
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from aiohttp import ClientSession, CookieJar, web
from aiohttp.test_utils import TestClient, TestServer

from agent_comms.active_route import active_route_path
from agent_comms.comms import Comms
from agent_comms.connector import (
    CALLER,
    ApprovalLink,
    AuthorizationCodeGrant,
    Connector,
    ConnectorVerb,
    ServeVerb,
    RevokeVerb,
)
from agent_comms.connector_store import (
    CALLBACK, CODE_SECONDS, AuthorizationQuery, ConnectorGrants, ConnectorStore,
    IssuedCode, TrustedBrowser, digest,
)
from agent_comms.coordinator import Coordination
from agent_comms.message_bus import WatchPass
from agent_comms.field_codec import FieldCodec
from agent_comms.installer.backup import StoreBackup
from agent_comms.maintenance_barrier import (
    InstallingPhase, MaintenanceBarrier, MaintenanceOperator, PausedPhase, ReadyPhase,
)
from agent_comms.store_files import _store_lock
from agent_comms.tools import ReadTool, ToolRequest, invoke_tool

LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


def test_serve_requires_explicit_root(tmp_path, capsys):
    with pytest.raises(SystemExit) as missing:
        ConnectorVerb.parse(["serve"])
    assert missing.value.code == 2
    assert "--root" in capsys.readouterr().err
    command = ConnectorVerb.parse(["serve", "--root", str(tmp_path)])
    assert isinstance(command, ServeVerb)
    assert command.root == tmp_path


def test_real_connector_refusals_and_replaced_source(tmp_path, source_programs):
    """The actual serve entrypoint, curl transport and production maintenance thread."""
    import os
    import subprocess
    import sys

    project = Path(__file__).resolve().parents[1]
    environment = {**os.environ, "PYTHONPATH": str(project / "src"),
                   "PATH": os.pathsep.join((str(source_programs.prefix / "bin"), os.environ["PATH"]))}
    proof = subprocess.run(
        [sys.executable, "-X", "faulthandler", str(project / "tests/verify_connector_live.py"),
         str(tmp_path / "live-connector")],
        env=environment, capture_output=True, text=True, timeout=60,
    )
    assert proof.returncode == 0, proof.stdout + proof.stderr
    assert "same maintenance thread completed normally" in proof.stdout


@pytest.mark.parametrize("registered", [True, False])
def test_connector_round_trip_reads_this_bus_as_its_caller(comms, tmp_path, registered):
    import json
    import os
    import site
    import socket
    import subprocess
    import sys

    comms.registry.declare(comms.registry.new_thread("claude-ai", frozenset(), str(comms.root)))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    environment = {**os.environ, "HOME": str(tmp_path / "home"), "PYTHONUNBUFFERED": "1",
        "PYTHONPATH": os.pathsep.join((str(Path(__file__).resolve().parents[1] / "src"),
                                       *site.getsitepackages()))}
    command = "from agent_comms.connector import main; raise SystemExit(main())"
    before = comms.bus.log.latest_sequence()
    service = subprocess.Popen(
        [sys.executable, "-c", command, "serve", "--root", comms.root, "--port", str(port)],
        env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        started = ""
        while "Running on" not in started:
            line = service.stdout.readline()
            assert line, started
            started += line
        if not registered:
            comms.registry.unregister("claude-ai")
            comms.registry.archive("claude-ai")
            comms.registry.begin_delete("claude-ai")
            comms.registry.remove("claude-ai")
        result = subprocess.run(
            [sys.executable, "-c", command, "round_trip", comms.root,
             "--port", str(port)], env=environment,
            capture_output=True, text=True, timeout=60)
        if registered:
            assert result.returncode == 0, result.stderr
            assert json.loads(result.stdout) == {
                "round_trip": f"comms_channels read alike on port {port}"}
        else:
            assert result.returncode != 0
            assert "connector round trip failed" in result.stdout + result.stderr
    finally:
        service.terminate()
        service.wait(timeout=30)
    assert comms.bus.log.latest_sequence() == before


@pytest.fixture
def store(tmp_path: Path) -> ConnectorStore:
    return ConnectorStore.at(tmp_path)


@pytest.fixture
def connector(store: ConnectorStore, root: Path, comms: Comms):
    for created_at, name in enumerate(("worker", "claude-ai"), start=1):
        comms.registry.declare(comms.registry.new_thread(name, frozenset(), str(root), created_at=created_at))
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


async def approved_token(client: TestClient, comms: Comms) -> str:
    """Register, confirm the real owner-message link, and redeem its PKCE-bound code."""
    login = await ClientLogin.register(client)
    parameters = FieldCodec.encode(login.query)
    assert (await client.head("/authorize", params=parameters, allow_redirects=False)).status == 405
    waiting = await client.get("/authorize", params=parameters, allow_redirects=False)
    assert waiting.status == 200
    owner = comms.messaging.user_identity(str(comms.root))
    notice = comms.bus.inbox(owner.name)[-1]
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


async def test_request_log_names_the_route_and_never_its_values(client, store, root, comms, caplog):
    """Serve actual GET/POST approval secrets, with aiohttp's raw access log off."""
    login = await ClientLogin.register(client)
    assert (await client.get(
        "/authorize", params=FieldCodec.encode(login.query), allow_redirects=False
    )).status == 200
    owner = comms.messaging.user_identity(str(comms.root))
    link = urlsplit(comms.bus.inbox(owner.name)[-1].body.splitlines()[-1])
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
class PublishingWatch(WatchPass):
    calls: int = 0
    completed: threading.Event = field(default_factory=threading.Event)

    def pass_body(self, comms: Comms) -> None:
        self.calls += 1
        comms.messaging.send("claude-ai", "worker", "One watch pass")
        self.completed.set()


async def test_watch_pass_waits_for_install_then_runs_once(comms, copied_connector, caplog):
    original = comms.messaging.send("worker", "claude-ai", "Original stays unchanged").message
    connector = await copied_connector()
    copied = Comms(connector.root)
    barrier = MaintenanceBarrier(copied.registry.store.path)
    held = barrier.begin(MaintenanceOperator.current())
    held = barrier.advance(held, PausedPhase)
    held = barrier.advance(held, InstallingPhase)
    opened = threading.Event()

    @contextmanager
    def opened_wire():
        with connector.service_wire() as wire:
            opened.set()
            yield wire

    watch = PublishingWatch()
    pending = asyncio.create_task(Coordination.watch_worker(watch, opened_wire))
    try:
        try:
            assert await asyncio.to_thread(opened.wait, 3)
            assert not await asyncio.to_thread(watch.completed.wait, .05)
            assert watch.calls == 0
            assert copied.bus.log.latest_sequence() == original.seq
            assert not [record for record in caplog.records if record.levelno >= logging.ERROR]
        finally:
            barrier.advance(held, ReadyPhase)
        assert await asyncio.to_thread(watch.completed.wait, 3)
    finally:
        pending.cancel()
        with suppress(asyncio.CancelledError):
            await pending
    assert watch.calls == 1
    assert [message.body for message in copied.bus.inbox("worker")] == ["One watch pass"]
    assert comms.bus.log.latest_sequence() == original.seq
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]


class FailingWatch(WatchPass):
    def pass_body(self, comms: Comms) -> None:
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
    pending = asyncio.create_task(Coordination.watch_worker(FailingWatch(), connector.service_wire))
    try:
        await asyncio.wait_for(logged.wait(), 3)
    finally:
        pending.cancel()
        with suppress(asyncio.CancelledError):
            await pending
        logger.removeHandler(handler)
    error, = (record for record in caplog.records if record.levelno >= logging.ERROR)
    assert error.exc_info[0] is RuntimeError
    assert str(error.exc_info[1]) == "A real watch failure"
    assert error.exc_info[2] is not None


async def test_registration_accepts_only_the_claude_callback(client):
    refused = await client.post(
        "/register", json={"redirect_uris": ["https://elsewhere.example/cb"]}
    )
    assert refused.status == 400


async def test_connector_lists_every_tool(client, store, comms):
    token = await approved_token(client, comms)
    headers = {"Authorization": f"Bearer {token}"}
    listed = (await (await client.post("/mcp", json=LIST, headers=headers)).json())["result"]["tools"]
    family = ToolRequest.members_with(ToolRequest)
    assert [tool["name"] for tool in listed] == [tool.declared_name for tool in family]
    assert [tool["annotations"]["readOnlyHint"] for tool in listed] == [
        issubclass(tool, ReadTool) for tool in family]
    # Every write tool says, from the one write marker, that claude-ai may act on its authorization.
    writes = [tool["description"] for tool in listed if not tool["annotations"]["readOnlyHint"]]
    assert writes and all(description.endswith(ToolRequest.bus_effect) for description in writes)
    assert all("claude-ai to call this without asking" in description for description in writes)


async def test_connector_reads_two_newest_first_conversation_pages(client, store, comms):
    comms.registry.declare(comms.registry.new_thread("commander", frozenset(), str(comms.root)))
    originals = [comms.messaging.send("worker", "claude-ai", f"history {number}").message
                 for number in range(5)]
    comms.messaging.send("commander", "claude-ai", "not this conversation")
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
    from agent_comms.channel_targets import CommandTarget

    comms.channels.create_tag("comms")
    for name in ("worker", "claude-ai"):
        comms.channels.update_tags(name, add=frozenset({"comms"}))
    originals = [comms.messaging.send(
        "worker", "#comms", f"@claude-ai channel post {number}").message
        for number in range(55)]
    connector = await copied_connector()
    copied = Comms(connector.root)
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
             {"name": "comms_send", "arguments": {"from": "claude-ai", "to": "worker", "body": "Owner said go"}})
    for call in calls:
        reply = await (await client.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": call})).json()
        assert reply["result"]["isError"] is False, reply
    assert comms.registry.require("claude-ai").goal.text == "Ship the connector"
    assert [(message.sender, message.body) for message in comms.bus.inbox("worker")] == [
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
    in_process = invoke_tool(Comms(root), "comms_threads", {}, caller=CALLER)
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
        headers = {"Authorization": f"Bearer {await approved_token(reader, Comms(copied_root))}"}
        before = (copied_root / "bus.jsonl").read_bytes()
        barrier = MaintenanceBarrier(copied_root / "registry.json")
        held = barrier.begin(MaintenanceOperator.current())
        for phase in phases:
            held = barrier.advance(held, phase)
        with _store_lock(barrier.wire_path), _store_lock(barrier.registry_path), ExitStack() as locks:
            directory = os.open(active_route_path().parent, os.O_RDONLY | os.O_DIRECTORY)
            locks.callback(os.close, directory)
            fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
            started = time.monotonic()
            response = await reader.post("/mcp", headers=headers, timeout=2, json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "comms_send", "arguments": {
                    "from": "claude-ai", "to": "worker", "body": "Please read this message"}},
            })
            reply = await response.json()
            elapsed = time.monotonic() - started
            assert elapsed < 1, (elapsed, reply)
            assert response.status == 200
            result = reply["result"]
            assert result["isError"] is True
            refusal = json.loads(result["content"][0]["text"])
            assert refusal == {
                "type": "held", "error": "Maintenance holds ingress: the bus is under maintenance"}
            assert barrier.current_unlocked() == held
            assert (copied_root / "bus.jsonl").read_bytes() == before
    assert comms.bus.log.latest_sequence() == original.seq
    assert comms.owners.maintenance.read().turn_pending is False


async def test_approval_previews_do_not_grant_browser_trust(client, store, comms):
    login = await ClientLogin.register(client)
    assert (await client.get(
        "/authorize", params=FieldCodec.encode(login.query), allow_redirects=False
    )).status == 200
    owner = comms.messaging.user_identity(str(comms.root))
    link = urlsplit(comms.bus.inbox(owner.name)[-1].body.splitlines()[-1])
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
    assert store.read().holds(token)


async def test_pending_authorizations_are_bounded(client, store, comms):
    login = await ClientLogin.register(client)
    for number in range(ConnectorGrants.waiting_limit):
        query = replace(login.query, state=str(number))
        assert (await client.get(
            "/authorize", params=FieldCodec.encode(query), allow_redirects=False
        )).status == 200
    owner = comms.messaging.user_identity(str(comms.root))
    before = len(comms.bus.inbox(owner.name))
    assert before == ConnectorGrants.waiting_limit
    blocked = replace(login.query, state="overflow")
    for _ in range(2):
        refused = await client.get(
            "/authorize", params=FieldCodec.encode(blocked), allow_redirects=False
        )
        assert refused.status == 429 and int(refused.headers["Retry-After"]) == CODE_SECONDS
    assert len(store.read().requests) == ConnectorGrants.waiting_limit
    assert blocked.request_id not in store.read().requests
    assert len(comms.bus.inbox(owner.name)) == before
    repeated = replace(login.query, state="0")
    assert (await client.get(
        "/authorize", params=FieldCodec.encode(repeated), allow_redirects=False
    )).status == 200
    assert len(comms.bus.inbox(owner.name)) == before
    held = store.read().requests[repeated.request_id]
    store.update(lambda grants: replace(grants, requests=grants.requests | {
        repeated.request_id: replace(held, expires_at=time.time() - 1)
    }))
    assert (await client.get(
        "/authorize", params=FieldCodec.encode(blocked), allow_redirects=False
    )).status == 200
    assert len(store.read().requests) == ConnectorGrants.waiting_limit
    assert len(comms.bus.inbox(owner.name)) == before + 1


async def test_owner_browser_approval_lifecycle(client, store, comms):
    """One real HTTP/store/bus journey: owner consent, remembered browser, expiry and revoke."""
    token = await approved_token(client, comms)
    owner = comms.messaging.user_identity(str(comms.root))
    notices = comms.bus.inbox(owner.name)
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
    assert grants.holds(token)

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
    assert len(comms.bus.inbox(owner.name)) == 1

    client.session.cookie_jar.clear()
    query = replace(query, state="untrusted-browser")
    for _ in range(2):
        waiting = await client.get(
            "/authorize", params=FieldCodec.encode(query), allow_redirects=False
        )
        assert waiting.status == 200
        assert "sent to the owner on comms" in await waiting.text()
    notices = comms.bus.inbox(owner.name)
    assert len(notices) == 2  # Retry the same waiting query, not the owner's message.
    link = urlsplit(notices[-1].body.splitlines()[-1])
    assert (await client.get(link.path + "wrong", allow_redirects=False)).status == 400
    expired = replace(store.read().requests[query.request_id], expires_at=time.time() - 1)
    store.update(lambda grants: replace(
        grants, requests={query.request_id: expired},
        codes=grants.codes | {"expired-code": IssuedCode(
            query.client_id, query.code_challenge, expires_at=expired.expires_at
        )},
    ))
    assert (await client.get(link.path, allow_redirects=False)).status == 400
    store.update(lambda grants: grants.issued(query.client_id)[0])
    assert not store.read().requests and "expired-code" not in store.read().codes

    waiting = await client.get("/authorize", params=FieldCodec.encode(query))
    assert waiting.status == 200
    held = store.read().requests[query.request_id]
    store.update(lambda grants: replace(grants, requests={
        query.request_id: replace(held, expires_at=time.time() - 1)
    }))
    fresh = replace(query, state="fresh-wait")
    assert (await client.get("/authorize", params=FieldCodec.encode(fresh))).status == 200
    assert query.request_id not in store.read().requests
    assert store.read().requests[fresh.request_id].expires_at <= time.time() + CODE_SECONDS

    assert RevokeVerb().apply(store) == 0
    assert not store.read().trusted_browsers
    client.session.cookie_jar.update_cookies({TrustedBrowser.cookie_name: browser})
    assert (await client.get(
        "/authorize", params=FieldCodec.encode(fresh), allow_redirects=False
    )).status == 200
    assert not store.read().codes and not store.read().trusts(browser)
    assert len(comms.bus.inbox(owner.name)) == 5


async def test_a_revoked_token_is_refused(client, store, comms):
    token = await approved_token(client, comms)
    assert RevokeVerb().apply(store) == 0
    refused = await client.post("/mcp", json=LIST, headers={"Authorization": f"Bearer {token}"})
    assert refused.status == 401
