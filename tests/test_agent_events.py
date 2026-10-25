"""Nominal extension, dispatch ordering, and shared settlement contracts."""

from agent_comms.pi_payloads import ReportedFlag, ReportedText, RequestId
import ast
from dataclasses import FrozenInstanceError, dataclass, replace
from pathlib import Path

import pytest

import agent_comms.agent_events as events
from agent_comms.pending_requests import PendingRequests
from metaclass_registry.mro_dispatch import (
    MroDispatch,
    handles,
)


async def test_mro_specific_before_shared_and_consumer_override():
    class Left(events.AgentEvent):
        pass

    class Right(events.AgentEvent):
        pass

    @dataclass(frozen=True)
    class Diamond(Left, Right):
        pass

    seen = []

    class Consumer(MroDispatch):
        @handles(Left)
        async def left(self, event):
            seen.append("left")

        @handles(Right)
        async def right(self, event):
            seen.append("right")

        @handles(events.AgentEvent)
        async def root(self, event):
            seen.append("root")

    class Override(Consumer):
        @handles(Left)
        async def left(self, event):
            seen.append("override")

    await Override().dispatch(Diamond())
    assert seen == ["override", "right", "root"]
    assert Diamond.__mro__.count(events.AgentEvent) == 1


def test_unannotated_c3_override_masks_handler_and_instance_binding_remains_live():
    @dataclass(frozen=True)
    class Value(events.AgentEvent):
        pass

    seen = []

    class Left(MroDispatch):
        @handles(Value)
        def masked(self, value):
            raise AssertionError("The unannotated override must suppress this handler")

    class Right(MroDispatch):
        @handles(Value)
        def masked(self, value):
            raise AssertionError("C3 must not fall through to another base declaration")

        @handles(Value, events.AgentEvent)
        def kept(self, value):
            seen.append("declared")

    class Consumer(Left, Right):
        masked = None

    first, second = Consumer(), Consumer()
    # Instance behavior still binds at invocation. A borrowed instance method
    # need not repeat the class's handler-membership declaration.
    first.kept = lambda value: seen.append("borrowed")
    value = Value()
    assert first.dispatch_sync(value) is value
    assert second.dispatch_sync(value) is value
    assert seen == ["borrowed", "borrowed", "declared", "declared"]


@pytest.mark.parametrize("synchronous", [False, True])
async def test_selected_handlers_preserve_replacements_and_identity(synchronous):
    @dataclass(frozen=True)
    class Value(events.AgentEvent):
        text: str

    selected = []
    seen = []
    borrowed = object()

    class SyncConsumer(MroDispatch):
        def handlers_for(self, value):
            selected.append(value)
            yield from super().handlers_for(value)

        @handles(Value)
        def specific(self, value, context):
            assert context is borrowed
            if value.text == "wrong":
                return events.Notice("wrong event")
            return replace(value, text="replacement")

        @handles(events.AgentEvent)
        def shared(self, value, context):
            assert context is borrowed
            seen.append(value.text)

    class AsyncConsumer(SyncConsumer):
        @handles(Value)
        async def specific(self, value, context):
            return super().specific(value, context)

        @handles(events.AgentEvent)
        async def shared(self, value, context):
            return super().shared(value, context)

    consumer = SyncConsumer() if synchronous else AsyncConsumer()
    value = Value("original")
    result = (consumer.dispatch_sync(value, borrowed) if synchronous
              else await consumer.dispatch(value, borrowed))
    assert selected == [value]
    assert seen == ["replacement"]
    assert result == Value("replacement")
    with pytest.raises(TypeError):
        if synchronous:
            consumer.dispatch_sync(Value("wrong"), borrowed)
        else:
            await consumer.dispatch(Value("wrong"), borrowed)


@pytest.mark.parametrize("synchronous", [False, True])
async def test_handler_can_consume_without_returning_a_publication(synchronous):
    @dataclass(frozen=True)
    class PrivateObservation(events.AgentEvent):
        pass

    seen = []

    class SyncConsumer(MroDispatch):
        @handles(PrivateObservation)
        def consume(self, value):
            seen.append("consumed")
            return self.DISCARD

        @handles(events.AgentEvent)
        def must_not_run(self, value):
            seen.append("published")

    class AsyncConsumer(SyncConsumer):
        @handles(PrivateObservation)
        async def consume(self, value):
            return super().consume(value)

        @handles(events.AgentEvent)
        async def must_not_run(self, value):
            return super().must_not_run(value)

    consumer = SyncConsumer() if synchronous else AsyncConsumer()
    value = PrivateObservation()
    result = (consumer.dispatch_sync(value) if synchronous else await consumer.dispatch(value))
    assert result is None
    assert seen == ["consumed"]


def test_payload_fields_and_frozen_multiple_inheritance():
    start = events.ToolStart("id", "read", "Read file", {"path": "file"})
    assert events.ToolEvent in type(start).__mro__
    for value in (start, events.OriginalInputStarted(), events.ModelChanged("id", True)):
        with pytest.raises(FrozenInstanceError):
            value.extra = "not a declared field"
    from agent_comms.turn_failure import AuthorityChanged

    done = events.Done("failed", False, AuthorityChanged("failed"), {"elapsed_ms": 3})
    assert done.reason_code == "authority_changed"
    assert done.diagnostic == {"elapsed_ms": 3}


async def test_request_correlation_separates_families_and_ignores_late_results():
    from agent_comms.pi_commands import SetModel, SetThinkingLevel
    from agent_comms.pi_events import Response

    pending = PendingRequests()
    model_command = SetModel(id="same", provider="test", model_id="one")
    thinking_command = SetThinkingLevel(id="same", level="off")
    model = pending.add(SetModel, "same", request=model_command)
    thinking = pending.add(SetThinkingLevel, "same", request=thinking_command)
    with pytest.raises(ValueError):
        pending.add(SetModel, "same", request=model_command)
    refused = Response(
        id=RequestId("same"), command=SetModel, success=ReportedFlag(False),
        error=ReportedText("unavailable"),
    )
    assert pending.take(SetModel, "same", refused) is model_command
    assert await model is refused
    assert not thinking.done()
    accepted = Response(id=RequestId("same"), command=SetThinkingLevel, success=ReportedFlag(True))
    assert pending.take(SetThinkingLevel, "same", accepted) is thinking_command
    assert await thinking is accepted
    assert pending.take(SetThinkingLevel, "same", refused) is None
    cancelled = pending.add(SetModel, "cancelled", request=SetModel(id=RequestId("cancelled")))
    cancelled.cancel()
    assert pending.take(SetModel, "cancelled", refused).id == RequestId("cancelled")


def test_internal_event_consumers_do_not_recover_string_tags():
    root = Path(__file__).parents[1] / "src" / "agent_comms"
    for filename, method in (
        ("owned_turn.py", "stream"),
        ("turn_progress.py", "consume"),
    ):
        tree = ast.parse((root / filename).read_text())
        function = next(
            n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == method
        )
        for node in ast.walk(function):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert not (
                    isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "event"
                    and node.func.attr == "get"
                )
            if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
                assert node.value.id != "event"
