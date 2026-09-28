"""Actual ACP stdio -> owner socket -> pinned Pi -> failing local HTTP provider."""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

from agent_comms.acp_extension import (
    QueuePromptRequest,
    RequestFailedUpdate,
    decode_updates,
    encode_request,
)
from agent_comms.comms import Comms

PACKAGE = Path(os.environ["PI_COMPACTION_TEST_PACKAGE"])


async def main():
    calls = []

    async def provider(reader, writer):
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            length = next(
                (
                    int(line.split(b":", 1)[1])
                    for line in header.split(b"\r\n")
                    if line.lower().startswith(b"content-length:")
                ),
                0,
            )
            body = await reader.readexactly(length)
            calls.append(json.loads(body))
            payload = json.dumps(
                {
                    "error": {
                        "message": "The usage limit has been reached",
                        "type": "insufficient_quota",
                    }
                }
            ).encode()
            writer.write(
                b"HTTP/1.1 402 Payment Required\r\nContent-Type: application/json\r\nContent-Length: "
                + str(len(payload)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + payload
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(provider, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    with tempfile.TemporaryDirectory(prefix="t2-native-", dir="/var/tmp") as directory:
        base = Path(directory)
        root, project, config = base / "wire", base / "project", base / "config"
        project.mkdir()
        config.mkdir()
        (config / "models.json").write_text(
            json.dumps(
                {
                    "providers": {
                        "fixture": {
                            "baseUrl": f"http://127.0.0.1:{port}/v1",
                            "api": "openai-completions",
                            "apiKey": "local-only",
                            "models": [
                                {
                                    "id": "fixture",
                                    "name": "Local-only",
                                    "contextWindow": 32768,
                                    "maxTokens": 1024,
                                }
                            ],
                        }
                    }
                }
            )
        )
        (config / "settings.json").write_text(json.dumps({"compaction": {"enabled": False}}))
        comms = Comms(root)
        marker = comms.messaging.initialize_private_initial_protocol()
        env = {
            key: value for key, value in os.environ.items() if not key.startswith("AGENT_COMMS_")
        }
        env.update(
            AGENT_COMMS_ROOT=str(root),
            AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID=marker,
            AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE=str(PACKAGE),
            PI_CODING_AGENT_DIR=str(config),
            AGENT_COMMS_NATIVE_CONFIG_DIR=str(config),
            AGENT_COMMS_AGENT_BIN="pi",
            AGENT_COMMS_AGENT_MODELS="fixture/fixture",
            AGENT_COMMS_AGENT_ARGS="--offline --no-extensions --no-skills --no-context-files --no-tools --provider fixture --model fixture",
            XDG_CONFIG_HOME=str(base / "xdg-config"),
            XDG_DATA_HOME=str(base / "xdg-data"),
            XDG_STATE_HOME=str(base / "xdg-state"),
        )
        env.pop("PYTHONPATH", None)
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "agent_comms.acp",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stderr = asyncio.create_task(process.stderr.read())
        notifications = []

        async def request(number, method, params):
            process.stdin.write(
                (
                    json.dumps({"jsonrpc": "2.0", "id": number, "method": method, "params": params})
                    + "\n"
                ).encode()
            )
            await process.stdin.drain()
            while True:
                raw = await asyncio.wait_for(process.stdout.readline(), 55)
                if not raw:
                    raise AssertionError("ACP exited: " + (await stderr).decode())
                value = json.loads(raw)
                if value.get("id") == number:
                    return value
                notifications.append(value)

        try:
            assert "result" in await request(
                1, "initialize", {"protocolVersion": 1, "clientCapabilities": {}}
            )
            response = await request(2, "session/new", {"cwd": str(project), "mcpServers": []})
            assert "result" in response, response
            session = response["result"]["sessionId"]
            response = await request(
                3,
                "session/prompt",
                {
                    "sessionId": session,
                    "prompt": [{"type": "text", "text": "local failing provider input"}],
                    "_meta": encode_request(QueuePromptRequest("local failing provider input")),
                },
            )
            Path("evidence/t2-boundary/native-error-debug.json").write_text(
                json.dumps(
                    {"response": response, "notifications": notifications, "posts": len(calls)},
                    indent=2,
                )
            )
            facts = [
                fact
                for notification in notifications
                for fact in decode_updates(
                    notification.get("params", {}).get("update", {}).get("_meta")
                )
            ]
            reports = [fact.failure for fact in facts if isinstance(fact, RequestFailedUpdate)]
            assert len(reports) == 1, reports
            failure = reports[0]
            error = response.get("error", {"code": failure.code, "message": failure.detail})
            assert "usage limit" in failure.detail.lower(), failure
            assert failure.title == "Provider usage limit reached"
            assert len(calls) == 1, "No model request retry permitted"
            facts = [
                fact
                for notification in notifications
                for fact in decode_updates(
                    notification.get("params", {}).get("update", {}).get("_meta")
                )
            ]
            assert any(
                isinstance(fact, RequestFailedUpdate)
                and "usage limit" in fact.failure.detail.lower()
                for fact in facts
            )
            from agent_comms.input_disposition import InputDispositions

            rows = InputDispositions(root / InputDispositions.filename).read().rows
            assert rows and all(row.native_id is not None for row in rows.values())
            Path(
                os.environ.get("T2_ERROR_RECEIPT", "evidence/t2-boundary/native-error-receipt.json")
            ).write_text(
                json.dumps(
                    {
                        "error": error,
                        "title": failure.title,
                        "detail": failure.detail,
                        "action": failure.action,
                        "input_disposition": failure.input_disposition,
                        "provider_posts": len(calls),
                        "notification_count": len(notifications),
                        "notifications": notifications,
                        "session_id": session,
                    },
                    indent=2,
                )
            )
            print(
                "Actual ACP stdio/owner/pinned Pi/local HTTP quota failure: readable reason, one POST, no replay"
            )
        finally:
            for thread in comms.registry.snapshot().threads.values():
                if thread.pid is not None:
                    await asyncio.to_thread(comms.owners.stop, thread.name)
            process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), 8)
            except TimeoutError:
                process.kill()
                await process.wait()
            await stderr
    server.close()
    await server.wait_closed()


if __name__ == "__main__":
    asyncio.run(main())
