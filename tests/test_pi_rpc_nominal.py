"""S2 declaration extension and real stream-boundary invariants."""

from __future__ import annotations

import asyncio
import itertools
import json

import pytest

import agent_comms.pi_commands as commands
import agent_comms.pi_events as pi
import agent_comms.turn_failure as failures
from agent_comms.nominal.field_codec import FieldCodec, InputId, MalformedRecord
from agent_comms.native_session_prepare import NativeSessionPreparation
from agent_comms.native_tool_call import OwnerToolConnection
from agent_comms.pi_payloads import AnonymousRequest, ReportedText, RequestId
from agent_comms.pi_rpc import PiRpcChannel
from agent_comms.turn_item import PiRequestKey
from agent_comms.turn_phase import (
    CompactionPhase,
    Excursion,
    ModelWaitPhase,
    PromptAcceptancePhase,
    StallExempt,
    ToolRunningPhase,
    TurnPhase,
)


from native_response_fixture import native_response_session


class AuditPausePhase(StallExempt, Excursion):
    stall_reason = "audit_pause_no_progress"


class AuditPauseStart(pi.PiEvent, starts=AuditPausePhase):
    pass


class AuditPauseEnd(pi.PiEvent, ends=AuditPausePhase):
    pass


def test_new_excursion_declares_both_transitions_without_dispatch_edits():
    phase = PiRpcChannel.decode_record(b'{"type":"audit_pause_start"}\n').on(ModelWaitPhase())
    assert isinstance(phase, AuditPausePhase)
    assert TurnPhase.member_named(phase.declared_name) is type(phase)
    assert phase.declared_name in TurnPhase.names()
    assert phase.model_progress() is phase
    stalled = phase.stalled()
    assert stalled is phase
    assert (stalled.stall_reason, stalled.declared_name) == (
        "audit_pause_no_progress", "audit_pause"
    )
    assert isinstance(AuditPauseEnd().on(phase), ModelWaitPhase)
    assert issubclass(AuditPausePhase, StallExempt)
    assert AuditPausePhase in TurnPhase.members_with(StallExempt)


def test_phase_watchdogs_and_overlapping_tools_preserve_protocol_semantics():
    from agent_comms.turn_watchdog import ModelWait, UnboundedWait

    bounded, unbounded = ModelWait(), UnboundedWait()
    stalled = PromptAcceptancePhase().stalled()
    assert isinstance(stalled, PromptAcceptancePhase)
    assert (stalled.stall_reason, stalled.declared_name) == (
        "prompt_acceptance_timeout", "prompt_acceptance"
    )
    phase = pi.CompactionStart().on(ModelWaitPhase())
    assert isinstance(phase, CompactionPhase) and phase.pauses_input_clock
    assert phase.model_wait(bounded, unbounded) is unbounded
    assert CompactionPhase.model_wait is ToolRunningPhase.model_wait
    # Compaction completion exits even if a tool was already present.
    assert isinstance(pi.CompactionEnd().on(phase), ModelWaitPhase)
    phase = pi.ToolExecutionStart(tool_call_id=ReportedText("a")).on(ModelWaitPhase())
    phase = pi.ToolExecutionStart(tool_call_id=ReportedText("b")).on(phase)
    assert isinstance(phase, ToolRunningPhase)
    assert tuple(identity for identity, _ in phase.tools) == ("a", "b")
    assert phase.model_wait(bounded, unbounded) is unbounded
    assert phase.model_progress() is phase
    phase = pi.ToolExecutionEnd(tool_call_id=ReportedText("a")).on(phase)
    assert isinstance(phase, ToolRunningPhase)
    assert tuple(identity for identity, _ in phase.tools) == ("b",)
    finished = pi.ToolExecutionEnd(tool_call_id=ReportedText("b")).on(phase)
    assert isinstance(finished, ModelWaitPhase)
    assert finished.model_wait(bounded, unbounded) is bounded


@pytest.mark.parametrize(
    "left,right",
    list(
        itertools.combinations(
            [
                failures.InputIdUnavailable,
                failures.IdentityUncertain,
                failures.AuthorityChanged,
                failures.FollowupUnrecognized,
                failures.InputMissing,
                failures.FinalStopMissing,
                failures.QueuedInputMissing,
                failures.ModelRequestFailed,
            ],
            2,
        )
    ),
)
def test_failure_precedence_and_text_have_one_owner(
    left, right, native_response_session: NativeSessionPreparation,
):
    session = native_response_session
    for cls in (left, right, left):
        session.output.record_failure(cls(cls.__name__))
    expected = min((left, right), key=lambda cls: cls.declared_position())
    assert type(session.output.failure) is expected
    assert session.output.failure_text == expected.__name__
    assert session.output.failure.declared_name == expected.declared_name


