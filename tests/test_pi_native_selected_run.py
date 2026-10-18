"""The selected native backend emits observed events once through run_turn."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_comms.agent_backend import InputContent, TurnInbox
from agent_comms.agent_events import Chunk
from agent_comms.extension_ui import NoUiRequest
from agent_comms.native_pi import NativeTurnResult
from agent_comms.pi_native_backend import PiNativeBackend
from agent_comms.selected_turn_delivery import SelectedTurnDelivery
from agent_comms.tracked_turn import TrackedDone, TrackedTurnSession


@pytest.mark.asyncio
async def test_selected_native_turn_streams_observations_and_verified_completion(
    tmp_path: Path, monkeypatch
):
    seen = []
    native_record = object()
    proof = object()

    async def fake_execute(package, **kwargs):
        assert package == tmp_path
        assert kwargs["input_id"] == "a" * 32
        assert kwargs["prompt"] == "fresh"
        await kwargs["observe_event"](native_record)
        await kwargs["observe_event"](Chunk("first"))
        await kwargs["observe_event"](Chunk("second"))
        return NativeTurnResult("answer", proof)

    monkeypatch.setattr(TrackedTurnSession, "execute", fake_execute)

    async def observe(event):
        seen.append(event)

    claim = SimpleNamespace(
        input_id="a" * 32,
        bus=SimpleNamespace(
            _registry=SimpleNamespace(store=SimpleNamespace(path=tmp_path / "registry.json")),
            log=SimpleNamespace(path=tmp_path / "bus.jsonl"),
        ),
        session=SimpleNamespace(path=tmp_path / "session.jsonl"),
        _measurements=object(),
    )
    delivery = SelectedTurnDelivery(
        claim=claim,
        execution=SimpleNamespace(native_package=tmp_path),
        model=object(),
        observe_event=observe,
        following=object(),
        worktree=str(tmp_path),
        environment={},
        inbox=TurnInbox(),
        finish_event=asyncio.Event(),
        send_boundary=lambda *args: None,
        interrupt_boundary=lambda *args: None,
        input_started=lambda *args: True,
        request_progress=lambda *args: None,
        ui_request=NoUiRequest(),
    )
    thread = SimpleNamespace(
        worktree=str(tmp_path), thinking_level=object(), require_turn_lease=lambda: object()
    )
    backend = object.__new__(PiNativeBackend)
    events = []
    async for event in backend.run_turn(thread, InputContent(text="fresh"), delivery):
        events.append(event)
        await observe(event)

    assert seen == [native_record, *events]
    assert [event.text for event in events] == ["first", "second", "answer"]
    assert isinstance(events[-1], TrackedDone)
    assert events[-1].selected_result(claim.session.path).context is proof
