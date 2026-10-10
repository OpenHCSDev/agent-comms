"""Image prompts survive ACP decoding, fresh Pi input and queued steering."""

import asyncio
import base64
import json
import os
import sys

import pytest
from acp import RequestError
from acp.schema import ImageContentBlock

from agent_comms import agent_events as ae
from agent_comms import backend
from agent_comms.acp_extension import (
    InputDeliveryChangedUpdate,
    QueueChangedUpdate,
    QueuePromptRequest,
    SteerPromptRequest,
    TurnStartedUpdate,
    decode_updates,
    encode_request,
)
from agent_comms.comms import wire
from agent_comms.image_inputs import MAX_IMAGE_BYTES, ImageInput, prompt_images
from agent_comms.runtime import RuntimeProxy, socket_path
from agent_comms.agent_backend import InputContent, InputId, InputRequest, WhenBusy
from delivery_owner_fixture import canonical_agent
from pi_session_turn import one_turn_events

pytestmark = pytest.mark.usefixtures("native_rpc_fixture")
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
                "resource": {"uri": "file:///image.png", "blob": PNG, "mimeType": "image/png"},
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
    agent = canonical_agent(
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
        yield ae.Done(ok=True, text="Image seen")

    monkeypatch.setattr(backend, "stream_agent_events", stream)
    try:
        await agent.prompt("project", [{"type": "text", "text": text}, IMAGE])
        assert received == [ImageInput(PNG, "image/png")]
    finally:
        await agent.shutdown()


async def test_image_steering_and_queue_restoration_keep_attachment_reference(
    tmp_path, monkeypatch
):
    agent = await make_agent(tmp_path, monkeypatch)
    started = asyncio.Event()
    received = asyncio.Event()
    commands, updates = ([], [])

    class Client:
        async def session_update(self, **kwargs):
            updates.append(kwargs["update"].model_dump(by_alias=True, exclude_none=True))

    agent.on_connect(Client())

    async def stream(*args, **kwargs):
        started.set()
        commands.append(await kwargs["steering_queue"].get())
        received.set()
        await asyncio.Event().wait()
        yield ae.Done(ok=True, text="")

    monkeypatch.setattr(backend, "stream_agent_events", stream)
    turn = asyncio.create_task(agent.prompt("project", [{"type": "text", "text": "original"}]))
    try:
        await asyncio.wait_for(started.wait(), 3)
        reference = "What is this? @/private/clipboard-image.png"
        await agent.prompt(
            "project",
            [{"type": "text", "text": "What is this?"}, IMAGE],
            field_meta=encode_request(QueuePromptRequest(reference, True)),
        )
        await asyncio.wait_for(received.wait(), 3)
        assert commands[0]["images"] == [IMAGE]
        assert commands[0]["streamingBehavior"] == "steer"
        await agent.cancel("project")
        await turn
        assert any(
            [item.text for item in fact.projection.restored] == [reference]
            for update in updates
            for fact in decode_updates(update.get("_meta"))
            if isinstance(fact, QueueChangedUpdate)
        )
    finally:
        await agent.shutdown()


@pytest.mark.skipif(os.name == "nt", reason="Unix socket owner attachment")
async def test_busy_proxy_image_keeps_delivery_and_attachment_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/model")
    comms = wire(tmp_path / "wire")
    owner = canonical_agent(
        comms, agent_bin="pi-image-stub", agent_args=["--model", "test/model"], runtime_enabled=True
    )
    client = canonical_agent(comms)
    await owner.new_session(cwd=str(tmp_path / "project"), mcp_servers=[])
    started = asyncio.Event()

    async def stream(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()
        yield ae.Done(ok=True, text="")

    monkeypatch.setattr(backend, "stream_agent_events", stream)
    proxy = RuntimeProxy(client, "project", socket_path(comms.root, os.getpid()))
    try:
        await proxy.subscribe()
        client.sessions.proxies["project"] = proxy
        turn = asyncio.create_task(owner.prompt("project", [{"type": "text", "text": "first"}]))
        await asyncio.wait_for(started.wait(), 3)
        reference = "What is this? @/private/clipboard-image.png"
        result = await client.prompt(
            "project",
            [{"type": "text", "text": "What is this?"}, IMAGE],
            field_meta=encode_request(SteerPromptRequest(reference, True)),
        )
        input_id = next(
            f.input_id
            for f in decode_updates(result.field_meta)
            if isinstance(f, InputDeliveryChangedUpdate)
        )
        pending = owner.inputs.queued_inputs["project"][input_id]
        assert pending.text == reference and pending.images == (ImageInput(PNG, "image/png"),)
        assert owner.inputs.dispositions.read().rows["acp:" + input_id].declared_name == "reserved"
        await client.prompt(
            "project",
            [{"type": "text", "text": "What is this?"}, IMAGE],
            field_meta=encode_request(QueuePromptRequest(reference, True)),
        )
        assert any(
            (
                item.text == reference and item.echo
                for item in owner.inputs.queued_inputs["project"].values()
            )
        )
        await owner.cancel("project")
        await turn
    finally:
        await proxy.close()
        client.sessions.proxies.clear()
        await client.shutdown()
        await owner.shutdown()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX executable fixture")
async def test_failed_image_child_never_exposes_encoded_attachment(tmp_path, monkeypatch):
    stub = tmp_path / "pi-image-stub"
    stub.write_text(
        f"#!{sys.executable}\n"
        + "\nimport json, sys\nfor line in sys.stdin:\n    command = json.loads(line)\n    if command['type'] == 'get_state':\n        print(json.dumps({'type': 'response', 'id': command['id'],\n            'command': 'get_state', 'success': True,\n            'data': {'nativeInputProofCapability': 'pi-native-input-v1-live-only'}}),\n            flush=True)\n    elif command['type'] == 'prompt':\n        sys.stderr.write(json.dumps(command))\n        sys.exit(7)\n"
    )
    stub.chmod(493)
    events = [
        event
        async for event in one_turn_events(
            str(stub),
            ["--model", "test/model"],
            "inspect",
            str(tmp_path),
            images=(ImageInput(PNG, "image/png"),),
        )
    ]
    assert events[-1].ok is False
    assert events[-1].diagnostic["exit_code"] == 7
    assert PNG not in repr(events)
    agent = await make_agent(tmp_path / "acp", monkeypatch)
    agent.turns.agent_bin = str(stub)
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


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX executable fixture")
async def test_failed_queued_image_child_never_exposes_encoded_attachment(tmp_path, monkeypatch):
    stub = tmp_path / "pi-image-stub"
    stub.write_text(
        f"#!{sys.executable}\n"
        + "\nimport json, sys\nfor line in sys.stdin:\n    command = json.loads(line)\n    if command['type'] == 'get_state':\n        print(json.dumps({'type': 'response', 'id': command['id'],\n            'command': 'get_state', 'success': True,\n            'data': {'nativeInputProofCapability': 'pi-native-input-v1-live-only'}}),\n            flush=True)\n    elif command['type'] == 'prompt' and 'images' not in command:\n        print(json.dumps({'type': 'response', 'id': command['id'],\n            'command': 'prompt', 'success': True}), flush=True)\n        print(json.dumps({'type': 'message_start', 'message': {'role': 'user',\n            'content': command['message'], 'inputId': command['inputId']}}), flush=True)\n    elif command['type'] == 'prompt':\n        sys.stderr.write(json.dumps(command))\n        sys.exit(7)\n"
    )
    stub.chmod(493)
    queue = asyncio.Queue()
    queue.put_nowait(InputRequest(
        input_id=InputId("follow-up"),
        content=InputContent(
            text="User follow-up:\ninspect image", images=(ImageInput(PNG, "image/png"),),
        ),
        when_busy=WhenBusy.STEER,
    ))
    events = [
        event
        async for event in one_turn_events(
            str(stub), ["--model", "test/model"], "first", str(tmp_path), steering_queue=queue
        )
    ]
    assert events[-1].ok is False
    assert events[-1].diagnostic["exit_code"] == 7
    assert PNG not in repr(events)
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/model")
    agent = canonical_agent(
        wire(tmp_path / "acp-wire"), agent_bin=str(stub), agent_args=["--model", "test/model"]
    )
    await agent.new_session(cwd=str(tmp_path / "project"), mcp_servers=[])
    started = asyncio.Event()
    updates = []

    class Client:
        async def session_update(self, **kwargs):
            update = kwargs["update"].model_dump(by_alias=True, exclude_none=True)
            updates.append(update)
            if any(isinstance(f, TurnStartedUpdate) for f in decode_updates(update.get("_meta"))):
                started.set()

    agent.on_connect(Client())
    try:
        turn = asyncio.create_task(agent.prompt("project", [{"type": "text", "text": "first"}]))
        await asyncio.wait_for(started.wait(), 3)
        await agent.prompt("project", [{"type": "text", "text": "inspect image"}, IMAGE])
        await asyncio.wait_for(turn, 3)
        assert PNG not in json.dumps(updates)
    finally:
        await agent.shutdown()


async def test_routed_image_rejected_before_bus_side_effects(tmp_path, monkeypatch):
    agent = await make_agent(tmp_path, monkeypatch)
    try:
        with pytest.raises(RequestError):
            await agent.prompt("project", [{"type": "text", "text": "#all look"}, IMAGE])
        assert not agent._comms.views.full_history()

    finally:
        agent.sessions.proxies.clear()
        await agent.shutdown()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX executable fixture")
async def test_rpc_prompt_serializes_images_unchanged(tmp_path):
    captured = tmp_path / "prompt.json"
    stub = tmp_path / "pi-image-stub"
    stub.write_text(
        f"#!{sys.executable}\n"
        + f"\nimport json, pathlib, sys\nstate = json.loads(sys.stdin.readline())\nprint(json.dumps({{'type': 'response', 'command': 'get_state', 'id': state['id'],\n                  'success': True, 'data': {{'nativeInputProofCapability':\n                  'pi-native-input-v1-live-only'}}}}), flush=True)\ncommand = json.loads(sys.stdin.readline())\nassert command['type'] == 'prompt'\npathlib.Path({str(captured)!r}).write_text(json.dumps(command))\nprint(json.dumps({{'type': 'response', 'id': command['id'],\n                  'command': 'prompt', 'success': True}}), flush=True)\nprint(json.dumps({{'type': 'message_start', 'message': {{'role': 'user',\n                  'content': command['message'], 'inputId': command['inputId']}}}}), flush=True)\nprint(json.dumps({{'type': 'message_end', 'message': {{'role': 'assistant',\n                  'stopReason': 'stop'}}}}), flush=True)\nprint(json.dumps({{'type': 'agent_settled'}}), flush=True)\nsys.stdin.readline()  # postturn get_state\nsys.stdin.readline()  # get_session_stats\nprint(json.dumps({{'type': 'response', 'command': 'get_session_stats',\n                  'success': True, 'data': {{}}}}), flush=True)\n"
    )
    stub.chmod(493)
    events = [
        event
        async for event in one_turn_events(
            str(stub), [], "inspect", str(tmp_path), images=(ImageInput(PNG, "image/png"),)
        )
    ]
    assert events[-1].ok is True
    request = json.loads(captured.read_text())
    assert {key: request[key] for key in ("type", "message", "images")} == {
        "type": "prompt",
        "message": "inspect",
        "images": [IMAGE],
    }