def test_external_formats_are_derived_and_pin_wire_spellings():
    input_id = InputId.mint()
    assert commands.Prompt(
        id=RequestId(PiRequestKey("p")), input_id=input_id, message="hello",
        owner_tools=OwnerToolConnection("/unused", "a" * 64),
    ).to_rpc() == {
        "type": "prompt",
        "id": "p",
        "inputId": FieldCodec.encode(input_id),
        "message": "hello",
        "ownerTools": {"path": "/unused", "token": "a" * 64},
    }
    assert commands.SetModel(
        id=RequestId(PiRequestKey("s")), provider="configured", model_id="kept"
    ).to_rpc() == {
        "type": "set_model",
        "id": "s",
        "provider": "configured",
        "modelId": "kept",
    }
    assert {
        c.declared_name
        for c in commands.PiCommand.members_with(commands.MutatesSession)
        if c.__module__ == commands.PiCommand.__module__
    } == {
        "new_session",
        "switch_session",
        "fork",
        "clone",
    }


async def test_correlation_handles_out_of_order_ids_and_anonymous_commands():
    channel = PiRpcChannel(asyncio.StreamReader())
    first = commands.SetModel(id=RequestId(PiRequestKey("a")), provider="p", model_id="one")
    second = commands.SetModel(id=RequestId(PiRequestKey("b")), provider="p", model_id="two")
    channel.track(first)
    channel.track(second)

    def response(identity):
        return PiRpcChannel.decode_record(
            json.dumps({"type": "response", "command": "set_model", "id": identity,
                        "success": True}).encode()
        )

    assert channel.correlate(response("b")) is second
    assert channel.correlate(response("a")) is first
    with pytest.raises(MalformedRecord):
        channel.correlate(response("a"))
    one, two = (
        commands.InterruptSteering(id=AnonymousRequest()),
        commands.InterruptSteering(id=AnonymousRequest()),
    )
    channel.track(one)
    channel.track(two)
    reply = PiRpcChannel.decode_record(b'{"type":"response","command":"interrupt_steering","success":true}')
    assert channel.correlate(reply) is one
    assert channel.correlate(reply) is two
    channel.pending.cancel_all()


@pytest.mark.parametrize("anonymous", (False, True))
async def test_pending_response_registers_each_child_exchange_once(tmp_path, anonymous):
    import sys

    from agent_comms.child_process import BoundedRun
    from agent_comms.native_child import PiChild

    responder = """import json, sys
for line in sys.stdin:
    request = json.loads(line)
    response = {"type": "response", "command": request["type"], "success": True}
    if "id" in request:
        response["id"] = request["id"]
    print(json.dumps(response), flush=True)
"""
    async with BoundedRun.session(
        (sys.executable, "-u", "-c", responder), cwd=tmp_path, timeout=commands.CATALOG_TIMEOUT
    ) as proc:
        child = PiChild(proc, PiRpcChannel(proc.stdout))
        for index in range(2):
            request = commands.SetSteeringMode(
                id=(
                    AnonymousRequest() if anonymous
                    else RequestId(PiRequestKey(f"exchange-{index}"))
                ),
                mode="all",
            )
            response = await request.exchange(child, strict=True)
            response.success.require_success(response)


async def test_fragmented_cancelled_line_remains_intact_in_the_only_reader():
    stream = asyncio.StreamReader(limit=8)
    channel = PiRpcChannel(stream)
    stream.feed_data(b'{"type":"future_thing", "data":"' + b"x" * 50)
    pending = asyncio.create_task(channel.readline(max_bytes=256))
    for _ in range(20):
        await asyncio.sleep(0)
        if channel.chunks:
            break
    pending.cancel()
    await asyncio.gather(pending, return_exceptions=True)
    stream.feed_data(b'"}\n')
    event = PiRpcChannel.decode_record(await channel.readline(max_bytes=256), strict=True)
    assert isinstance(event, pi.UnknownPiEvent)
    assert event.payload["data"] == "x" * 50


@pytest.mark.parametrize(
    "row", [b'{"type":"response","type":"agent_settled"}\n', b"[]\n", b"{}", b"\xff\n"]
)
def test_strict_native_decoding_cannot_check_malformed_rows(row):
    with pytest.raises((ValueError, UnicodeError)):
        PiRpcChannel.decode_record(row, strict=True)


def test_private_evidence_refuses_duplicate_keys():
    from agent_comms.native_failures import NativePiUnavailable
    from agent_comms.native_pi import PrivateEvidenceRead

    with pytest.raises(NativePiUnavailable):
        PrivateEvidenceRead._decode_row(b'{"type":"session","type":"message"}\n')


async def test_channel_keeps_draining_the_child_pipe_while_core_handles_a_record():
    """Node blocks on a full stdout pipe; Core's handling of one record must not fill it."""
    source = asyncio.StreamReader()
    channel = PiRpcChannel(source)
    source.feed_data(b'{"type":"agent_start"}\n')
    assert await channel.readline() == b'{"type":"agent_start"}\n'
    source.feed_data(b"x" * 4 * 1024 * 1024 + b"\n")  # far past asyncio's pause threshold
    await asyncio.sleep(0.05)  # Core is busy with the previous record; no readline runs
    assert not source._buffer
    source.feed_eof()
    assert len(await channel.readline()) == 4 * 1024 * 1024 + 1
    assert await channel.readline() == b""
