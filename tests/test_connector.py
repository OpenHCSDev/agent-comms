"""The claude.ai connector: OAuth in front of an MCP transport over the ToolRequest family."""

import base64
import hashlib
import secrets
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from aiohttp.test_utils import TestClient, TestServer

from agent_comms.comms import Comms
from agent_comms.connector import (
    CALLER,
    CALLBACK,
    AuthorizationQuery,
    Connector,
    ApproveVerb,
    RevokeVerb,
)
from agent_comms.connector_store import ConnectorStore
from agent_comms.rule_reviews import AnsweredRuleReviewEntry, DrawnRuleReviewEntry, RuleReviews
from agent_comms.tools import ReadTool, ToolRequest, invoke_tool

LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


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
            expected = ("connector round trip was refused" if not registered
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
    async with TestClient(TestServer(connector.application())) as client:
        yield client


async def approved_token(client: TestClient, store: ConnectorStore) -> str:
    """Register as claude.ai does, have the owner approve on this machine, and redeem the code."""
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
    request = AuthorizationQuery(**query).request_id
    assert ApproveVerb(request=request).apply(store) == 0
    redirected = await client.get("/authorize", params=query, allow_redirects=False)
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
    return (await issued.json())["access_token"]


async def test_a_request_without_an_approved_token_is_refused(client, store):
    for headers in ({}, {"Authorization": "Bearer guessed"}):
        refused = await client.post("/mcp", json=LIST, headers=headers)
        assert refused.status == 401
        assert "oauth-protected-resource" in refused.headers["WWW-Authenticate"]
    assert (await client.get("/.well-known/oauth-authorization-server")).status == 200


async def test_registration_accepts_only_the_claude_callback(client):
    refused = await client.post(
        "/register", json={"redirect_uris": ["https://elsewhere.example/cb"]}
    )
    assert refused.status == 400


async def test_connector_lists_every_tool(client, store, comms):
    token = await approved_token(client, store)
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
    headers = {"Authorization": f"Bearer {await approved_token(client, store)}"}

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


async def test_connector_thread_tools_act_as_claude_ai(client, store, comms):
    headers = {"Authorization": f"Bearer {await approved_token(client, store)}"}
    reviews = RuleReviews(comms.root)
    reviews.write("claude-ai", DrawnRuleReviewEntry(("Declare each fact once",), 1))
    reviews.write("claude-ai", AnsweredRuleReviewEntry(
        1, "This tick moves rules.py:47 into one typed Core record that Core owns."))
    calls = ({"name": "comms_set_goal", "arguments": {"text": "Ship the connector"}},
             {"name": "comms_send", "arguments": {"from": "claude-ai", "to": "worker", "body": "Owner said go"}})
    for call in calls:
        reply = await (await client.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": call})).json()
        assert reply["result"]["isError"] is False, reply
    assert comms.registry.require("claude-ai").goal.text == "Ship the connector"
    assert [(message.sender, message.body) for message in comms.bus.inbox("worker")] == [
        ("claude-ai", "Owner said go")]


async def test_a_call_returns_what_the_tool_returns_in_process(client, store, root):
    token = await approved_token(client, store)
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
    assert [thread["name"] for thread in in_process["threads"]] == ["claude-ai", "worker"]


async def test_a_revoked_token_is_refused(client, store):
    token = await approved_token(client, store)
    assert RevokeVerb().apply(store) == 0
    refused = await client.post("/mcp", json=LIST, headers={"Authorization": f"Bearer {token}"})
    assert refused.status == 401
