"""Actual pinned CLI through ordinary TurnSession; localhost provider only."""

import asyncio
import json
import os
from pathlib import Path

from native_proof_cases import read_proof_rows, write_proof_rows

import pytest

from agent_comms import agent_events as events
from agent_comms import backend


from native_backend_fixture import native_backend_fixture


@pytest.fixture
async def native_backend(tmp_path_factory, monkeypatch):
    if not os.environ.get("PI_COMPACTION_TEST_PACKAGE"):
        pytest.skip("Set PI_COMPACTION_TEST_PACKAGE to the immutable native bundle")
    monkeypatch.delenv("AGENT_COMMS_THREAD", raising=False)
    # Native originals belong to the persistent run, even when a consuming
    # module supplies a separate auto-deleting sealed-bus tmp_path fixture.
    async with native_backend_fixture(tmp_path_factory.mktemp("saved-native-owner")) as owner:
        yield owner

async def test_actual_native_queued_settlement_large_reuse_and_validated_reopen(native_backend):
    owner = native_backend
    first = await owner.run("first", followup="queued followup")
    assert isinstance(first[-1], events.Done) and first[-1].ok, first[-1]
    assert [row[2] for row in owner.starts] == ["first", "queued followup"]
    assert [row[0] for row in owner.starts] == [None, "queued"]
    assert len([event for event in first if isinstance(event, events.StreamSettled)]) == 1
    retained = owner.persistent.custody.child.proc
    assert retained is not None and retained.alive()

    await owner.force_reopen()
    assert not retained.alive()
    owner.provider.text = "Reopened retained context."
    reopened = await owner.run("after validated reopen")
    assert reopened[-1].ok and reopened[-1].text == owner.provider.text, reopened[-1]
    assert owner.persistent.custody.child.proc is not retained
    assert owner.persistent.custody.child.proc.alive()
    assert owner.persistent.custody.idle().current
    reopened_child = owner.persistent.custody.child.proc
    # The deliberately oversized output tests framing and exceeds Core's
    # immutable retained-context limit. Pi compacts it before settling; the
    # history and split-turn prefix each require a summary provider request.
    owner.provider.text = "L" * (2 * 1024 * 1024 + 257)
    second = await owner.run("large response")
    assert second[-1].ok and second[-1].text == owner.provider.text, second[-1]
    assert owner.persistent.custody.child.proc is reopened_child
    assert len(owner.saved_inputs()) == 4
    print(f"ordinary_native_response_bytes={len(second[-1].text.encode())}")

    assert len(owner.starts) == len(owner.saved_inputs()) == 4
    assert owner.provider.posts == len(owner.provider.requests) == 6
    original_texts = ["first", "queued followup", "after validated reopen", "large response"]
    for request, text in zip(owner.provider.requests[:4], original_texts, strict=True):
        assert request["messages"][-1] == {
            "role": "user", "content": [{"type": "text", "text": text}]
        }
    for request in owner.provider.requests[4:]:
        assert request["messages"][0]["content"].startswith(
            "You are a context summarization assistant."
        )
    compacted = [
        row for row in map(json.loads, owner.session.read_text().splitlines())
        if row["type"] == "compaction"
    ]
    assert len(compacted) == 1
    assert compacted[0]["tokensBefore"] > 300_000
    assert len({row[1] for row in owner.starts}) == 4
    proofs = read_proof_rows(owner.session)
    assert {row["inputId"] for row in proofs} == {row[1] for row in owner.starts}


