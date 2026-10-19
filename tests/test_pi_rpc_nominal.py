"""S2 declaration extension and real stream-boundary invariants."""

from __future__ import annotations
from agent_comms.launch_runtime import LaunchRuntime, RestartEnvironment
from agent_comms.selected_session import SelectedSession

import asyncio
import itertools
import json
from pathlib import Path

import pytest

import agent_comms.pi_commands as commands
import agent_comms.pi_events as pi
import agent_comms.turn_failure as failures
from agent_comms.native_session_prepare import NativeSessionPreparation
from agent_comms.native_tool_call import OwnerToolConnection
from agent_comms.native_startup import NativeStartup
from agent_comms.native_pi import NativePiRpcLaunch
from agent_comms.pi_native_backend import PersistentPiSession
from agent_comms.pi_rpc import PiRpcChannel
from agent_comms.turn_phase import (
    CompactionPhase,
    Excursion,
    ModelWaitPhase,
    PromptAcceptancePhase,
    StallExempt,
    ToolRunningPhase,
    TurnPhase,
)


class AuditPausePhase(StallExempt, Excursion):
    stall_reason = "audit_pause_no_progress"


class AuditPauseStart(pi.PiEvent, starts=AuditPausePhase):
    pass


class AuditPauseEnd(pi.PiEvent, ends=AuditPausePhase):
    pass


class AuditFailure(failures.TurnFailure):
    code = "audit_failure"
    precedence = 110
    input_uncertain = True


def test_new_excursion_declares_both_transitions_without_dispatch_edits():
    phase = ModelWaitPhase().on(
        PiRpcChannel.decode_record(b'{"type":"audit_pause_start"}\n'), set()
    )
    assert isinstance(phase, AuditPausePhase)
    assert TurnPhase.member_named(phase.declared_name) is type(phase)
    assert phase.declared_name in TurnPhase.names()
    assert phase.model_progress() is phase
    stalled = phase.stalled(True)
    assert stalled is phase
    assert (stalled.stall_reason, stalled.declared_name) == (
        "audit_pause_no_progress", "audit_pause"
    )
    assert isinstance(phase.on(AuditPauseEnd(), set()), ModelWaitPhase)
    assert issubclass(AuditPausePhase, StallExempt)
    assert AuditPausePhase in TurnPhase.members_with(StallExempt)


def test_phase_watchdogs_and_overlapping_tools_preserve_protocol_semantics():
    stalled = PromptAcceptancePhase().stalled(False)
    assert isinstance(stalled, PromptAcceptancePhase)
    assert (stalled.stall_reason, stalled.declared_name) == (
        "prompt_acceptance_timeout", "prompt_acceptance"
    )
    phase = ModelWaitPhase().on(pi.CompactionStart(), {"tool"})
    assert isinstance(phase, CompactionPhase) and phase.pauses_input_clock
    # Compaction completion exits even if a tool was already present.
    assert isinstance(phase.on(pi.CompactionEnd(), {"tool"}), ModelWaitPhase)
    phase = ModelWaitPhase().on(pi.ToolExecutionStart(tool_call_id="a"), {"a"})
    phase = phase.on(pi.ToolExecutionStart(tool_call_id="b"), {"a", "b"})
    assert isinstance(phase, ToolRunningPhase)
    assert tuple(identity for identity, _ in phase.tools) == ("a", "b")
    phase = phase.on(pi.ToolExecutionEnd(tool_call_id="a"), {"b"})
    assert isinstance(phase, ToolRunningPhase)
    assert tuple(identity for identity, _ in phase.tools) == ("b",)
    assert isinstance(phase.on(pi.ToolExecutionEnd(tool_call_id="b"), set()), ModelWaitPhase)


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
                AuditFailure,
            ],
            2,
        )
    ),
)
def test_failure_precedence_text_and_uncertainty_have_one_owner(left, right):
    session = NativeSessionPreparation(
        NativePiRpcLaunch(("unused",), Path.cwd(), {}, SelectedSession(Path.cwd()), Path.cwd(), root=Path.cwd(), runtime=LaunchRuntime.running(), configuration=RestartEnvironment.inherit({})),
        native_session=PersistentPiSession(), startup=NativeStartup(Path.cwd()),
    )
    for cls in (left, right, left):
        session.output.record_failure(cls(cls.__name__))
    expected = max((left, right), key=lambda cls: cls.precedence)
    assert type(session.output.failure) is expected
    assert session.output.failure_text == expected.__name__
    assert session.output.failure.code == expected.code
    assert session.output.failure.input_uncertain == expected.input_uncertain


