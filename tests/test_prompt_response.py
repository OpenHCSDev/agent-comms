"""Prompt command ids, declared image wire values and their single response result."""
import asyncio
import base64
from types import SimpleNamespace

import pytest

from agent_comms.agent_events import PromptAccepted
from agent_comms.field_codec import FieldCodec
from agent_comms.image_inputs import ImageInput
from agent_comms.native_tool_call import OwnerToolConnection
from agent_comms.pi_commands import PiCommand, Prompt
from agent_comms.pi_events import Response
from agent_comms.pi_rpc import PiRpcChannel
from agent_comms.prompt_state import UnacknowledgedPrompt


def prompt(**options):
    return Prompt(message="hello", owner_tools=OwnerToolConnection("/unused", "a" * 64), **options).identified()


async def test_only_the_matching_prompt_response_yields_the_accepted_result():
    command, following = prompt(), prompt()
    assert command.id and command.id != following.id
    reader = PiRpcChannel(asyncio.StreamReader())
    reader.track(command)
    reader.track(following)
    queued = []
    session = SimpleNamespace(
        command=command,
        native=SimpleNamespace(reader=reader),
        inputs=SimpleNamespace(acknowledge=queued.append),
        prompt_state=UnacknowledgedPrompt(),
        watchdog=SimpleNamespace(progress=lambda: None),
    )
    unrelated = Response(command=Prompt, id=following.id, success=True)
    assert [event async for event in unrelated.apply(session)] == []
    assert queued == [unrelated] and not session.prompt_state.acknowledged
    response = Response(command=Prompt, id=command.id, success=True)
    assert [event async for event in response.apply(session)] == [PromptAccepted()]
    assert session.prompt_state.acknowledged
    assert [event async for event in response.apply(session)] == []


def test_images_use_the_declared_pi_tag_for_encoding_and_decoding():
    image = ImageInput(base64.b64encode(b"image").decode(), "image/png")
    command = prompt(images=(image,))
    raw = command.to_rpc()
    assert raw["images"] == [FieldCodec.encode(image)]
    assert raw["images"][0]["type"] == "image"
    assert PiCommand.from_wire(raw) == command
    for tag in ("text", None):
        malformed = {**raw, "images": [{**raw["images"][0], "type": tag}]}
        with pytest.raises(ValueError):
            PiCommand.from_wire(malformed)
    missing = {**raw, "images": [{k: v for k, v in raw["images"][0].items() if k != "type"}]}
    with pytest.raises(ValueError):
        PiCommand.from_wire(missing)


def test_anonymous_wire_command_keeps_its_anonymous_id():
    raw = prompt().to_rpc()
    raw.pop("id")
    decoded = PiCommand.from_wire(raw)
    assert decoded.id is None
    assert decoded.to_rpc() == raw
    identified = decoded.identified()
    assert identified.id and identified.identified() is identified
