"""Actual ACP/owner/Pi journey with an explicit loopback provider rejection."""
import argparse
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import sys
import time
import hashlib

from acp import spawn_agent_process
from acp.exceptions import RequestError
from agent_comms.native_fork import ForkSessionHelper, ForkSessionRequest
from contextlib import nullcontext
from uuid import uuid4
import threading

from acp.schema import TextContentBlock
from agent_comms.acp import CommsClient
from agent_comms.acp_extension import RequestFailedUpdate, PromptCancelledUpdate, decode_updates
from agent_comms.comms import Comms
from agent_comms.native_package import verify_native_package
from agent_comms.threads import Thread


class ReceiptSubscriber:
    def __init__(self):
        self.failures = []
        self.cancellations = []

    async def session_update(self, **kwargs):
        for update in decode_updates(kwargs["update"].model_dump(by_alias=True, exclude_none=True).get("_meta")):
            if isinstance(update, RequestFailedUpdate):
                self.failures.append(update.failure.feedback)
            if isinstance(update, PromptCancelledUpdate):
                self.cancellations.append(update.input_state)


async def main(package: Path, evidence: Path, source: Path, *, cancel_only=False):
    verify_native_package(package)
    evidence.mkdir(parents=True, exist_ok=False)
    requests, server_errors = [], []
    context_limit, input_count = 1048576, 432636
    accepted = threading.Event()
    release = threading.Event()

    class Provider(BaseHTTPRequestHandler):
        def do_POST(self):
            try:
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                payload = json.loads(raw)
                allowance = payload.get("max_tokens")
                assert payload["model"] == "z-ai/glm-5.3-flash"
                assert self.headers["Authorization"] == "Bearer offline-only-fixture"
                requests.append({"allowance": allowance, "bytes": len(raw),
                                 "body_sha256": hashlib.sha256(raw).hexdigest(),
                                 "messages": payload["messages"], "tools": payload.get("tools"),
                                 "monotonic": time.monotonic()})
                number = 5 if cancel_only else len(requests)
                assert len(requests) <= (1 if cancel_only else 5), "Unexpected replay or provider call"
                if number in (1, 2, 4, 5):
                    assert allowance is None, "Capability was manufactured into generation intent"
                if number == 2:
                    completion = 643482  # Original endpoint's reported implicit allowance.
                    message = (f"This endpoint's maximum context length is {context_limit} tokens. "
                               f"However, you requested about {input_count + completion} tokens "
                               f"(427363 of text input, 5273 of tool input, {completion} in the output)")
                    body = json.dumps({"error": {"message": message, "code": 400}}).encode()
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                elif number == 4:
                    body = json.dumps({"error": {"message": "Unsupported fixture parameter.", "code": "invalid_request_error"}}).encode()
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                else:
                    if number == 3:
                        assert allowance > 0 and allowance + input_count <= context_limit
                        assert payload["messages"] == requests[1]["messages"]
                        assert payload.get("tools") == requests[1]["tools"]
                    chunks = [
                        {"id": "budget", "object": "chat.completion.chunk", "created": 1, "model": "z-ai/glm-5.3-flash",
                         "choices": [{"index": 0, "delta": {"role": "assistant", "content": "BUDGET_NATIVE_RECOVERY_OK"}, "finish_reason": None}]},
                        {"id": "budget", "object": "chat.completion.chunk", "created": 1, "model": "z-ai/glm-5.3-flash",
                         "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                         "usage": {"prompt_tokens": input_count, "completion_tokens": 10, "total_tokens": input_count + 10}},
                    ]
                    body = ("".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n").encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    if number == 5:
                        self.end_headers()
                        self.wfile.write(("data: "+json.dumps(chunks[0])+"\n\n").encode())
                        self.wfile.flush()
                        accepted.set()
                        release.wait(15)
                        return
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                assert len(requests) == 5, "Only an accepted cancellation closes the provider socket"
            except Exception as error:
                server_errors.append(repr(error))
                print("OWNED_PROVIDER_FIXTURE_ERROR", repr(error), "requests", len(requests), flush=True)
                self.send_error(400, "Owned budget fixture failed")

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        bus = Path("/home/ts/wt") / ("ac453-" + uuid4().hex[:8])
        bus.mkdir(mode=0o700)
        with nullcontext(bus):
            project = evidence / "project"
            project.mkdir()
            config = evidence / "pi"
            config.mkdir(mode=0o700)
            fork_environment = {key: value for key, value in os.environ.items()
                                if not key.startswith(("PI_", "AGENT_COMMS_")) and key != "PYTHONPATH"}
            fork = await ForkSessionHelper.run(ForkSessionRequest(str(package), str(source), str(project), str(config / 'sessions')),
                                               cwd=project, env=fork_environment)
            retained = Path(fork.session_file)
            before = retained.read_bytes()
            assert len(before) >= 40_000_000, "Requires original representative saved history"
            source_stat = source.stat()
            source_digest = hashlib.sha256(source.read_bytes()).hexdigest()
            (config / "models.json").write_text(json.dumps({"providers": {"openrouter": {
                "baseUrl": f"http://127.0.0.1:{server.server_port}/v1", "api": "openai-completions",
                "models": [{"id": "z-ai/glm-5.3-flash", "contextWindow": 1048575,
                            "maxTokens": 943717, "compat": {"maxTokensField": "max_tokens"}}],
            }}}))
            (config / "auth.json").write_text(json.dumps({"openrouter": {"type": "api_key", "key": "offline-only-fixture"}}))
            comms = Comms(Path(bus) / "wire")
            root_id = comms.messaging.initialize_private_initial_protocol()
            comms.owners.pin_private_nk_launch(comms.root, root_id, package)
            comms.registry.declare(Thread("budget-native", frozenset(), str(project), session_file=str(retained),
                                          model="openrouter/z-ai/glm-5.3-flash", thinking_level="low"))
            environment = dict(fork_environment, PI_CODING_AGENT_DIR=str(config),
                               AGENT_COMMS_AGENT_BIN="pi", AGENT_COMMS_AGENT_ARGS="--offline --no-extensions --no-skills --no-context-files",
                               AGENT_COMMS_AGENT_MODELS="openrouter/z-ai/glm-5.3-flash",
                               AGENT_COMMS_ROOT=str(comms.root), AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID=root_id,
                               AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE=str(package),
                               XDG_CONFIG_HOME=str(evidence / "config"), XDG_DATA_HOME=str(evidence / "data"),
                               XDG_STATE_HOME=str(evidence / "state"))
            class StdioSubscriber(ReceiptSubscriber):
                def on_connect(self, connection):
                    pass
                async def request_permission(self, **kwargs):
                    raise AssertionError("No permission belongs to the controlled provider journey")
            subscriber = StdioSubscriber()
            try:
                async with spawn_agent_process(subscriber, sys.executable, "-m", "agent_comms.acp",
                                               env=environment, cwd=project) as (client, process):
                    await client.initialize(protocol_version=1, client_capabilities={})
                    await client.load_session(cwd=str(project), session_id="budget-native", mcp_servers=[])
                    for text in (() if cancel_only else ("NEW_PRIVATE_DESIRED_ABSENCE", "NEW_PRIVATE_SECONDARY_REJECTION")):
                        async with asyncio.timeout(45):
                            await client.prompt(session_id="budget-native", prompt=[TextContentBlock(type="text", text=text)])
                    if not cancel_only:
                        assert len(requests) == 3 and not subscriber.failures, (len(requests), subscriber.failures)
                        try:
                            async with asyncio.timeout(30):
                                await client.prompt(session_id="budget-native", prompt=[TextContentBlock(type="text", text="NEW_PRIVATE_UNRELATED_REJECTION")])
                        except RequestError as error:
                            assert "Unsupported fixture parameter" in str(error.data)
                        else:
                            raise AssertionError("Unrelated rejection did not preserve RequestError")
                        assert len(requests) == 4 and len(subscriber.failures) == 1
                        assert "Unsupported fixture parameter" in subscriber.failures[0]
                    cancellation = asyncio.create_task(client.prompt(session_id="budget-native",
                        prompt=[TextContentBlock(type="text", text="NEW_PRIVATE_ACCEPTED_CANCEL")]))
                    async with asyncio.timeout(30):
                        while not accepted.is_set():
                            await asyncio.sleep(.025)
                        await client.cancel(session_id="budget-native")
                        cancel_result = await cancellation
                        release.set()
                        assert len(subscriber.cancellations) == 1, "Original canonical cancellation was not observed"
                    assert len(requests) == (1 if cancel_only else 5), "Accepted stream/cancel was replayed"
                assert not server_errors, server_errors
                owner = comms.registry.require("budget-native")
                native = Path(owner.session_file)
                records = [json.loads(line) for line in native.read_text().splitlines()]
                before_records = [json.loads(line) for line in before.splitlines()]
                before_ids = {row["id"] for row in before_records if "id" in row}
                new = [row for row in records if row.get("id") not in before_ids]
                users = [row for row in new if row.get("type") == "message" and row.get("message", {}).get("role") == "user"]
                assert len(users) == (1 if cancel_only else 4), "A new private input was replayed"
                terminal = [row["message"] for row in new if row.get("type") == "message" and row.get("message", {}).get("role") == "assistant"]
                # Core cancellation retires the scoped native child. It can settle before
                # native persistence of an assistant message; do not invent such a message.
                assert len(subscriber.cancellations) == 1
                assert not any(row.get("type") == "compaction" for row in new)
                assert owner.active_turn is None, "Canonical lease did not settle"
                assert source.stat().st_size == source_stat.st_size
                assert hashlib.sha256(source.read_bytes()).hexdigest() == source_digest
                report = {"requests": [{key: value for key, value in request.items() if key not in ("messages", "tools")} for request in requests],
                          "source": str(source), "source_bytes": source_stat.st_size, "source_sha256": source_digest,
                          "fork": str(retained), "private_root": str(comms.root), "new_native_users": len(users), "new_compactions": 0,
                          "desired_absence": True, "secondary_same_payload_recovery": not cancel_only,
                          "native_cancel_update": str(subscriber.cancellations[0]),
                          "acp_cancel_stop_reason": cancel_result.stop_reason,
                          "native_cancel_terminal_assistant": terminal[-1]["stopReason"] if terminal else None,
                          "unrelated400_calls": 0 if cancel_only else 1, "accepted_cancel_calls": 1,
                          "production_stdio_acp": True, "public_replays": 0, "paid_calls": 0}
                (evidence / "receipt.json").write_text(json.dumps(report, indent=2))
                print("ACTUAL_RETAINED_NATIVE_STDIO_ACP_BUDGET_JOURNEY_PASS", report, flush=True)
            finally:
                release.set()
                await asyncio.to_thread(comms.owners.stop, "budget-native")
                assert not comms.registry.require("budget-native").process_alive
                diagnostics = comms.root / "diagnostics"
                if diagnostics.is_dir():
                    shutil.copytree(diagnostics, evidence / "diagnostics")
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--cancel-only", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.package, args.evidence, args.source, cancel_only=args.cancel_only))
