"""Controlled Responses SSE through the real Pi Codex decoder, never a paid route."""

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def local_codex_key():
    claim = {"https://api.openai.com/auth": {"chatgpt_account_id": "offline-fixture"}}
    payload = base64.urlsafe_b64encode(json.dumps(claim).encode()).decode().rstrip("=")
    return "offline." + payload + ".offline"


class CodexLoopbackProvider:
    def __init__(
        self,
        *,
        retained=20,
        reasoning=12000,
        text="Retained summary.",
        after_chunk=None,
        response_factory=None,
    ):
        self.retained, self.reasoning, self.text = retained, reasoning, text
        self.after_chunk = after_chunk
        self.response_factory = response_factory
        self.chunk_characters = 8
        self.input_tokens = 100
        self.requests = []
        self.failures = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.handler())

    def handler(self):
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                try:
                    raw = self.rfile.read(int(self.headers["Content-Length"]))
                    if self.headers.get("Content-Encoding") == "zstd":
                        from compression import zstd

                        raw = zstd.decompress(raw)
                    request = json.loads(raw)
                    assert self.headers["Authorization"] == "Bearer " + local_codex_key()
                    assert "max_output_tokens" not in request and "maxTokens" not in request
                    provider.requests.append(request)
                    number = len(provider.requests)
                    assert number <= 16, "Native summary exceeded its bounded fixture plan"
                    text, retained, reasoning = (
                        provider.response_factory(request, number)
                        if provider.response_factory is not None
                        else (provider.text, provider.retained, provider.reasoning)
                    )
                    item = {
                        "type": "message",
                        "role": "assistant",
                        "id": f"msg_{number}",
                        "status": "in_progress",
                        "content": [],
                    }
                    events = [
                        {"type": "response.output_item.added", "output_index": 0, "item": item}
                    ]
                    events.extend(
                        {
                            "type": "response.output_text.delta",
                            "output_index": 0,
                            "content_index": 0,
                            "delta": text[i : i + provider.chunk_characters],
                        }
                        for i in range(0, len(text), provider.chunk_characters)
                    )
                    done = {
                        **item,
                        "status": "completed",
                        "content": [{"type": "output_text", "text": text, "annotations": []}],
                    }
                    events.extend(
                        [
                            {"type": "response.output_item.done", "output_index": 0, "item": done},
                            {
                                "type": "response.completed",
                                "response": {
                                    "id": f"resp_{number}",
                                    "status": "completed",
                                    "output": [done],
                                    "usage": {
                                        "input_tokens": provider.input_tokens,
                                        "output_tokens": retained + reasoning,
                                        "output_tokens_details": {"reasoning_tokens": reasoning},
                                        "total_tokens": provider.input_tokens
                                        + retained
                                        + reasoning,
                                    },
                                },
                            },
                        ]
                    )
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    for index, event in enumerate(events):
                        self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
                        self.wfile.flush()
                        if provider.after_chunk is not None:
                            provider.after_chunk(request, number, index)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # A true overrun aborts sibling map calls after joining.
                except Exception as error:
                    provider.failures.append(repr(error))
                    self.send_error(400, "Offline provider contract failure")

            def log_message(self, *_args):
                pass

        return Handler

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    def __enter__(self):
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join()
