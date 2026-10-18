"""The claude.ai connector: OAuth in front of an MCP transport over the ToolRequest family."""

import argparse
import base64
import hashlib
import secrets
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from aiohttp.test_utils import TestClient, TestServer

from agent_comms.comms import Comms
from agent_comms.connector import (
    CALLBACK,
    AuthorizationQuery,
    Connector,
    ConnectorStore,
    approve,
    revoke,
)
from agent_comms.tool_caller import NoThreadCaller
from agent_comms.tools import invoke_tool

LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


@pytest.fixture
def store(tmp_path: Path) -> ConnectorStore:
    return ConnectorStore.at(tmp_path)


@pytest.fixture
async def client(store: ConnectorStore, root: Path, comms: Comms):
    comms.registry.declare(
        comms.registry.new_thread("worker", frozenset(), str(root), created_at=1.0)
    )
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
    assert approve(argparse.Namespace(request=request), store) == 0
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


async def test_an_approved_client_lists_exactly_the_readable_tools(client, store):
    token = await approved_token(client, store)
    listed = await (
        await client.post("/mcp", json=LIST, headers={"Authorization": f"Bearer {token}"})
    ).json()
    assert [tool["name"] for tool in listed["result"]["tools"]] == ["comms_threads"]


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
    in_process = invoke_tool(Comms(root), "comms_threads", {}, caller=NoThreadCaller())
    assert called["result"]["structuredContent"] == in_process
    assert [thread["name"] for thread in in_process["threads"]] == ["worker"]


async def test_a_revoked_token_is_refused(client, store):
    token = await approved_token(client, store)
    assert revoke(argparse.Namespace(), store) == 0
    refused = await client.post("/mcp", json=LIST, headers={"Authorization": f"Bearer {token}"})
    assert refused.status == 401
