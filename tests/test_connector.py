"""The claude.ai connector: OAuth in front of an MCP transport over the ToolRequest family."""

import base64
import fcntl
import hashlib
import os
import secrets
import shutil
import json
import logging
import time
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from aiohttp import ClientSession, CookieJar, web
from aiohttp.test_utils import TestClient, TestServer

from agent_comms.active_route import active_route_path
from agent_comms.comms import Comms
from agent_comms.connector import (
    CALLER,
    Connector,
    ConnectorVerb,
    ServeVerb,
    RevokeVerb,
)
from agent_comms.connector_store import (
    CALLBACK, CODE_SECONDS, AuthorizationQuery, ConnectorStore, IssuedCode, TrustedBrowser, digest,
)
from agent_comms.field_codec import FieldCodec
from agent_comms.maintenance_barrier import (
    InstallingPhase, MaintenanceBarrier, MaintenanceOperator, PausedPhase,
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


def test_real_connector_refusals_and_replaced_source():
    """The actual serve entrypoint, curl transport and production maintenance thread."""
    import os
    import subprocess
    import sys

    project = Path(__file__).resolve().parents[1]
    environment = {**os.environ, "PYTHONPATH": str(project / "src")}
    proof = subprocess.run(
        [sys.executable, "-X", "faulthandler", str(project / "tests/verify_connector_live.py")],
        env=environment, capture_output=True, text=True, timeout=60,
    )
    assert proof.returncode == 0, proof.stdout + proof.stderr
    assert "same maintenance thread completed normally" in proof.stdout


@pytest.mark.parametrize("served_by,registered", [
    ("installed", True), ("other", True), ("installed", False),
])
def test_connector_round_trip_owns_runtime_and_caller_validation(comms, tmp_path, served_by, registered):
    import json
    import os
    import site
    import socket
    import subprocess
    import sys

    comms.registry.declare(comms.registry.new_thread("claude-ai", frozenset(), str(comms.root)))
    runtimes = {name: tmp_path / name / "bin/python" for name in ("installed", "other")}
    for runtime in runtimes.values():
        runtime.parent.mkdir(parents=True)
        runtime.symlink_to(sys.executable)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    environment = {**os.environ, "HOME": str(tmp_path / "home"), "PYTHONUNBUFFERED": "1",
        "PYTHONPATH": os.pathsep.join((str(Path(__file__).resolve().parents[1] / "src"),
                                       *site.getsitepackages()))}
    command = "from agent_comms.connector import main; raise SystemExit(main())"
    before = comms.bus.log.latest_sequence()
    service = subprocess.Popen(
        [runtimes[served_by], "-c", command, "serve", "--root", comms.root, "--port", str(port)],
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
            [runtimes["installed"], "-c", command, "round_trip", comms.root,
             runtimes["installed"], "--port", str(port)], env=environment,
            capture_output=True, text=True, timeout=60)
        if registered and served_by == "installed":
            assert result.returncode == 0, result.stderr
            assert json.loads(result.stdout) == {
                "round_trip": f"comms_channels read alike through {runtimes['installed']}"}
        else:
            assert result.returncode != 0
            expected = ("connector round trip failed" if not registered
                        else f"runs {runtimes['other']}, not the installed")
            assert expected in result.stdout + result.stderr
    finally:
        service.terminate()
        service.wait(timeout=30)
    assert comms.bus.log.latest_sequence() == before


@pytest.fixture
def store(tmp_path: Path) -> ConnectorStore:
    return ConnectorStore.at(tmp_path)


@pytest.fixture
async def client(store: ConnectorStore, root: Path, comms: Comms):
    for created_at, name in enumerate(("worker", "claude-ai"), start=1):
        comms.registry.declare(comms.registry.new_thread(name, frozenset(), str(root), created_at=created_at))
    connector = Connector(store, "http://127.0.0.1:18766", root)
    async with TestServer(connector.application()) as server:
        cookies = CookieJar(unsafe=True, treat_as_secure_origin=server.make_url("/"))
        async with TestClient(server, cookie_jar=cookies) as client:
            yield client


async def approved_token(client: TestClient, comms: Comms) -> str:
    """Register, open the real owner-message link, and redeem its PKCE-bound code."""
    registered = await (
        await client.post(
            "/register", json={"redirect_uris": [CALLBACK], "client_name": "claude.ai"}
        )
    ).json()
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    query = {
        "response_type": "code",
        "client_id": registered["client_id"],
        "redirect_uri": CALLBACK,
        "code_challenge": challenge.decode(),
        "code_challenge_method": "S256",
        "state": "s",
    }
    waiting = await client.get("/authorize", params=query, allow_redirects=False)
    assert waiting.status == 200
    owner = comms.messaging.user_identity(str(comms.root))
    notice = comms.bus.inbox(owner.name)[-1]
    link = urlsplit(notice.body.splitlines()[-1])
    redirected = await client.get(link.path, allow_redirects=False)
    assert redirected.status == 302
    cookie = redirected.cookies[TrustedBrowser.cookie_name]
    assert cookie["secure"] and cookie["httponly"] and cookie["samesite"] == "Lax"
    assert cookie["path"] == "/" and not cookie["domain"]
    assert redirected.headers["Cache-Control"] == "no-store"
    code = parse_qs(urlsplit(redirected.headers["Location"]).query)["code"][0]
    issued = await client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "client_id": registered["client_id"],
            "redirect_uri": CALLBACK,
        },
    )
    assert issued.status == 200
    return (await issued.json())["access_token"]


