"""Recorded peer tests of request correlation; native acceptance is separate."""

import asyncio
import json
from dataclasses import dataclass

import pytest

from agent_comms.pi_commands import CatalogQuery, PiCommand
from agent_comms.pi_payloads import ModelsData
from agent_comms.pi_rpc import PiRpcChannel


@pytest.mark.parametrize("ending", ["matched", "refused", "eof", "malformed"])
async def test_new_command_uses_existing_correlation_and_cleans_pending(ending, monkeypatch):
    monkeypatch.setattr(PiCommand, "__registry__", dict(PiCommand.__registry__))

    @dataclass(frozen=True, kw_only=True)
    class CatalogAuditQuery(CatalogQuery):
        response_payload = ModelsData

    received, peers = [], []

    async def serve(reader, writer):
        peers.append(asyncio.current_task())
        try:
            request = json.loads(await reader.readline())
            received.append(request)
            result = {
                "type": "response",
                "command": request["type"],
                "id": request["id"],
                "success": True,
                "data": {"models": [{"provider": "local", "id": "x" * 200000}]},
            }
            for unrelated in (
                {"type": "future_catalog_event"},
                {**result, "id": "different-id", "data": {"models": []}},
                {**result, "command": "get_available_thinking_levels", "data": {"levels": []}},
            ):
                writer.write((json.dumps(unrelated) + "\n").encode())
            if ending == "matched":
                writer.write((json.dumps(result) + "\n").encode())
            elif ending == "refused":
                writer.write(
                    (
                        json.dumps(
                            {
                                **result,
                                "success": False,
                                "error": "catalog unavailable",
                                "data": None,
                            }
                        )
                        + "\n"
                    ).encode()
                )
            elif ending == "malformed":
                writer.write(b"not json\n")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    reader, writer = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
    channel = PiRpcChannel(reader)
    try:
        async with asyncio.timeout(5):
            if ending in {"eof", "malformed"}:
                with pytest.raises(EOFError if ending == "eof" else ValueError):
                    await CatalogAuditQuery().exchange(channel, writer)
            else:
                response = await CatalogAuditQuery().exchange(channel, writer)
                assert response.command is CatalogAuditQuery
                if ending == "matched":
                    assert response.data.models[0].id == "x" * 200000
                else:
                    assert response.success is False and response.error == "catalog unavailable"
        assert len(received) == 1, "No retry or duplicate write on any outcome"
        assert not channel.pending._pending
    finally:
        writer.close()
        await writer.wait_closed()
        server.close()
        await server.wait_closed()
        await asyncio.gather(*peers)
