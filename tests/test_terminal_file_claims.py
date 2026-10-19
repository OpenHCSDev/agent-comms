"""Exact terminal claim release on real private stores and worker processes."""

from bus_history import read_history
import asyncio
import json
import os
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_comms.attempt_states import AttemptFailedAttempt, UnknownOutcomeAttempt
from agent_comms.selected_claim import publish_selected_resource_claim
from agent_comms.coordination_tables.attempts import ReplayFact
from agent_comms.envelope_claim_transitions import ExistingFileClaim
from agent_comms.execution_states import FailedExecution
from agent_comms.obligation_states import FailedResponse
from agent_comms.owner_fence import OwnerFence
from test_claim_bus_read import selected_turn


@pytest.mark.parametrize("outcome", ["failure", "unknown"])
def test_terminal_outcomes_release_exact_claims(monkeypatch, outcome):
    monkeypatch.setattr("test_claim_bus_read.prepare_fence_token", lambda: "test-token")
    with selected_turn() as (comms, store, selected, resource):
        claim = publish_selected_resource_claim(comms, store, selected, "Alice", ExistingFileClaim(resource))
        snapshot = store.snapshots.get("execution")
        attempt = snapshot.attempt
        fence = OwnerFence(attempt.execution_id, attempt.attempt_ordinal, attempt.owner_thread,
                           attempt.owner_generation, attempt.revision, "test-token")
        if outcome == "unknown":
            terminal = store.attempts.fail_unknown(fence, expected_pointer_revision=snapshot.pointer_revision).value
            assert terminal.replay.facts & ReplayFact.UNKNOWN_EFFECTS
            assert not terminal.can_retry
        else:
            final = store.attempts.advance(
                fence, type(attempt.lifecycle), expected_pointer_revision=snapshot.pointer_revision,
                backend_done=True, process_dead=True,
            ).value
            terminal = store.attempts.settle_nonpublication(
                final.fence, expected_pointer_revision=final.snapshot.pointer_revision,
                outcome=AttemptFailedAttempt(),
            ).value
        assert terminal.attempt.lifecycle.failed and not terminal.is_current
        expected_outcome = UnknownOutcomeAttempt if outcome == "unknown" else AttemptFailedAttempt
        assert type(terminal.attempt.lifecycle) is expected_outcome
        phase = store.session._connection.execute(
            "SELECT phase, phase_kind FROM attempts WHERE execution_id = ? AND attempt_ordinal = ?",
            (attempt.execution_id, attempt.attempt_ordinal),
        ).fetchone()
        assert tuple(phase) == (expected_outcome.declared_name, expected_outcome.declared_name)
        assert dict(comms.bus.log.claim_projection()) == {}
        notice = read_history(comms)[-1]
        release = notice.claim_transition.releases[0]
        expected = (
            f"Released files after a turn with unknown outcome; read them before editing: {resource.name}"
            if outcome == "unknown"
            else f"Released files after a failed turn: {resource.name}"
        )
        assert notice.body == expected
        assert (release.resource, release.generation) == (claim.resource, claim.generation)

        # A delayed old settlement must leave a later acquired generation intact.
        later = comms.messaging.send("Alice", "sender", "Independent manual claim",
                                            claims=(ExistingFileClaim(resource),)).message
        current = comms.bus.log.claim_projection()[str(resource)]
        assert current.generation != claim.generation
        before = (comms.root / "bus.jsonl").read_bytes()
        with store.session.settlement() as boundary:
            terminal.release_resources(boundary, terminal.attempt.lifecycle)
        assert (comms.root / "bus.jsonl").read_bytes() == before
        assert comms.bus.log.claim_projection()[str(resource)] == current
        assert later.claim_transition.generation == current.generation


def test_unknown_outcome_never_defers_when_retry_is_otherwise_authorized():
    execution, response = UnknownOutcomeAttempt().disposition(
        SimpleNamespace(retry_authorized=True), SimpleNamespace(attempt_ordinal=1)
    )

    assert type(execution) is FailedExecution
    assert type(response) is FailedResponse


