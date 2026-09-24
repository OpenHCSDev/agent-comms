"""Image prompts survive ACP decoding, fresh Pi input and queued steering."""

import asyncio
import base64
import json
import os
import sys

import pytest
from acp import RequestError
from acp.schema import ImageContentBlock

from agent_comms import backend, wire
from agent_comms.acp import CommsAgent
from agent_comms.image_inputs import MAX_IMAGE_BYTES, ImageInput, prompt_images
from agent_comms.runtime import RuntimeProxy, socket_path

PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aDqkAAAAASUVORK5CYII="
IMAGE = {"type": "image", "data": PNG, "mimeType": "image/png"}


def test_standard_acp_image_and_legacy_resource_are_decoded():
    assert prompt_images([ImageContentBlock.model_validate(IMAGE)]) == (
        ImageInput(PNG, "image/png"),
    )
    assert prompt_images(
        [
            {
                "type": "resource",
                "resource": {
                    "uri": "file:///image.png",
                    "blob": PNG,
                    "mimeType": "image/png",
                },
            }
        ]
    ) == (ImageInput(PNG, "image/png"),)


@pytest.mark.parametrize(
    "blocks",
    [
        [{**IMAGE, "data": "not base64!"}],
        [{**IMAGE, "data": ""}],
        [{**IMAGE, "mimeType": "image/svg+xml"}],
        [IMAGE] * 9,
    ],
)
def test_invalid_images_fail_instead_of_being_dropped(blocks):
    with pytest.raises(ValueError):
        prompt_images(blocks)


