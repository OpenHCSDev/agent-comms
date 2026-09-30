"""Real installed SDK/stdio ACP/selected native failure; only provider is controlled.

Reuse the SDK subscriber and selected-owner setup from the installed budget
journey. The isolated root is retained on failure, including uncertain inputs.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import threading

from acp import spawn_agent_process
from acp.exceptions import RequestError
from acp.schema import TextContentBlock

import agent_comms
from agent_comms.acp_extension import RequestFailedUpdate, decode_updates
from agent_comms.acp_failure import PromptFailureReceipt, ProviderQuotaFailure
from agent_comms.comms import Comms
from agent_comms.fresh_private_session import create_fresh_private_session
from agent_comms.field_codec import FieldCodec
from agent_comms.input_disposition import InputDispositions
from agent_comms.threads import Thread

from native_context_budget_installed_journey import ReceiptSubscriber


REASON = "Controlled provider usage limit has been reached; original input not retried"


class FailureSubscriber(ReceiptSubscriber):
    def __init__(self):
        super().__init__()
        self.failure_facts = []

    async def session_update(self, **kwargs):
        await super().session_update(**kwargs)
        metadata = kwargs["update"].model_dump(by_alias=True, exclude_none=True).get("_meta")
        self.failure_facts.extend(
            update.failure for update in decode_updates(metadata)
            if isinstance(update, RequestFailedUpdate)
        )

    def on_connect(self, connection):
        pass

    async def request_permission(self, **kwargs):
        raise AssertionError("Controlled provider journey needs no external permission")


async def journey(package: Path, evidence: Path, installed: Path, *, repeat_load: bool = False):
    assert Path(agent_comms.__file__).is_relative_to(installed), "Must use noneditable installed wheel"
    assert "src" not in Path(agent_comms.__file__).parts
    evidence.mkdir(mode=0o700, parents=True, exist_ok=False)
    project, config, root = (evidence / name for name in ("project", "pi", "wire"))
    project.mkdir(mode=0o700)
    config.mkdir(mode=0o700)
    requests, provider_errors = [], []

    class Provider(BaseHTTPRequestHandler):
        def do_POST(self):
            try:
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                assert payload["model"] == "error-fixture"
                assert self.headers["Authorization"] == "Bearer offline-only-error-fixture"
                requests.append({"model": payload["model"], "messages": len(payload["messages"])})
                assert len(requests) <= 2, "Original accepted failure was replayed"
                if len(requests) == 1:
                    chunks = [
                        {"id": "history", "object": "chat.completion.chunk", "created": 1,
                         "model": "error-fixture", "choices": [{"index": 0,
                         "delta": {"role": "assistant", "content": "PRIVATE_ERROR_HISTORY_OK"},
                         "finish_reason": None}]},
                        {"id": "history", "object": "chat.completion.chunk", "created": 1,
                         "model": "error-fixture", "choices": [{"index": 0,
                         "delta": {}, "finish_reason": "stop"}],
                         "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}},
                    ]
                    body = ("".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
                            + "data: [DONE]\n\n").encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                else:
                    # Original external nested object/array/encoded JSON text,
                    # delivered through the actual provider adapter and native.
                    detail = json.dumps({"data": [None, {"details": REASON}], "message": "Internal error"})
                    body = json.dumps({"error": {"message": detail, "code": "invalid_request_error"}}).encode()
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as error:
                provider_errors.append(repr(error))
                self.send_error(400, "Controlled error fixture refused")

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    (config / "models.json").write_text(json.dumps({"providers": {"error-local": {
        "baseUrl": f"http://127.0.0.1:{server.server_port}/v1", "api": "openai-completions",
        "models": [{"id": "error-fixture", "contextWindow": 200000, "maxTokens": 8192}],
    }}}))
    (config / "auth.json").write_text(json.dumps({"error-local": {
        "type": "api_key", "key": "offline-only-error-fixture",
    }}))
    (config / "settings.json").write_text(json.dumps({
        "compaction": {"enabled": False},
        "retry": {"enabled": False, "maxRetries": 0, "provider": {"maxRetries": 0}},
    }))
    comms = Comms(root)
    root_id = comms.messaging.initialize_private_initial_protocol()
    comms.owners.pin_private_nk_launch(root, root_id, package)
    fresh = create_fresh_private_session(evidence / "sessions", worktree=project)
    comms.registry.declare(Thread("error-native", frozenset(), str(project), session_file=str(fresh.path),
                                  model="error-local/error-fixture", thinking_level="off"))
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("PI_", "AGENT_COMMS_")) and key != "PYTHONPATH"}
    environment.update(
        PYTHONPATH=str(installed), PI_CODING_AGENT_DIR=str(config),
        AGENT_COMMS_NATIVE_CONFIG_DIR=str(config), AGENT_COMMS_AGENT_BIN="pi",
        AGENT_COMMS_AGENT_ARGS="--offline --no-extensions --no-skills --no-context-files --no-tools",
        AGENT_COMMS_AGENT_MODELS="error-local/error-fixture", AGENT_COMMS_ROOT=str(root),
        AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID=root_id, AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE=str(package),
        XDG_CONFIG_HOME=str(evidence / "config"), XDG_DATA_HOME=str(evidence / "data"),
        XDG_STATE_HOME=str(evidence / "state"),
    )
    @asynccontextmanager
    async def attached(subscriber):
        async with spawn_agent_process(subscriber, sys.executable, "-m", "agent_comms.acp",
                                       env=environment, cwd=project) as (client, process):
            await client.initialize(protocol_version=1, client_capabilities={})
            await client.load_session(cwd=str(project), session_id="error-native", mcp_servers=[])
            yield client

    subscriber = FailureSubscriber()
    try:
        async with asyncio.timeout(45):
            async with attached(subscriber) as client:
                response = await client.prompt(session_id="error-native", prompt=[
                    TextContentBlock(type="text", text="One private history input")])
                assert response.stop_reason == "end_turn"
                assert len(requests) == 1 and not subscriber.failure_facts
            async with attached(subscriber) as client:
                assert len(requests) == 1, "Saved-history attach emitted another input"
                if repeat_load:
                    await client.load_session(cwd=str(project), session_id="error-native", mcp_servers=[])
                    assert len(requests) == 1, "Repeated load emitted another input"
                try:
                    await client.prompt(session_id="error-native", prompt=[
                        TextContentBlock(type="text", text="One private provider failure input")])
                except RequestError as error:
                    receipt = PromptFailureReceipt.from_error(error.code, str(error), error.data)
                else:
                    raise AssertionError("Real provider refusal did not reach SDK RequestError")
                assert receipt.notification_published
                assert isinstance(receipt.failure, ProviderQuotaFailure)
                assert REASON in receipt.failure.detail
                (evidence / "failure-boundary.json").write_text(json.dumps({
                    "receipt": FieldCodec.encode(receipt),
                    "notifications": [FieldCodec.encode(fact) for fact in subscriber.failure_facts],
                }, indent=2) + "\n")
                assert subscriber.failure_facts == [receipt.failure]
                assert len(requests) == 2 and not provider_errors
                owner = comms.registry.require("error-native")
                native = Path(owner.session_file)
                original = native.read_bytes()
                dispositions = InputDispositions(root / InputDispositions.filename).read()
            async with attached(FailureSubscriber()) as client:
                assert len(requests) == 2, "Failure/history reattachment replayed original input"
                assert native.read_bytes() == original
                assert InputDispositions(root / InputDispositions.filename).read() == dispositions
                assert comms.registry.require("error-native").active_turn is None
                users = [row["message"] for row in map(json.loads, original.splitlines())
                         if row.get("type") == "message" and row["message"].get("role") == "user"]
                assert len(users) == 2
                report = {"installed_core": str(Path(agent_comms.__file__).parent),
                          "sdk": importlib.metadata.version("agent-client-protocol"),
                          "provider_posts": 2, "native_originals": 2, "same_client_repeated_load": repeat_load,
                          "notification_published": receipt.notification_published,
                          "failure_title": receipt.failure.title,
                          "disposition": receipt.failure.input_disposition,
                          "nested_provider_reason_preserved": True,
                          "passive_failed_history_and_dispositions_unchanged": True,
                          "public_effects": 0, "paid_calls": 0, "replays": 0}
                (evidence / "receipt.json").write_text(json.dumps(report, indent=2) + "\n")
                print("ACTUAL_INSTALLED_SDK_ACP_ERROR_PAYLOAD_PASS", json.dumps(report), flush=True)
    finally:
        await asyncio.to_thread(comms.owners.stop, "error-native")
        assert not comms.registry.require("error-native").process_alive
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--installed", type=Path, required=True)
    parser.add_argument("--repeat-load", action="store_true")
    args = parser.parse_args()
    asyncio.run(journey(args.package, args.evidence, args.installed, repeat_load=args.repeat_load))