async def test_a_request_without_an_approved_token_is_refused(client, store):
    for headers in ({}, {"Authorization": "Bearer guessed"}):
        refused = await client.post("/mcp", json=LIST, headers=headers)
        assert refused.status == 401
        assert "oauth-protected-resource" in refused.headers["WWW-Authenticate"]
    assert (await client.get("/.well-known/oauth-authorization-server")).status == 200


async def test_a_refused_call_answers_its_own_request_id(client, store):
    """A client waits forever on a call whose refusal carries a null id."""
    token = await approved_token(client, store)
    call = {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": "comms_channels", "arguments": {}, "undeclared": {}}}
    refused = await (await client.post(
        "/mcp", json=call, headers={"Authorization": f"Bearer {token}"})).json()
    assert refused["id"] == 7
    assert "undeclared" in refused["error"]["message"]


async def test_request_log_names_the_route_and_never_its_values(client, store, root, caplog):
    """Served as run_app serves it, with aiohttp's raw access log off."""
    runner = web.AppRunner(Connector(store, "http://127.0.0.1:18766", root).application(),
                           access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        with caplog.at_level(logging.INFO):
            async with ClientSession() as session:
                await session.get(f"http://127.0.0.1:{runner.addresses[0][1]}/authorize",
                                  params={"state": "one-use-value"})
    finally:
        await runner.cleanup()
    lines = [record.getMessage() for record in caplog.records]
    assert any("/authorize" in line and " 400 " in line for line in lines), lines
    assert all("one-use-value" not in line for line in lines), lines


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


async def test_connector_channel_history_on_a_copy_leaves_unread_unchanged(client, comms, tmp_path):
    from agent_comms.channel_targets import CommandTarget

    comms.channels.create_tag("comms")
    for name in ("worker", "claude-ai"):
        comms.channels.update_tags(name, add=frozenset({"comms"}))
    originals = [comms.messaging.send(
        "worker", "#comms", f"@claude-ai channel post {number}").message
        for number in range(55)]
    copied_root = tmp_path / "copied-wire"
    shutil.copytree(comms.root, copied_root)
    copied = Comms(copied_root)
    copied_store = ConnectorStore.at(tmp_path / "copied-auth")
    connector = Connector(copied_store, "http://127.0.0.1:18766", copied_root)
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
    comms, tmp_path, phases, monkeypatch,
):
    for name in ("worker", "claude-ai"):
        comms.registry.declare(comms.registry.new_thread(name, frozenset(), str(comms.root)))
    original = comms.messaging.send("worker", "claude-ai", "Ready for your message").message
    copied_root = tmp_path / ".agent-comms"
    shutil.copytree(comms.root, copied_root)
    monkeypatch.setenv("HOME", str(tmp_path))
    store = ConnectorStore.at(tmp_path / "copy-auth")
    connector = Connector(store, "http://127.0.0.1:18766", copied_root)
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
