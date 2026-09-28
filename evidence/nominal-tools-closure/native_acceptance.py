"""Actual immutable Pi SDK -> installed CLI -> isolated filesystem tool calls."""

import asyncio
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agent_comms.child_process import AttachedChild
from agent_comms.comms import wire
from agent_comms.threads import Thread

TREE = Path(__file__).resolve().parents[2]
PACKAGE = TREE / "stack/.pi-native-5fdef596596173bd/node_modules/@earendil-works/pi-coding-agent"


async def main():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            index = len(requests)
            if index <= 2:
                name, args = (
                    ("comms_send", {"from": "worker", "to": "peer", "body": "NATIVE_TOOL_RECEIPT"})
                    if index == 1
                    else ("comms_inbox", {"thread": "peer", "ack": False})
                )
                delta = {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": f"call_{index}",
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)},
                        }
                    ],
                }
                finish = "tool_calls"
            else:
                delta, finish = {"content": "NATIVE_TOOLS_OK"}, "stop"
            chunk = {
                "id": f"response-{index}",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "z-ai/glm-5.3-flash",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
            }
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode())
            self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    events = []
    try:
        with tempfile.TemporaryDirectory(
            dir=TREE / ".artifacts", prefix="nominal-native-"
        ) as directory:
            root = Path(directory)
            config = root / "config"
            config.mkdir()
            origin = f"http://127.0.0.1:{server.server_port}"
            (config / "auth.json").write_text("{}")
            (config / "models.json").write_text(
                json.dumps({"providers": {"openrouter": {"baseUrl": origin + "/v1"}}})
            )
            comms = wire(root / "wire")
            for name in ("worker", "peer"):
                comms.threads.register(Thread(name, frozenset(), str(root)))
            env = {
                **os.environ,
                "AGENT_COMMS_ROOT": str(root / "wire"),
                "S1_NATIVE_PACKAGE": str(PACKAGE),
                "PI_CODING_AGENT_DIR": str(config),
                "S1_LOCAL_ORIGIN": origin,
                "S1_PYTHON": sys.executable,
                "S1_COMMS_TOOLS": "1",
                "PI_AGENT_ID": "worker",
            }
            env.pop("PYTHONPATH", None)
            child = await AttachedChild.start(
                (
                    "node",
                    str(TREE / "tests/native_event_host.mjs"),
                    "--provider",
                    "openrouter",
                    "--model",
                    "z-ai/glm-5.3-flash",
                ),
                cwd=root,
                env=env,
            )

            async def stderr():
                return await child.stderr.read()

            errors = asyncio.create_task(stderr())
            try:
                child.stdin.write(b'{"type":"prompt","message":"Exercise tools then finish"}\n')
                await child.stdin.drain()
                async with asyncio.timeout(45):
                    while True:
                        line = await child.stdout.readline()
                        if not line:
                            raise AssertionError((await errors).decode())
                        row = json.loads(line)
                        events.append(row)
                        if row.get("type") == "agent_settled":
                            break
                ends = [row for row in events if row.get("type") == "tool_execution_end"]
                assert len(ends) == 2 and all(not row.get("isError") for row in ends), ends
                assert len(requests) == 3, len(requests)
                assert [m.body for m in comms.bus.inbox("peer")] == ["NATIVE_TOOL_RECEIPT"]
                tool_messages = [m for m in requests[-1]["messages"] if m.get("role") == "tool"]
                assert (
                    json.loads(tool_messages[-1]["content"])["messages"][0]["text"]
                    == "NATIVE_TOOL_RECEIPT"
                )
                receipt = {
                    "provider": "loopback only",
                    "native_package": PACKAGE.name,
                    "installed_core": __import__("agent_comms").__file__,
                    "provider_calls": len(requests),
                    "successful_tools": [row.get("toolName") for row in ends],
                    "ack_false_preserved_inbox": True,
                }
                (TREE / "evidence/nominal-tools-closure/native-receipt.json").write_text(
                    json.dumps(receipt, indent=2) + "\n"
                )
                print(json.dumps(receipt))
            finally:
                await child.stop()
                await errors
    finally:
        server.shutdown()
        server.server_close()
        serving.join()


if __name__ == "__main__":
    asyncio.run(main())
