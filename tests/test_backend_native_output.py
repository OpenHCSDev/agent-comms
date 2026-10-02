"""Actual Pi output ownership, including tool recovery and retained image privacy."""

import asyncio
import json
import os

from agent_comms import agent_events as events
from agent_comms.image_inputs import ImageInput
from agent_comms.native_session_prepare import NativeSessionPreparation
from agent_comms.turn_failure import TerminalFailure

pytest_plugins = ("test_backend_native_lifecycle",)


class MissingAuditEvidence(TerminalFailure):
    code = "missing_audit_evidence"
    default_text = "New declaration rejected terminal evidence."
    precedence = 55

    @classmethod
    def detected(cls, session, transport_ok):
        return transport_ok and session.task == "Exercise new terminal failure declaration"


async def test_actual_native_new_terminal_failure_refuses_success_and_retention(native_backend):
    owner = native_backend
    result = await owner.run("Exercise new terminal failure declaration")
    assert not result[-1].ok and result[-1].reason_code == MissingAuditEvidence.code
    assert result[-1].text == MissingAuditEvidence.default_text
    assert len(owner.starts) == len(owner.saved_inputs()) == owner.provider.posts == 1
    assert not owner.persistent.available
    assert all(not child.alive() for child in owner.children)


async def test_actual_native_cold_preparation_retains_without_admitting_input(native_backend):
    owner = native_backend
    state = await NativeSessionPreparation.open(
        owner.persistent,
        "pi",
        [
            "--provider",
            "response-local",
            "--model",
            "fixture",
            "--offline",
            "--no-extensions",
            "--no-tools",
        ],
        worktree=str(owner.project),
        environment=dict(os.environ),
        session_file=str(owner.session),
    )
    assert state.session_file == str(owner.session) and state.session_id
    assert state.is_streaming is False and state.is_compacting is False
    assert owner.persistent.available and owner.persistent.custody.child.proc.alive()
    assert len(owner.starts) == len(owner.saved_inputs()) == owner.provider.posts == 0


async def test_actual_native_tool_failure_cannot_replace_turn_output(native_backend, monkeypatch):
    owner = native_backend
    ordinary = owner.provider.handle

    async def tool_then_answer(reader, writer):
        if owner.provider.posts:
            await ordinary(reader, writer)
            return
        try:
            header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)
            length = next(
                int(line.split(b":", 1)[1])
                for line in header.split(b"\r\n")
                if line.lower().startswith(b"content-length:")
            )
            await reader.readexactly(length)
            owner.provider.posts += 1
            chunk = {
                "id": "chatcmpl-tool",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "fixture",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "unavailable-tool",
                                    "type": "function",
                                    "function": {"name": "unavailable_tool", "arguments": "{}"},
                                }
                            ]
                        },
                    }
                ],
            }
            body = b"data: " + json.dumps(chunk).encode() + b"\n\ndata: [DONE]\n\n"
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    monkeypatch.setattr(owner.provider, "handle", tool_then_answer)
    result = await owner.run("Recover from one unavailable tool and answer")
    ends = [event for event in result if isinstance(event, events.ToolEnd)]
    assert len(ends) == 1 and not ends[0].ok, result
    assert result[-1].ok and result[-1].text == owner.provider.text, result[-1]
    assert len(owner.starts) == len(owner.saved_inputs()) == 1
    assert owner.provider.posts == 2
    print("native_tool_output", repr(result), flush=True)


async def test_actual_native_retained_image_failure_redacts_provider_text(native_backend):
    owner = native_backend
    model_file = owner.config / "models.json"
    config = json.loads(model_file.read_text())
    config["providers"]["response-local"]["models"][0]["input"] = ["text", "image"]
    model_file.write_text(json.dumps(config))
    image = ImageInput(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jZ1kAAAAASUVORK5CYII=",
        "image/png",
    )
    first = await owner.run("Inspect this diagnostic image", images=(image,))
    assert first[-1].ok, first[-1]
    retained = owner.persistent.custody.child.proc
    assert retained is not None and owner.persistent.custody.child.sensitive_diagnostics
    owner.provider.status = 503
    second = await owner.run("A new diagnostic input after the image")
    assert not second[-1].ok
    assert second[-1].text == "Image prompt failed; backend diagnostics withheld."
    assert all(
        event.text == second[-1].text and not event.diagnostics
        for event in second
        if isinstance(event, events.Error)
    )
    assert "loopback retryable failure" not in repr(second)
    assert len(owner.starts) == len(owner.saved_inputs()) == owner.provider.posts == 2
    assert len(owner.children) == 1 and not retained.alive()
    print("native_image_failure", repr(second), flush=True)