async def test_actual_native_failure_after_tool_keeps_terminal_and_effect_once(
    native_backend, monkeypatch
):
    from acp import RequestError
    from acp.schema import TextContentBlock

    owner = native_backend
    await owner.author_history()
    effect = owner.project / "one-effect.txt"
    owner.provider.tool_call = (
        "bash", {"command": "printf 'effect\\n' >> one-effect.txt"}
    )
    chunks = owner.provider.response_chunks

    def fail_after_tool():
        yield from chunks()
        owner.provider.status = 400
        owner.provider.error_message = "controlled failure after actual tool effect"
        owner.provider.error_type = "invalid_request_error"

    monkeypatch.setattr(owner.provider, "response_chunks", fail_after_tool)
    async with owner.open_owner(native_options=("--tools", "bash")) as (agent, session):
        with pytest.raises(RequestError):
            await agent.prompt(session, [TextContentBlock(
                type="text", text="Distinct isolated native terminal verification."
            )])
        assert effect.read_text() == "effect\n"
        journal = [json.loads(line) for line in owner.session.read_text().splitlines()]
        messages = [row["message"] for row in journal if row.get("type") == "message"]
        errors = [message for message in messages if message.get("stopReason") == "error"]
        assert len(errors) == 1 and "controlled failure" in errors[0]["errorMessage"]
        assert owner.provider.posts == 2  # One tool request, one terminal refusal; no retries.
        tools = [message for message in messages if message.get("role") == "toolResult"]
        assert len(tools) == 1
        assert len([message for message in messages if message.get("role") == "user"]) == 2
        reports = list((owner.root / "diagnostics").glob("*.json"))
        failures = [json.loads(path.read_text()) for path in reports]
        assert len(failures) == 1
        assert failures[0]["reason"] == "model_request_failed"


@pytest.mark.parametrize("termination", ["cancel", "eof", "owner_stop"])
async def test_actual_native_interrupted_turn_never_replays_or_retains(native_backend, termination):
    owner = native_backend
    owner.provider.status = 0
    turn = asyncio.create_task(owner.run("single uncertain input"))
    try:
        async with asyncio.timeout(15):
            while owner.provider.posts != 1:
                assert not turn.done(), turn.result() if turn.done() else None
                await asyncio.sleep(0.01)
        child = owner.children[-1]
        session = backend.TurnSession.active[turn]
        stderr, forwarding = session.native.stderr_task, session.native.forwarding_task
        if termination == "cancel":
            turn.cancel()
            with pytest.raises(asyncio.CancelledError):
                await turn
        else:
            if termination == "owner_stop":
                await backend.terminate_task_process(turn)
            else:
                await child.stop()
            result = await turn
            assert isinstance(result[-1], events.Done) and not result[-1].ok
        assert turn not in backend.TurnSession.active
        assert stderr.done() and forwarding is not None and forwarding.done()
        assert not child.alive()
        assert not owner.persistent.available
        assert len(owner.starts) == len(owner.saved_inputs()) == owner.provider.posts == 1
    finally:
        if not turn.done():
            turn.cancel()
        await asyncio.gather(turn, return_exceptions=True)


async def test_actual_native_cancelled_owner_stop_keeps_cleanup_custody(
    native_backend, monkeypatch
):
    from agent_comms.native_custody import PiSessionChild, RetiringNative

    owner = native_backend
    owner.provider.status = 0
    turn = asyncio.create_task(owner.run("Uncertain input before interrupted owner stop"))
    entered, release = asyncio.Event(), asyncio.Event()
    close = PiSessionChild._shutdown
    stop = None
    try:
        async with asyncio.timeout(15):
            while owner.provider.posts != 1:
                assert not turn.done()
                await asyncio.sleep(0.01)
        session = backend.TurnSession.active[turn]
        forwarding = session.native.forwarding_task

        async def held_close(child, *, graceful):
            if child is session.native:
                entered.set()
                await release.wait()
            return await close(child, graceful=graceful)

        monkeypatch.setattr(PiSessionChild, "_shutdown", held_close)
        stop = asyncio.create_task(backend.terminate_task_process(turn))
        await asyncio.wait_for(entered.wait(), 5)
        stop.cancel()
        assert not stop.done()
        assert backend.TurnSession.active[turn] is session
        assert isinstance(owner.persistent.custody, RetiringNative)
        assert not owner.persistent.custody.child._close_job.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await stop
        result = await asyncio.wait_for(turn, 10)
        assert not result[-1].ok
        assert not session.native.proc.alive()
        assert session.native.stderr_task.done() and forwarding.done()
        assert session.native.forwarding_task is None
        assert turn not in backend.TurnSession.active
        assert owner.provider.posts == len(owner.saved_inputs()) == 1
    finally:
        release.set()
        if not turn.done():
            turn.cancel()
        await asyncio.gather(turn, return_exceptions=True)
        if stop is not None:
            await asyncio.gather(stop, return_exceptions=True)