def test_combined_image_budget():
    data = base64.b64encode(b"x" * (MAX_IMAGE_BYTES // 2 + 1)).decode()
    with pytest.raises(ValueError, match="4 MiB"):
        prompt_images([{**IMAGE, "data": data}] * 2)


async def make_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/model")
    agent = CommsAgent(
        wire(tmp_path / "wire"), agent_bin="pi-image-stub", agent_args=["--model", "test/model"]
    )
    await agent.new_session(cwd=str(tmp_path / "project"), mcp_servers=[])
    return agent


@pytest.mark.parametrize("text", ["What is in this image?", ""])
async def test_initial_and_image_only_prompts_reach_backend(tmp_path, monkeypatch, text):
    agent = await make_agent(tmp_path, monkeypatch)
    received = []

    async def stream(*args, **kwargs):
        received.extend(kwargs["images"])
        yield {"type": "done", "ok": True, "text": "Image seen"}

    monkeypatch.setattr(backend, "stream_agent_events", stream)
    try:
        await agent.prompt("project", [{"type": "text", "text": text}, IMAGE])
        assert received == [ImageInput(PNG, "image/png")]
        assert agent._session_metadata("project")["agentComms"]["imagePrompts"] is True
    finally:
        await agent.shutdown()


async def test_image_steering_and_queue_restoration_keep_attachment_reference(
    tmp_path, monkeypatch
):
    agent = await make_agent(tmp_path, monkeypatch)
    started = asyncio.Event()
    received = asyncio.Event()
    commands, updates = [], []

    class Client:
        async def session_update(self, **kwargs):
            updates.append(kwargs["update"].model_dump(by_alias=True, exclude_none=True))

    agent.on_connect(Client())

    async def stream(*args, **kwargs):
        started.set()
        commands.append(await kwargs["steering_queue"].get())
        received.set()
        await asyncio.Event().wait()
        yield {"type": "done", "ok": True}

    monkeypatch.setattr(backend, "stream_agent_events", stream)
    turn = asyncio.create_task(agent.prompt("project", [{"type": "text", "text": "original"}]))
    try:
        await started.wait()
        reference = "What is this? @/private/clipboard-image.png"
        await agent.prompt(
            "project",
            [{"type": "text", "text": "What is this?"}, IMAGE],
            field_meta={"agentComms": {"userText": reference, "deferDisplay": True}},
        )
        await asyncio.wait_for(received.wait(), 3)
        assert commands[0]["images"] == [IMAGE]
        assert commands[0]["streamingBehavior"] == "steer"
        await agent.cancel("project")
        await turn
        assert any(
            update.get("_meta", {}).get("agentComms", {}).get("restored") == [reference]
            for update in updates
        )
    finally:
        await agent.shutdown()


@pytest.mark.skipif(os.name == "nt", reason="Unix socket owner attachment")
async def test_busy_proxy_image_keeps_delivery_and_attachment_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/model")
    comms = wire(tmp_path / "wire")
    owner = CommsAgent(
        comms,
        agent_bin="pi-image-stub",
        agent_args=["--model", "test/model"],
        runtime_enabled=True,
    )
    client = CommsAgent(comms)
    await owner.new_session(cwd=str(tmp_path / "project"), mcp_servers=[])
    started = asyncio.Event()

    async def stream(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()
        yield {"type": "done", "ok": True}

    monkeypatch.setattr(backend, "stream_agent_events", stream)
    proxy = RuntimeProxy(client, "project", socket_path(comms.root, os.getpid()))
    try:
        await proxy.subscribe()
        client._proxies["project"] = proxy
        client._proxy_image_support["project"] = True
        turn = asyncio.create_task(owner.prompt("project", [{"type": "text", "text": "first"}]))
        await asyncio.wait_for(started.wait(), 3)
        reference = "What is this? @/private/clipboard-image.png"
        result = await client.prompt(
            "project",
            [{"type": "text", "text": "What is this?"}, IMAGE],
            field_meta={
                "agentComms": {
                    "userText": reference,
                    "deferDisplay": True,
                    "delivery": "steer",
                }
            },
        )
        assert result.field_meta["agentComms"]["inputDisposition"]["delivery"] == "steer"
        assert not owner._queued_inputs.get("project")
        await client.prompt(
            "project",
            [{"type": "text", "text": "What is this?"}, IMAGE],
            field_meta={"agentComms": {"userText": reference, "deferDisplay": True}},
        )
        assert any(
            item.text == reference and item.echo
            for item in owner._queued_inputs["project"].values()
        )
        await owner.cancel("project")
        await turn
    finally:
        await proxy.close()
        client._proxies.clear()
        await client.shutdown()
        await owner.shutdown()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX executable fixture")
async def test_failed_image_child_never_exposes_encoded_attachment(tmp_path, monkeypatch):
    stub = tmp_path / "pi-image-stub"
    stub.write_text(f"#!{sys.executable}\n" + """
import json, sys
for line in sys.stdin:
    command = json.loads(line)
    if command['type'] == 'get_state':
        print(json.dumps({'type': 'response', 'id': command['id'],
            'command': 'get_state', 'success': True,
            'data': {'nativeInputProofCapability': 'pi-native-input-v1-live-only'}}),
            flush=True)
    elif command['type'] == 'prompt':
        sys.stderr.write(json.dumps(command))
        sys.exit(7)
""")
    stub.chmod(0o755)
    events = [
        event
        async for event in backend.stream_agent_events(
            str(stub),
            ["--model", "test/model"],
            "inspect",
            str(tmp_path),
            images=(ImageInput(PNG, "image/png"),),
        )
    ]
    assert events[-1]["ok"] is False
    assert PNG not in json.dumps(events)

    agent = await make_agent(tmp_path / "acp", monkeypatch)
    agent._agent_bin = str(stub)
    updates = []

    class Client:
        async def session_update(self, **kwargs):
            updates.append(kwargs["update"].model_dump(by_alias=True, exclude_none=True))

    agent.on_connect(Client())
    try:
        await agent.prompt("project", [{"type": "text", "text": "inspect"}, IMAGE])
        assert PNG not in json.dumps(updates)
    finally:
        await agent.shutdown()


async def test_legacy_owner_and_relay_fail_explicitly(tmp_path, monkeypatch):
    agent = await make_agent(tmp_path, monkeypatch)
    try:
        with pytest.raises(RequestError):
            await agent.prompt("project", [{"type": "text", "text": "#all look"}, IMAGE])
        assert not agent._comms.full_history()
        agent._proxies["project"] = object()
        with pytest.raises(RequestError):
            await agent.prompt("project", [IMAGE])
    finally:
        agent._proxies.clear()
        await agent.shutdown()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX executable fixture")
async def test_rpc_prompt_serializes_images_unchanged(tmp_path):
    captured = tmp_path / "prompt.json"
    stub = tmp_path / "pi-image-stub"
    stub.write_text(f"#!{sys.executable}\n" + f"""
import json, pathlib, sys
state = json.loads(sys.stdin.readline())
print(json.dumps({{'type': 'response', 'command': 'get_state', 'id': state['id'],
                  'success': True, 'data': {{'nativeInputProofCapability':
                  'pi-native-input-v1-live-only'}}}}), flush=True)
command = json.loads(sys.stdin.readline())
assert command['type'] == 'prompt'
pathlib.Path({str(captured)!r}).write_text(json.dumps(command))
print(json.dumps({{'type': 'response', 'id': command['id'],
                  'command': 'prompt', 'success': True}}), flush=True)
print(json.dumps({{'type': 'message_start', 'message': {{'role': 'user',
                  'content': command['message'], 'inputId': command['inputId']}}}}), flush=True)
print(json.dumps({{'type': 'message_end', 'message': {{'role': 'assistant',
                  'stopReason': 'stop'}}}}), flush=True)
print(json.dumps({{'type': 'agent_settled'}}), flush=True)
sys.stdin.readline()  # postturn get_state
sys.stdin.readline()  # get_session_stats
print(json.dumps({{'type': 'response', 'command': 'get_session_stats',
                  'success': True, 'data': {{}}}}), flush=True)
""")
    stub.chmod(0o755)
    events = [
        event
        async for event in backend.stream_agent_events(
            str(stub), [], "inspect", str(tmp_path), images=(ImageInput(PNG, "image/png"),)
        )
    ]
    assert events[-1]["ok"] is True
    request = json.loads(captured.read_text())
    assert {key: request[key] for key in ("type", "message", "images")} == {
        "type": "prompt",
        "message": "inspect",
        "images": [IMAGE],
    }
