"""Local HTTP provider shared by retained canonical compaction acceptance."""

import asyncio
import json


class LoopbackProvider:
    def __init__(self, *, status: int = 503, text: str = "local summary", response_timeout: float = 15):
        self.status = status
        self.text = text
        self.port = 0
        self.posts = 0
        self.paths = []
        self.requests = []
        self.tool_call = None
        self.thinking = ""
        self.response_gate = None
        self.response_timeout = response_timeout

    def response_chunks(self):
        if self.thinking:
            yield {"reasoning_content": self.thinking}, None
            self.thinking = ""
        if self.tool_call is None:
            yield {"content": self.text}, None
            yield {}, "stop"
            return
        name, arguments = self.tool_call
        self.tool_call = None
        assert any(tool["function"]["name"] == name for tool in self.requests[-1]["tools"])
        yield {
            "tool_calls": [
                {
                    "index": 0,
                    "id": "local-tool-once",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
            ]
        }, None
        yield {}, "tool_calls"

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)
            lines = head.decode("ascii", errors="replace").split("\r\n")
            if lines[0].startswith("POST "):
                self.posts += 1
                self.paths.append(lines[0])
            length = next(
                (
                    int(line.split(":", 1)[1])
                    for line in lines[1:]
                    if line.lower().startswith("content-length:")
                ),
                0,
            )
            if length:
                body = await asyncio.wait_for(reader.readexactly(length), 3)
                self.requests.append(json.loads(body))
            if self.response_gate is not None:
                await asyncio.wait_for(self.response_gate.wait(), self.response_timeout)
            if self.status == 0:
                # This attempt stays in flight until Pi is cancelled.
                await asyncio.wait_for(reader.read(), 20)
                return
            if self.status in {400, 503}:
                body = (
                    b'{"error":{"message":"maximum context length exceeded; '
                    b'private prompt","type":"context_length_exceeded"}}'
                    if self.status == 400
                    else b'{"error":{"message":"loopback retryable failure","type":"server_error"}}'
                )
                writer.write(
                    f"HTTP/1.1 {self.status} Failure\r\n".encode()
                    + b"Content-Type: application/json\r\nContent-Length: "
                    + str(len(body)).encode()
                    + b"\r\nConnection: close\r\n\r\n"
                    + body
                )
            else:

                def event(delta, reason):
                    chunk = {
                        "id": "chatcmpl-local",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "fake-compact",
                        "choices": [
                            {"index": 0, "delta": delta, "finish_reason": reason}
                        ],
                    }
                    return b"data: " + json.dumps(chunk).encode() + b"\n\n"

                body = b"".join(
                    event(delta, reason) for delta, reason in self.response_chunks()
                ) + b"data: [DONE]\n\n"
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: "
                    + str(len(body)).encode()
                    + b"\r\nConnection: close\r\n\r\n"
                    + body
                )
            await writer.drain()
        except (OSError, TimeoutError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()
