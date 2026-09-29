"""Actual ACP/owner/Pi journey with an explicit loopback provider rejection."""
import argparse
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading

from acp.schema import TextContentBlock
from agent_comms.acp import CommsClient
from agent_comms.acp_extension import RequestFailedUpdate, decode_updates
from agent_comms.comms import Comms
from agent_comms.native_package import verify_native_package
from agent_comms.threads import Thread


class ReceiptSubscriber:
    def __init__(self):
        self.failures = []

    async def session_update(self, **kwargs):
        for update in decode_updates(kwargs["update"].get("_meta")):
            if isinstance(update, RequestFailedUpdate):
                self.failures.append(update.failure.feedback)


async def main(package: Path, evidence: Path):
    verify_native_package(package)
    evidence.mkdir(parents=True, exist_ok=False)
    requests, server_errors = [], []
    context_limit, input_count = 1048576, 405913  # Exact live-incident provider fixture.

    class Provider(BaseHTTPRequestHandler):
        def do_POST(self):
            try:
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                payload = json.loads(raw)
                allowance = payload["max_tokens"]
                requests.append({"allowance": allowance, "bytes": len(raw), "messages": payload["messages"]})
                if len(requests) > 2:
                    raise AssertionError("Provider request did not converge")
                if len(requests) == 1:
                    assert allowance + input_count > context_limit
                    message = (f"Requested token count exceeds the model's maximum context length of {context_limit} tokens. "
                               f"You requested a total of {input_count + allowance} tokens: {input_count} tokens from the input "
                               f"messages and {allowance} tokens for the completion. Please reduce the number of tokens.")
                    body = json.dumps({"error": {"message": "Provider returned error", "code": 400,
                                      "metadata": {"raw": json.dumps({"errors": [{"message": message}], "success": False})}}}).encode()
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                else:
                    assert allowance + input_count <= context_limit
                    assert payload["messages"] == requests[0]["messages"], "Recovery changed the original context"
                    chunks = [
                        {"id": "budget", "object": "chat.completion.chunk", "created": 1, "model": "fixture",
                         "choices": [{"index": 0, "delta": {"role": "assistant", "content": "BUDGET_NATIVE_RECOVERY_OK"}, "finish_reason": None}]},
                        {"id": "budget", "object": "chat.completion.chunk", "created": 1, "model": "fixture",
                         "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                         "usage": {"prompt_tokens": input_count, "completion_tokens": 10, "total_tokens": input_count + 10}},
                    ]
                    body = ("".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n").encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as error:
                server_errors.append(repr(error))
                print("OWNED_PROVIDER_FIXTURE_ERROR", repr(error),
                      "fields", sorted(payload), "requests", len(requests), flush=True)
                self.send_error(400, "Owned budget fixture failed")

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix="comms-budget-test-", dir="/var/tmp") as bus:
            project = evidence / "project"
            project.mkdir()
            config = evidence / "pi"
            config.mkdir(mode=0o700)
            (config / "models.json").write_text(json.dumps({"providers": {"selected-offline": {
                "baseUrl": f"http://127.0.0.1:{server.server_port}/v1", "api": "openai-completions",
                "models": [{"id": "fixture", "name": "Owned context rejection fixture", "contextWindow": context_limit,
                            "maxTokens": context_limit, "compat": {"maxTokensField": "max_tokens"}}],
            }}}))
            (config / "auth.json").write_text(json.dumps({"selected-offline": {"type": "api_key", "key": "offline-only-fixture"}}))
            os.environ.update(PI_CODING_AGENT_DIR=str(config), AGENT_COMMS_AGENT_BIN="pi",
                              AGENT_COMMS_AGENT_ARGS="--provider selected-offline --model fixture --no-extensions --no-skills --no-context-files",
                              AGENT_COMMS_AGENT_MODELS="selected-offline/fixture")
            comms = Comms(Path(bus) / "wire")
            root_id = comms.messaging.initialize_private_initial_protocol()
            os.environ.update(AGENT_COMMS_ROOT=str(comms.root),
                              AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID=root_id,
                              AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE=str(package),
                              XDG_CONFIG_HOME=str(evidence / "config"),
                              XDG_DATA_HOME=str(evidence / "data"),
                              XDG_STATE_HOME=str(evidence / "state"))
            comms.registry.declare(Thread("budget-native", frozenset(), str(project),
                                          model="selected-offline/fixture", thinking_level="off"))
            client = CommsClient(comms, runtime_enabled=True, private_nk_native_package=package, private_nk_wire_root_id=root_id)
            subscriber = ReceiptSubscriber()
            client.on_connect(subscriber)
            try:
                await client.load_session(cwd=str(project), session_id="budget-native")
                # A large actual native input reproduces the reported ~40%-full window.
                original = "BUDGET_RECOVERY_INPUT\n\n" + "Retained context remains available without forced compaction.\n" * 25650
                async with asyncio.timeout(90):
                    await client.prompt("budget-native", [TextContentBlock(type="text", text=original)])
                print("PROVIDER_ALLOWANCES", [request["allowance"] for request in requests], server_errors, flush=True)
                assert not subscriber.failures, subscriber.failures
                assert not server_errors, server_errors
                assert len(requests) == 2, len(requests)
                owner = comms.registry.require("budget-native")
                native = Path(owner.session_file)
                records = [json.loads(line) for line in native.read_text().splitlines()]
                users = [row for row in records if row.get("type") == "message" and row.get("message", {}).get("role") == "user"]
                assert len(users) == 1, "The native user input was replayed"
                assert "BUDGET_NATIVE_RECOVERY_OK" in native.read_text()
                assert not any(row.get("type") == "compaction" for row in records), "A fitting input was compacted"
                report = {"requests": [{key: value for key, value in request.items() if key != "messages"} for request in requests],
                          "native_users": len(users), "compactions": 0, "response": "BUDGET_NATIVE_RECOVERY_OK"}
                (evidence / "receipt.json").write_text(json.dumps(report, indent=2))
                print("ACTUAL_ACP_OWNER_PI_CONTEXT_REJECTION_RECOVERED_SAME_INPUT_NO_COMPACTION", report, flush=True)
            finally:
                await client.shutdown()
                await asyncio.to_thread(comms.owners.stop, "budget-native")
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
    args = parser.parse_args()
    asyncio.run(main(args.package, args.evidence))
