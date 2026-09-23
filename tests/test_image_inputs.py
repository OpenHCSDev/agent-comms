"""Image prompts survive ACP decoding, fresh Pi input and queued steering."""

import asyncio
import base64
import json
import sys

import pytest
from acp import RequestError
from acp.schema import ImageContentBlock

from agent_comms import backend, wire
from agent_comms.acp import CommsAgent
from agent_comms.image_inputs import MAX_IMAGE_BYTES, ImageInput, prompt_images

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
for line in sys.stdin:
    command = json.loads(line)
    if command['type'] == 'prompt':
        pathlib.Path({str(captured)!r}).write_text(json.dumps(command))
        print(json.dumps({{'type': 'response', 'id': command.get('id'),
                          'command': 'prompt', 'success': True}}), flush=True)
        print(json.dumps({{'type': 'agent_settled'}}), flush=True)
    elif command['type'] == 'get_session_stats':
        print(json.dumps({{'type': 'response', 'command': 'get_session_stats',
                          'success': True, 'data': {{}}}}), flush=True)
        break
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
