"""Prompt command ids, declared image wire values and their single response result."""
import base64

import pytest

from agent_comms.field_codec import FieldCodec
from agent_comms.image_inputs import ImageInput
from agent_comms.native_tool_call import OwnerToolConnection
from agent_comms.pi_commands import PiCommand, Prompt


def prompt(**options):
    return Prompt(message="hello", owner_tools=OwnerToolConnection("/unused", "a" * 64), **options)


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


@pytest.mark.parametrize("identity", [None, "opaque-id"])
def test_unknown_command_reads_its_id_through_the_declared_field(identity):
    raw = {"type": "future_command", "id": identity, "payload": "kept"}
    command = PiCommand.from_wire(raw)
    assert command.id == identity and command.to_rpc() == raw
    raw.pop("id")
    assert PiCommand.from_wire(raw).id is None
    assert PiCommand.from_wire(raw).to_rpc() == raw
    with pytest.raises(ValueError):
        PiCommand.from_wire({**raw, "id": 3})


def test_anonymous_wire_command_keeps_its_anonymous_id():
    raw = prompt().to_rpc()
    raw.pop("id")
    decoded = PiCommand.from_wire(raw)
    assert decoded.id is None
    assert decoded.to_rpc() == raw
    assert prompt().id and prompt().id != prompt().id
