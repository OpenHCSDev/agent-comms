"""Setup refusal stops a real copied-session ACP worker before selecting its DM."""
import asyncio
import json
import shutil
from pathlib import Path

import pytest

from agent_comms.activity import StoppedDrainDiagnostic
from agent_comms.coordinated_runtime import SelectedExecution


async def test_private_directory_failure_stops_one_fresh_dm(native_backend, monkeypatch):
    native = native_backend
    await native.author_history()
    original = native.session
    before = original.read_bytes()
    directory = native.root / "copied-native-sessions"
    directory.mkdir(mode=0o700)
    copied = directory / "copied.jsonl"
    shutil.copy2(original, copied)
    copied.chmod(0o600)
    native.session = copied
    observed = []
    actual = SelectedExecution.validate

    def validate(execution):
        observed.append(execution.owner_name)
        return actual(execution)

    async with native.open_owner(runtime_enabled=True, auto_wake=True) as (agent, session):
        directory.chmod(0o755)
        monkeypatch.setattr(SelectedExecution, "validate", validate)
        comms = agent._comms
        comms.threads.claim_thread("sender", tags=frozenset(), worktree=str(native.project))
        sent = comms.messaging.send("sender", session, "P632 one fresh DM; no replay").message
        task = agent.inputs.state(session).drain_task
        assert task is not None
        with pytest.raises(ValueError, match="owner-only"):
            await asyncio.wait_for(asyncio.shield(task), 20)
        assert task.done()
        diagnostic = next(view for view in comms.views.thread_views()
                          if view.thread.name == session).activity.readiness.source_diagnostic()
        assert isinstance(diagnostic, StoppedDrainDiagnostic)
        # These are later wakes of the same stopped worker, not new input sends.
        for _ in range(3):
            agent.inputs.wake_observer(session)
        assert task.done()
        assert agent.inputs.state(session).drain_task is task
        assert len(observed) == 1
        assert native.provider.posts == 0
        assert original.read_bytes() == before
        assert copied.read_bytes() == before
        result = {
            "attempts": len(observed), "diagnostic_kind": diagnostic.artifact_kind,
            "dm_message_id": sent.message_id, "native_directory_mode": "755",
            "provider_requests": native.provider.posts, "later_wakes": 3,
            "worker_stopped": task.done(), "source_unchanged": True,
            "replay": False, "copied_root": str(native.root),
        }
        output = Path('/home/ts/.cache/agent-scratch/core-native-runtime/p631')
        output.mkdir(parents=True, exist_ok=True)
        (output / 'result.json').write_text(json.dumps(result, indent=2))
        print(json.dumps(result), flush=True)
        directory.chmod(0o700)
