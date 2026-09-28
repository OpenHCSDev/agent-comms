"""Local HTTP provider shared by retained canonical compaction acceptance."""

import asyncio
import json


class LoopbackProvider:
    def __init__(self, *, status: int = 503):
        self.status = status
        self.port = 0
        self.posts = 0
        self.paths = []

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
                await asyncio.wait_for(reader.readexactly(length), 3)
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

                def event(text: str, reason):
                    chunk = {
                        "id": "chatcmpl-local",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "fake-compact",
                        "choices": [
                            {"index": 0, "delta": {"content": text}, "finish_reason": reason}
                        ],
                    }
                    return b"data: " + json.dumps(chunk).encode() + b"\n\n"

                body = event("local summary", None) + event("", "stop") + b"data: [DONE]\n\n"
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
