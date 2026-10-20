"""Image prompts survive ACP decoding, fresh Pi input and queued steering."""

from bus_history import read_history
import asyncio
import base64
import json
import os
import sys

import pytest
from acp import RequestError
from acp.schema import ImageContentBlock

import agent_comms.agent_events as ae
import agent_comms.backend as backend
from agent_comms.acp_extension import (
    InputDeliveryChangedUpdate,
    QueueChangedUpdate,
    QueuePromptRequest,
    SteerPromptRequest,
    InputStartedUpdate,
    decode_updates,
    encode_request,
)
from agent_comms.comms import wire
from agent_comms.image_inputs import MAX_IMAGE_BYTES, ImageInput, prompt_images
from agent_comms.runtime import RuntimeProxy, socket_path
from agent_comms.agent_backend import InputContent, InputId, InputRequest, WhenBusy
from delivery_owner_fixture import canonical_agent
from pi_session_turn import one_turn_events
from stored_inputs import stored_inputs

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
        pending = owner.inputs.state("project").queued[input_id]
        assert pending.text == reference and pending.images == (ImageInput(PNG, "image/png"),)
        assert stored_inputs(owner.inputs.dispositions.root).rows["acp:" + input_id].declared_name == "reserved"
        await client.prompt(
            "project",
            [{"type": "text", "text": "What is this?"}, IMAGE],
            field_meta=encode_request(QueuePromptRequest(reference, True)),
        )
        assert any(
            (
                item.text == reference and item.echo
                for item in owner.inputs.state("project").queued.values()
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


async def test_routed_image_rejected_before_bus_side_effects(tmp_path, monkeypatch):
    agent = await make_agent(tmp_path, monkeypatch)
    try:
        with pytest.raises(RequestError):
            await agent.prompt("project", [{"type": "text", "text": "#all look"}, IMAGE])
        assert not read_history(agent._comms)

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
