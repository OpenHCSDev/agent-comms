"""Normal native CLI compact/save/reopen using only the existing loopback fixture.

Run with PYTHONPATH=src and the repository test interpreter; pass the staged
pi-coding-agent package and an owned persistent fixture directory.
"""

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys


async def main():
    package, root = (Path(arg).resolve() for arg in sys.argv[1:])
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    spec = importlib.util.spec_from_file_location(
        "manual_fixture", Path(__file__).parents[2] / "tests/test_manual_compaction.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    provider = module.LoopbackProvider(status=200)
    server = await asyncio.start_server(provider.handle, "127.0.0.1", 0)
    config = root / "config"
    config.mkdir(mode=0o700, exist_ok=True)
    port = server.sockets[0].getsockname()[1]
    (config / "models.json").write_text(json.dumps({"providers": {"fixture": {
        "baseUrl": f"http://127.0.0.1:{port}/v1", "api": "openai-completions",
        "models": [{"id": "fixture", "name": "fixture", "contextWindow": 32000, "maxTokens": 4096}],
    }}}))
    (config / "auth.json").write_text(json.dumps({"fixture": {"type": "api_key", "key": "local-fixture"}}))
    (config / "settings.json").write_text(json.dumps({
        "compaction": {"enabled": True, "reserveTokens": 1000, "keepRecentTokens": 1000},
        "retry": {"enabled": False, "maxRetries": 0},
    }))
    session = root / "source.jsonl"
    rows = [{"type": "session", "version": 3, "id": "entry-store-cli", "cwd": str(root), "timestamp": "2026-09-28T17:00:00Z"}]
    for index in range(60):
        role = "assistant" if index % 2 else "user"
        message = {"role": role, "content": [{"type": "text", "text": "data " * 1000}], "timestamp": index}
        if role == "assistant":
            message.update(provider="fixture", model="fixture", api="openai-completions", stopReason="stop", usage={
                "input": 10000, "output": 1, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 10001,
                "cost": dict.fromkeys(["input", "output", "cacheRead", "cacheWrite", "total"], 0),
            })
        rows.append({"type": "message", "id": str(index), "parentId": str(index - 1) if index else None,
                     "timestamp": "2026-09-28T17:00:00Z", "message": message})
    original = "".join(json.dumps(row) + "\n" for row in rows).encode()
    session.write_bytes(original)
    session.chmod(0o600)
    env = dict(os.environ, PI_CODING_AGENT_DIR=str(config), AGENT_COMMS_SESSION_INDEX_DIR=str(root / "indexes"), PI_OFFLINE="1")
    env.pop("AGENT_COMMS_NATIVE_CONFIG_DIR", None)
    argv = ["node", str(package / "dist/cli.js"), "--mode", "rpc", "--offline", "--no-extensions", "--no-skills",
            "--no-context-files", "--no-prompt-templates", "--no-tools", "--provider", "fixture", "--model", "fixture", "--session", str(session)]

    async def launch(name):
        stderr = (root / f"{name}.stderr").open("wb")
        process = await asyncio.create_subprocess_exec(*argv, cwd=root, env=env, stdin=asyncio.subprocess.PIPE,
                                                      stdout=asyncio.subprocess.PIPE, stderr=stderr, limit=2**20)
        return process, stderr

    async def request(process, command):
        process.stdin.write((json.dumps({"type": command, "id": command}) + "\n").encode())
        await process.stdin.drain()
        while True:
            raw = await asyncio.wait_for(process.stdout.readline(), 60)
            if not raw:
                raise AssertionError(f"Native process exited before {command}")
            item = json.loads(raw)
            if item.get("type") == "response" and item.get("id") == command:
                assert item["success"], item
                return item["data"]

    async def stop(process, stderr):
        if process.returncode is None:
            process.terminate()
        await asyncio.wait_for(process.wait(), 5)
        stderr.close()

    try:
        process, stderr = await launch("compact")
        try:
            before = await request(process, "get_state")
            assert before["messageCount"] == 60, before
            result = await request(process, "compact")
            assert "local summary" in result["summary"], result
        finally:
            await stop(process, stderr)
        saved = session.read_bytes()
        assert saved.startswith(original), "Native writer changed original history"
        appended = [json.loads(line) for line in saved[len(original):].splitlines()]
        compactions = [row for row in appended if row["type"] == "compaction"]
        assert len(compactions) == 1, [row["type"] for row in appended]
        calls = provider.posts
        assert calls > 1, "Fixture did not reach the native chunker"
        process, stderr = await launch("reopen")
        try:
            state = await request(process, "get_state")
            entries = await request(process, "get_entries")
            assert len(entries["entries"]) == 60 + len(appended), len(entries["entries"])
            assert entries["entries"][0]["id"] == "0"
            assert entries["entries"][-1]["id"] == compactions[0]["id"]
            messages = await request(process, "get_messages")
            assert any(message["role"] == "compactionSummary" for message in messages["messages"]), messages
            assert provider.posts == calls, "Read-only reopen issued a provider call"
        finally:
            await stop(process, stderr)
        print(json.dumps({"normal_cli_compact": True, "reopened": True, "initial_messages": 60,
                          "requests": calls, "original_bytes_preserved": len(original),
                          "reopened_messages": state["messageCount"], "compactions": len(compactions)}))
    finally:
        server.close()
        await server.wait_closed()


if __name__ == "__main__":
    asyncio.run(main())
