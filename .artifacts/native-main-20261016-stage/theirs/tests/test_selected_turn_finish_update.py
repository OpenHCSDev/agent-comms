import asyncio
from types import SimpleNamespace

import pytest

from agent_comms.turn_runner import TurnRunner


@pytest.mark.parametrize(
    ("failure", "cleanup_failure"),
    [
        (None, None),
        (RuntimeError("selected failed"), None),
        (RuntimeError("selected failed"), RuntimeError("cleanup failed")),
    ],
)
async def test_selected_turn_publishes_canonical_end_on_success_and_failure(
    monkeypatch, failure, cleanup_failure
):
    events = []

    class Following:
        def __init__(self, inputs, session_id, owner, inbox):
            self.inbox = inbox

        async def pass_on_unread(self, turns):
            events.append("passed")

    class Inputs:
        def __init__(self):
            self.backend_inboxes = {}

        async def finish_turn_inputs(self, session_id, inbox):
            self.backend_inboxes.pop(session_id, None)
            events.append("inputs-finished")
            if cleanup_failure is not None:
                raise cleanup_failure

    class Execution:
        owner_name = "owner"

        async def run(self, **kwargs):
            events.append("execution")
            if failure is not None:
                raise failure
            return "result"

    class Runner:
        def __init__(self):
            self.turn_locks = {}
            self.turn_tasks = {}
            self.inputs = Inputs()
            self.comms = SimpleNamespace(registry=SimpleNamespace(require=lambda name: name))
            self.effects = SimpleNamespace(_emit_event=lambda *args, **kwargs: None)

        def backend_for(self, session_id, owner):
            return "backend"

        async def publish_turn_state(self, session_id):
            assert session_id not in self.turn_tasks
            assert session_id not in self.inputs.backend_inboxes
            events.append("turn-end")

    monkeypatch.setattr("agent_comms.turn_runner.OwnerFollowingInputs", Following)
    runner = Runner()
    if cleanup_failure is not None:
        with pytest.raises(RuntimeError, match="cleanup failed"):
            await TurnRunner.run_selected(runner, "session", Execution())
        assert events == ["execution", "inputs-finished", "turn-end"]
    elif failure is not None:
        with pytest.raises(RuntimeError, match="selected failed"):
            await TurnRunner.run_selected(runner, "session", Execution())
        assert events == ["execution", "inputs-finished", "turn-end"]
    else:
        assert await TurnRunner.run_selected(runner, "session", Execution()) == "result"
        assert events == ["execution", "passed", "inputs-finished", "turn-end"]
