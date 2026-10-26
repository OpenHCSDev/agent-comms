"""Prompt command ids, declared image wire values and their single response result."""
import asyncio
import base64
import json

import pytest

from agent_comms.agent_backend import FollowUpInputPolicy, InputContent, InputRequest, TurnInbox
from agent_comms.field_codec import FieldCodec, InputId
from agent_comms.image_input import ImageInput
from agent_comms.native_tool_call import OwnerToolConnection
from agent_comms.pi_commands import ClearQueue, FollowingPrompt, GetState, PiCommand, Prompt
from agent_comms.pi_payloads import AnonymousRequest, RequestId
from agent_comms.pi_rpc import PiRpcChannel
from agent_comms.turn_inputs import InputForwarding
from agent_comms.turn_failure import PromptSendFailed
from agent_comms.agent_events import FollowingInputRefused, InputCleared
from agent_comms.native_session_prepare import NativeSessionPreparation
from native_response_fixture import native_response_session
from agent_comms.turn_item import PiRequestKey


def prompt(**options):
    return Prompt(message="hello", input_id=InputId.mint(),
                  owner_tools=OwnerToolConnection("/unused", "a" * 64), **options)


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


@pytest.mark.parametrize(
    "identity,declared",
    [(None, AnonymousRequest()), ("opaque-id", RequestId(PiRequestKey("opaque-id")))],
)
def test_unknown_command_reads_its_id_through_the_declared_field(identity, declared):
    raw = {"type": "future_command", "id": identity, "payload": "kept"}
    command = PiCommand.from_wire(raw)
    assert command.id == declared and command.to_rpc() == raw
    raw.pop("id")
    assert PiCommand.from_wire(raw).id == AnonymousRequest()
    assert PiCommand.from_wire(raw).to_rpc() == raw
    with pytest.raises(ValueError):
        PiCommand.from_wire({**raw, "id": 3})


def test_anonymous_wire_command_keeps_its_anonymous_id():
    raw = prompt().to_rpc()
    raw.pop("id")
    decoded = PiCommand.from_wire(raw)
    assert decoded.id == AnonymousRequest()
    assert decoded.to_rpc() == raw
    assert prompt().id and prompt().id != prompt().id


@pytest.fixture
def following() -> FollowingPrompt:
    item = InputRequest(input_id=InputId.mint(), content=InputContent(text="following"),
                        when_busy=FollowUpInputPolicy)
    command = FollowingPrompt(input=item, message=item.content.text, input_id=InputId.mint(),
                              owner_tools=OwnerToolConnection("/unused", "a" * 64),
                              streaming_behavior=item.when_busy)
    return command


@pytest.mark.parametrize("success", (True, False))
async def test_following_receipt_keeps_its_command_without_a_second_acceptance_store(
    following: FollowingPrompt, success: bool,
):
    wire = following.to_rpc()
    assert wire["type"] == "prompt"
    assert wire["id"] == following.require_id()
    assert wire["inputId"] == following.input_id
    assert "input" not in wire
    channel = PiRpcChannel(asyncio.StreamReader())
    receipt = channel.track(following)
    forwarding = InputForwarding()
    forwarding.pending[following.input_id] = receipt
    reply = {"type": "response", "command": "prompt", "id": wire["id"], "success": success}
    if not success:
        reply["error"] = "explicit preflight rejection"
    response = channel.decode_record(json.dumps(reply).encode())
    assert channel.correlate(response) is following
    await channel.pending.before_settlement()
    assert await receipt.response() is response
    assert response.success.success_value is success
    assert forwarding.retire(following.input_id) is following.input
    assert forwarding.settled


@pytest.mark.parametrize("reverse", (False, True))
async def test_settlement_joins_original_and_following_receipts_not_metadata(
    following: FollowingPrompt, reverse: bool,
):
    channel = PiRpcChannel(asyncio.StreamReader())
    original = prompt()
    prompts = (following, original) if reverse else (original, following)
    for command in prompts:
        channel.track(command)
    metadata = channel.track(GetState())
    waiting = asyncio.create_task(channel.pending.before_settlement())
    try:
        for command in prompts:
            completed, _ = await asyncio.wait((waiting,), timeout=0.01)
            assert not completed
            response = channel.decode_record(json.dumps({
                "type": "response", "command": "prompt", "id": command.require_id(),
                "success": True,
            }).encode())
            assert channel.correlate(response) is command
        await asyncio.wait_for(waiting, timeout=1)
        assert not metadata.future.done()
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        channel.pending.cancel_all()


async def test_cancelling_a_receipt_waiter_does_not_cancel_the_native_receipt(following: FollowingPrompt):
    channel = PiRpcChannel(asyncio.StreamReader())
    receipt = channel.track(following)
    waiter = asyncio.create_task(receipt.response())
    await asyncio.sleep(0)
    waiter.cancel()
    await asyncio.gather(waiter, return_exceptions=True)
    assert not receipt.future.cancelled()
    channel.pending.cancel_all()


async def test_explicit_following_rejection_retires_only_its_transient_write(
    following: FollowingPrompt, native_response_session: NativeSessionPreparation,
):
    session = native_response_session
    channel = PiRpcChannel(asyncio.StreamReader())
    receipt = channel.track(following)
    session.inputs.pending[following.input_id] = receipt
    response = channel.decode_record(json.dumps({
        "type": "response", "command": "prompt", "id": following.require_id(),
        "success": False, "error": "native preflight refused",
    }).encode())
    assert channel.correlate(response) is following
    updates = tuple(following.on_response(response, session))
    assert updates
    assert session.output.failure == PromptSendFailed(
        f"Following input {following.input.input_id} was rejected: native preflight refused"
    )
    assert not session.inputs.pending
    assert session.rejected_commands == [FollowingInputRefused(id=following.input.input_id)]
    assert session.rejected_signal.is_set()
    assert await receipt.response() is response


async def test_queue_clear_reply_applies_without_a_forwarding_waiter(
    following: FollowingPrompt, native_response_session: NativeSessionPreparation,
):
    session = native_response_session
    channel = PiRpcChannel(asyncio.StreamReader())
    receipt = channel.track(following)
    session.inputs.pending[following.input_id] = receipt
    clear = ClearQueue(input_ids=(following.input_id,))
    channel.track(clear)
    response = channel.decode_record(json.dumps({
        "type": "response", "command": "clear_queue", "id": clear.require_id(),
        "success": True, "data": {"inputIds": [following.input_id]},
    }).encode())
    assert channel.correlate(response) is clear
    assert tuple(clear.on_response(response, session)) == ()
    assert not session.inputs.pending
    assert session.rejected_commands == [InputCleared(
        id=following.input.input_id, native_id=following.input_id,
        text=following.input.content.text,
    )]
    assert session.rejected_signal.is_set()
    channel.pending.cancel_all()


def test_unwritten_core_inbox_is_not_native_pending_work(following: FollowingPrompt):
    inbox = TurnInbox()
    inbox.put_nowait(following.input)
    forwarding = InputForwarding(inbox)
    assert inbox.ready
    assert not forwarding.unresolved
    assert forwarding.settled
