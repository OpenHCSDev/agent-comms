"""Installed native 429 exhaustion reaches one completed user-visible failure."""
import asyncio
import json
from pathlib import Path

import pytest

import agent_comms.native_pi as native_pi
from agent_comms.acp_extension import RequestFailedUpdate, decode_updates
from agent_comms.acp_failure import (
    NativeMessageFailureSource,
    ProviderQuotaFailure,
    NativeTurnError,
)
from delivery_owner_fixture import canonical_agent

RATE_LIMIT = "Too many requests for this provider"


class RateLimitedProvider:
    def __init__(self) -> None:
        self.posts = 0

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)
            lines = head.decode("ascii", errors="replace").split("\r\n")
            length = next((int(line.split(":", 1)[1]) for line in lines[1:]
                           if line.lower().startswith("content-length:")), 0)
            if length:
                await asyncio.wait_for(reader.readexactly(length), 3)
            self.posts += 1
            body = json.dumps({"error": {
                "message": RATE_LIMIT,
                "type": "rate_limit_error",
            }}).encode()
            writer.write(
                b"HTTP/1.1 429 Too Many Requests\r\nContent-Type: application/json\r\n"
                + b"Content-Length: " + str(len(body)).encode()
                + b"\r\nConnection: close\r\n\r\n" + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()


@pytest.mark.asyncio
async def test_installed_429_exhaustion_publishes_one_retry_owned_failure(
    private_comms, tmp_path, monkeypatch,
):
    provider = RateLimitedProvider()
    server = await asyncio.start_server(provider.handle, "127.0.0.1", 0)
    config = tmp_path / "config"
    config.mkdir(mode=0o700)
    (config / "models.json").write_text(json.dumps({"providers": {"limit-offline": {
        "baseUrl": f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1",
        "api": "openai-completions",
        "models": [{"id": "fixture", "name": "Local", "contextWindow": 32768,
                    "maxTokens": 2048}],
    }}}))
    (config / "auth.json").write_text(json.dumps({"limit-offline": {
        "type": "api_key", "key": "offline-only-fixture",
    }}))
    (config / "settings.json").write_text(json.dumps({
        "compaction": {"enabled": False},
        "retry": {"enabled": False, "maxRetries": 0, "provider": {"maxRetries": 0}},
    }))
    monkeypatch.setenv("AGENT_COMMS_NATIVE_CONFIG_DIR", str(config))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(config))
    monkeypatch.setenv("PI_PROVIDER", "limit-offline")
    monkeypatch.setenv("PI_MODEL", "fixture")
    monkeypatch.setenv("PI_REASONING_LEVEL", "off")

    launcher = str(Path(__file__).parents[1] / "stack/bin/pi-native")
    arguments = ["--provider", "limit-offline", "--model", "fixture", "--thinking", "off",
                 "--no-extensions", "--no-skills", "--no-context-files"]
    agent = canonical_agent(private_comms, agent_bin=launcher, agent_args=arguments, runtime_enabled=True)
    updates = []

    class Client:
        async def session_update(self, session_id, update):
            updates.append(update)

    agent.sessions.client = Client()
    try:
        async with asyncio.timeout(90):
            await agent.new_session(cwd=str(tmp_path / "project"), mcp_servers=[])
            with pytest.raises(NativeTurnError) as caught:
                await agent.turns.run_agent_turn(
                    "project", "project", "One isolated request that remains rate limited",
                    original_owner_input=True,
                )
        reports = [item for update in updates for item in decode_updates(update.field_meta)
                   if isinstance(item, RequestFailedUpdate)]
        assert len(reports) == 1
        report = reports[0]
        assert isinstance(report.failure, ProviderQuotaFailure)
        assert report.failure.title == "Provider usage limit reached"
        assert report.failure.retry_result.attempt == 3
        assert report.failure.retry_result.success is False
        assert RATE_LIMIT in report.failure.retry_result.final_error
        assert "Provider request retries: 3." in report.failure.action
        assert RATE_LIMIT in report.failure.action
        assert report.failure.next_step.startswith("Wait for the provider limit")
        assert isinstance(report.source, NativeMessageFailureSource)
        request = caught.value.request
        assert request.notification_published and request.failure == report.failure
        assert provider.posts == 4
        assert not any(getattr(getattr(update, "content", None), "text", None)
                       for update in updates)
        saved = Path(private_comms.registry.require("project").session_file)
        journal = [json.loads(line) for line in saved.read_text().splitlines()]
        users = [row for row in journal if row.get("type") == "message"
                 and row["message"]["role"] == "user"]
        refusals = [row for row in journal if row.get("type") == "message"
                    and row["message"].get("stopReason") == "error"]
        assert len(users) == 1 and len(refusals) == 4
    finally:
        await agent.shutdown()
        server.close()
        await server.wait_closed()
