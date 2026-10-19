"""Prompt command ids, declared image wire values and their single response result."""
import asyncio
import base64
import json
from types import SimpleNamespace

import pytest

from agent_comms.agent_events import PromptAccepted
from agent_comms.field_codec import FieldCodec, MalformedRecord
from agent_comms.image_inputs import ImageInput
from agent_comms.native_tool_call import OwnerToolConnection
from agent_comms.pi_commands import PiCommand, Prompt
from agent_comms.pi_events import Response
from agent_comms.pi_rpc import PiRpcChannel
from agent_comms.prompt_state import UnacknowledgedPrompt


def prompt(**options):
    return Prompt(message="hello", owner_tools=OwnerToolConnection("/unused", "a" * 64), **options)


async def test_only_the_matching_prompt_response_yields_the_accepted_result():
    command, following = prompt(), prompt()
    assert command.id and command.id != following.id
    reader = PiRpcChannel(asyncio.StreamReader())
    assert json.loads(reader.encode(command))["id"] == command.id
    reader.encode(following)
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
    with pytest.raises(MalformedRecord, match="no pending command"):
        [event async for event in response.apply(session)]


async def test_tracked_turn_inherits_the_same_correlated_prompt_application():
    from agent_comms.turn_watchdog import DEFAULT_MODEL_WAIT
    from agent_comms.tracked_turn import TrackedTurnSession

    command = prompt()
    reader = PiRpcChannel(asyncio.StreamReader())
    # A constructed, unopened turn needs no child or provider for response dispatch.
    turn = TrackedTurnSession(
        None, lambda **options: command, model=None, model_wait=DEFAULT_MODEL_WAIT,
        prompt_send_boundary=None, ingress=None, startup=None, selected_tool_mode=None,
        observe_event=None, request_observer=None, pi_session=object(),
        cursor_delivery=None, following=None,
    )
    turn.command = command
    turn.native = SimpleNamespace(reader=reader)
    turn.watchdog.launching(asyncio.get_running_loop().time, None)
    turn.watchdog.reading()
    turn.grant_prompt()
    assert json.loads(reader.encode(command))["id"] == command.id
    response = Response(command=Prompt, id=command.id, success=True)
    assert await turn.apply_native_event(response) == (PromptAccepted(),)
    assert turn.prompt_state.acknowledged
    with pytest.raises(MalformedRecord, match="no pending command"):
        await turn.apply_native_event(response)


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