def test_terminal_release_spans_the_existing_envelope_bound():
    with selected_turn() as (comms, store, selected, resource):
        for index in range(35):
            file = resource.parent / f"file-{index}.py"
            file.write_text("original\n")
            publish_selected_resource_claim(
                comms, store, replace(selected, operation_id=f"{index:032x}"), "Alice", ExistingFileClaim(file)
            )
        from test_claim_bus_read import settle_failed
        settle_failed(store)
        assert dict(comms.bus.log.claim_projection()) == {}
        releases = [m.claim_transition for m in read_history(comms)
                    if m.claim_transition and m.claim_transition.releases]
        assert [len(row.releases) for row in releases] == [32, 3]
        notices = [m for m in read_history(comms)
                   if m.claim_transition and m.claim_transition.releases]
        assert all(m.body.startswith("Released files after a failed turn: ") for m in notices)
        assert sum(m.body.count("file-") for m in notices) == 35


@pytest.mark.asyncio
async def test_real_worker_stop_start_releases_claim_and_edits_same_file(tmp_path, native_runtime_package):
    from agent_comms.comms import Comms
    from agent_comms.coordinator import Coordination
    from agent_comms.native_runtime_input import NativeRuntimeInput
    from agent_comms.threads import Thread
    from native_backend_fixture import native_backend_fixture

    async with native_backend_fixture(tmp_path, package=native_runtime_package) as native:
        target = native.project / "module.py"
        target.write_text("value = 1\n")
        comms = Comms(native.root)
        comms.registry.declare(comms.registry.new_thread('worker', frozenset(), str(native.project), session_file=str(native.session), model='response-local/fixture'))
        comms.threads.claim_thread("sender", tags=frozenset(), worktree=str(native.project))
        original_chunks = native.provider.response_chunks
        gate = asyncio.Event()

        def claim_then_hold():
            yield from original_chunks()
            native.provider.response_gate = gate

        native.provider.response_chunks = claim_then_hold
        native.provider.tool_call = ("write", {"path": str(target), "content": "value = 2\n"})
        arguments = native.native_arguments()
        first = await asyncio.to_thread(comms.owners.start, "worker", agent_args=arguments)
        try:
            comms.messaging.send("sender", "worker", "Change the value").message
            async with asyncio.timeout(40):
                while target.read_text() != "value = 2\n" or native.provider.posts < 2:
                    await asyncio.sleep(0.02)
            assert comms.bus.log.claim_projection()[str(target)].claim_scope is not None
            await asyncio.to_thread(comms.owners.stop, "worker")
            assert not comms.registry.require("worker").process_alive
            assert dict(comms.bus.log.claim_projection()) == {}
            with Coordination(str(native.root / "coordination.sqlite3")) as store:
                old = NativeRuntimeInput.select(store.session._connection)
                assert old
                old_bytes = tuple(old)
                terminal = store.snapshots.get(old[-1].execution.execution_id)
                assert terminal.attempt.lifecycle.failed and not terminal.can_retry
                assert terminal.replay.facts & ReplayFact.UNKNOWN_EFFECTS

            # Finish the old isolated HTTP handler before changing its mutable
            # fixture response. Otherwise it can consume the new tool proposal.
            old_response_finished = asyncio.Event()

            def finish_old_response():
                yield {"content": "Old stopped request"}, None
                yield {}, "stop"
                old_response_finished.set()

            native.provider.response_chunks = finish_old_response
            gate.set()
            async with asyncio.timeout(5):
                await old_response_finished.wait()
            def edit_after_restart():
                assert any(tool["function"]["name"] == "edit"
                           for tool in native.provider.requests[-1]["tools"])
                yield {"tool_calls": [{"index": 0, "id": "restart-edit", "type": "function",
                                      "function": {"name": "edit", "arguments":
                                      json.dumps({"path": str(target), "edits": [
                                          {"oldText": "value = 2", "newText": "value = 3"}]})}}]}, None
                yield {}, "tool_calls"
                native.provider.response_chunks = original_chunks

            native.provider.response_chunks = edit_after_restart
            native.provider.response_gate = None
            native.provider.text = "Edited after restart."
            second = await asyncio.to_thread(comms.owners.start, "worker", agent_args=arguments)
            assert first.pid != second.pid
            comms.messaging.send("sender", "worker", "Edit the same file for this new request").message
            async with asyncio.timeout(40):
                while target.read_text() != "value = 3\n" or comms.bus.log.claim_projection():
                    await asyncio.sleep(0.02)
            with Coordination(str(native.root / "coordination.sqlite3")) as store:
                assert tuple(NativeRuntimeInput.one(store.session._connection, input_id=row.input_id)
                             for row in old_bytes) == old_bytes
                assert not store.snapshots.get(terminal.execution.execution_id).can_retry
            assert any(m.body == "Edited after restart." for m in read_history(comms))
        finally:
            await asyncio.to_thread(comms.owners.stop, "worker")
