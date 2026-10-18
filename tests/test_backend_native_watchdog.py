"""Actual pinned Pi outcomes; no injected RPC events or paid provider requests."""

import asyncio
import json

import pytest
from agent_comms.turn_watchdog import ModelWait

import agent_comms.agent_events as events
import agent_comms.backend as backend
from agent_comms.agent_backend import InputContent, InputId, InputRequest, SendNow, TurnInbox, WhenBusy



@pytest.mark.parametrize("outcome", ["failure", "stall"])
async def test_actual_native_watchdog_failure_never_replays(native_backend, outcome):
    owner = native_backend
    owner.provider.status = 503 if outcome == "failure" else 0
    result = await owner.run("One diagnostic input", model_wait=ModelWait(2.0))
    print("native_outcome", outcome, repr(result), flush=True)
    assert isinstance(result[-1], events.Done) and not result[-1].ok, result[-1]
    assert len(owner.starts) == len(owner.saved_inputs()) == owner.provider.posts == 1
    assert not owner.persistent.available
    states = [item for item in result if isinstance(item, events.TurnState)]
    if outcome == "stall":
        assert [item.state for item in states] == ["model_stalled", "aborting", "failed"]
        assert "no RPC progress" in result[-1].text
    else:
        assert "loopback retryable failure" in result[-1].text
    assert all(not child.alive() for child in owner.children)


async def test_actual_native_interrupt_promotes_only_selected_followup(native_backend):
    owner = native_backend
    owner.provider.status = 0
    queue = TurnInbox()
    task = asyncio.create_task(owner.run("Interrupted original", queue=queue))
    try:
        async with asyncio.timeout(10):
            while owner.provider.posts != 1:
                assert not task.done(), task.result() if task.done() else None
                await asyncio.sleep(0.01)
        owner.provider.status = 200
        queue.put_nowait(InputRequest(input_id=InputId("selected"), content=InputContent(text="Selected followup"), when_busy=WhenBusy.STEER))
        queue.put_nowait(SendNow((InputId("selected"),)))
        result = await task
        assert result[-1].ok, result[-1]
        assert any(isinstance(item, events.SteeringInterrupted) for item in result)
        assert [item[2] for item in owner.starts] == ["Interrupted original", "Selected followup"]
        assert len(owner.saved_inputs()) == owner.provider.posts == 2
        assert len({item[1] for item in owner.starts}) == 2
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(
    "excursion", ["provider_retry", "overflow_compaction", "compaction_failure", "compaction_abort"]
)
async def test_actual_native_retry_and_compaction_excursions(
    native_backend, monkeypatch, excursion
):
    from native_event_host import install_event_host

    owner = native_backend
    configuration = json.loads((owner.config / "models.json").read_text())
    origin = configuration["providers"]["response-local"]["baseUrl"].removesuffix("/v1")
    install_event_host(
        monkeypatch,
        "pi",
        origin,
        native_settings={
            "compaction": {"enabled": True, "keepRecentTokens": 500, "reserveTokens": 16384},
            "retry": {
                "enabled": True,
                "maxRetries": 1,
                "baseDelayMs": 1,
                "provider": {"maxRetries": 0},
            },
        },
    )
    prior = 0
    if excursion != "provider_retry":
        owner.provider.text = "Prior retained context. " * 3000
        seeded = await owner.run("Seed actual retained context")
        assert seeded[-1].ok, seeded[-1]
        prior = owner.provider.posts
    owner.provider.text = "Recovered actual native turn."
    original = owner.provider.handle

    async def respond(reader, writer):
        if owner.provider.posts == prior:
            owner.provider.status = 503 if excursion == "provider_retry" else 400
        elif excursion == "compaction_failure":
            owner.provider.status = 503
        elif excursion == "compaction_abort":
            owner.provider.status = 0
        else:
            owner.provider.status = 200
        await original(reader, writer)

    owner.provider.handle = respond
    queue = TurnInbox()
    task = asyncio.create_task(
        owner.run("Recover without replaying this input", queue=queue)
    )
    try:
        if excursion == "compaction_abort":
            async with asyncio.timeout(10):
                while owner.provider.posts < prior + 2:
                    assert not task.done(), task.result() if task.done() else None
                    await asyncio.sleep(0.01)
            # The owner's cancel path: retire the turn's Pi child mid-summary.
            await backend.terminate_task_process(task)
        result = await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    print("native_excursion", excursion, repr(result), flush=True)
    assert len(owner.saved_inputs()) == len(owner.starts) == (2 if prior else 1)
    states = [item for item in result if isinstance(item, events.TurnState)]
    if excursion in {"compaction_failure", "compaction_abort"}:
        assert not result[-1].ok
        assert not any(isinstance(item, events.CompactionEnd) and not item.aborted
                       for item in result)
        if excursion == "compaction_failure":
            assert any(isinstance(item, events.CompactionEnd) and item.aborted for item in result)
        assert not any(
            json.loads(line)["type"] == "compaction"
            for line in owner.session.read_text().splitlines()
        )
        if excursion == "compaction_failure":
            # Pi's own summarizer follows the declared retry settings
            # (maxRetries 1): the overflowed request, then two summary requests.
            assert any(item.reason_code == "summarization_retry" for item in states)
            assert owner.provider.posts == prior + 3
        else:
            assert owner.provider.posts == prior + 2
    else:
        assert result[-1].ok and result[-1].text == owner.provider.text, result[-1]
        assert any(item.state == "retrying" for item in states)
        assert any(item.state == "recovered" for item in states)
        if excursion != "provider_retry":
            assert any(isinstance(item, events.CompactionStart) for item in result)
            assert any(
                isinstance(item, events.CompactionEnd) and not item.aborted for item in result
            )
            assert any(
                json.loads(line)["type"] == "compaction"
                for line in owner.session.read_text().splitlines()
            )
        assert owner.provider.posts == prior + (3 if prior else 2)