def test_external_formats_are_derived_and_pin_wire_spellings():
    assert commands.Prompt(id="p", input_id="n", message="hello", owner_tools=OwnerToolConnection("/unused", "a" * 64)).to_rpc() == {
        "type": "prompt",
        "id": "p",
        "inputId": "n",
        "message": "hello",
        "ownerTools": {"path": "/unused", "token": "a" * 64},
    }
    assert commands.SetModel(id="s", provider="configured", model_id="kept").to_rpc() == {
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
    first = commands.SetModel(id="a", provider="p", model_id="one")
    second = commands.SetModel(id="b", provider="p", model_id="two")
    channel.encode(first)
    channel.encode(second)

    def response(identity):
        return PiRpcChannel.decode_record(
            json.dumps({"type": "response", "command": "set_model", "id": identity}).encode()
        )

    assert channel.correlate(response("b")) is second
    assert channel.correlate(response("a")) is first
    assert channel.correlate(response("a")) is None
    one, two = commands.InterruptSteering(), commands.InterruptSteering()
    channel.encode(one)
    channel.encode(two)
    reply = PiRpcChannel.decode_record(b'{"type":"response","command":"interrupt_steering"}')
    assert channel.correlate(reply) is one
    assert channel.correlate(reply) is two
    channel.pending.cancel_all()


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


def test_turn_runner_and_duplicate_pi_decoders_are_retired():
    root = Path(__file__).parents[1] / "src/agent_comms"
    assert "def _stream_agent_events(" not in (root / "backend.py").read_text()
    for name in ["backend.py", "native_pi.py", "pi_events.py", "turn_inputs.py"]:
        text = (root / name).read_text()
        assert 'kind == "' not in text
        assert 'phase = "' not in text
    import ast

    # RPC event decoding and saved-evidence JSONL have distinct contracts.
    # The stream consumers must delegate decoding to PiRpcChannel, while the
    # saved-evidence reader may decode its own rows using shared key validation.
    for name in ("backend.py", "pi_events.py", "turn_inputs.py"):
        tree = ast.parse((root / name).read_text())
        assert not any(
            isinstance(node, ast.Call) and ast.unparse(node.func) == "json.loads"
            for node in ast.walk(tree)
        ), name
    decoders = []
    for name in ("backend.py", "native_pi.py", "pi_events.py", "turn_inputs.py", "pi_rpc.py"):
        tree = ast.parse((root / name).read_text())
        for method in ast.walk(tree):
            if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for node in ast.walk(method):
                    if isinstance(node, ast.Call) and ast.unparse(node.func) == "event_type.from_wire":
                        decoders.append((name, method.name))
    assert decoders == [("pi_rpc.py", "decode_record")]

    from agent_comms.native_pi import NativePiUnavailable, PrivateEvidenceRead

    with pytest.raises(NativePiUnavailable, match="evidence JSON is invalid"):
        PrivateEvidenceRead._decode_row(b'{"type":"session","type":"message"}\n')


def test_tracked_execution_uses_declared_handlers_without_procedural_entrypoints():
    import ast

    root = Path(__file__).parents[1] / "src/agent_comms"
    retired = {"run_native_pi_turn", "prepare_native_pi_rpc_launch"}
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                assert node.name not in retired
            elif isinstance(node, ast.ImportFrom):
                assert retired.isdisjoint(alias.name for alias in node.names)
    tree = ast.parse((root / "tracked_turn.py").read_text())
    turn = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "TrackedTurnSession"
    )
    handlers = [
        method
        for method in turn.body
        if isinstance(method, ast.AsyncFunctionDef) and method.decorator_list
    ]
    assert handlers
    for method in handlers:
        # Event identities belong to the declarations, never another if/elif dispatcher.
        assert not any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "isinstance"
            and any(
                isinstance(part, ast.Attribute)
                and isinstance(part.value, ast.Name)
                and part.value.id == "pi"
                for part in ast.walk(node)
            )
            for node in ast.walk(method)
        )


def test_queued_input_start_budget_waits_while_the_native_run_works():
    """A message queued mid-turn is not killed by the time the turn keeps working."""
    from types import SimpleNamespace

    from agent_comms.turn_watchdog import InputStartWait, ModelWait, ProgressWatchdog

    watchdog = ProgressWatchdog(model_wait=ModelWait(60.0), input_timeout=180.0, preflight_timeout=5.0)
    session = SimpleNamespace(
        awaiting_native_attestation=False,
        stats=SimpleNamespace(requested=False),
        prompt_state=SimpleNamespace(awaiting_start=True),
        active_tools=set(),
    )
    watchdog.phase = ModelWaitPhase()
    wait = watchdog.wait_for(session)
    assert isinstance(wait, InputStartWait)
    wait.elapsed = 170.0
    watchdog.now = 0.0
    watchdog.progress()
    assert wait.timeout() == 180.0
    watchdog.phase = ModelWaitPhase().on(pi.ToolExecutionStart(), {"tool"})
    session.active_tools = {"tool"}
    assert watchdog.wait_for(session).timeout() is None
