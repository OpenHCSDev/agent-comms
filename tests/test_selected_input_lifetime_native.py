"""Saved selected-owner history through real terminal/uncertain native outcomes.

Only HTTP provider responses and process termination are controlled. The native
package, admission writer, proof journal, bus and SQLite lifetime are real.
"""

import asyncio
import json
import os
from collections import deque
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.assignment_states import CompletedAssignment, FailedAssignment
from agent_comms.child_process import AttachedChild
from agent_comms.coordinated_runtime import SelectedExecution
from agent_comms.coordination_cohort import accept_delivery_cohort
from agent_comms.coordination_tables.attempts import ReplayFact
from agent_comms.coordinator import Coordination
from agent_comms.native_pi import NativePiTerminalFailure, NativePiUnavailable
from agent_comms.native_runtime_input import NativeRuntimeInput
from agent_comms.wake_candidate_index import WakeCandidateIndex
from agent_comms.wake_policy import PassiveWake
from compaction_loopback import LoopbackProvider
from test_coordinated_runtime import _root
from test_coordinated_runtime import tmp_path as private_root_fixture

tmp_path = private_root_fixture


class SelectedNativeJourney:
    def __init__(self, root, root_id, comms, package, direct):
        self.root, self.root_id, self.comms = root, root_id, comms
        self.package, self.direct = package, direct
        self.replies = deque()
        self.received = []
        self.children = []
        self.connections = set()
        self.session_file = None

    async def serve(self, reader, writer):
        task = asyncio.current_task()
        self.connections.add(task)
        try:
            reply = self.replies.popleft()
            self.received.append(reply)
            await reply.handle(reader, writer)
        finally:
            writer.close()
            await writer.wait_closed()
            self.connections.remove(task)

    def text(self, text):
        self.replies.append(LoopbackProvider(status=200, text=text))

    def execution(self):
        WakeCandidateIndex(self.comms.bus).maintain(rebuild=True)
        return SelectedExecution(
            root=self.root,
            wire_root_id=self.root_id,
            owner_name="beta",
            native_package=self.package,
            session_file=self.session_file,
        )

    def send(self, text):
        message = self.comms.messaging.send_initial_cohort(
            "sender", "beta" if self.direct else "#team", text
        )
        with Coordination(str(self.root / "coordination.sqlite3")) as store:
            accept_delivery_cohort(self.comms.bus, self.root_id, message.seq, store)
        return message

    def records(self):
        with Coordination(str(self.root / "coordination.sqlite3")) as store:
            return NativeRuntimeInput.select(store.session._connection)

    async def seed(self):
        if not self.direct:
            self.text('{"decision":"FULL"}')
        self.text("Saved selected history before the failure")
        seed = self.execution()
        result = await seed.run()
        assert result.disposition is CompletedAssignment
        row = next(row for row in self.records() if row.input_id == result.input_id)
        self.session_file = Path(row.session_file)
        return row


@pytest.fixture
async def journey(tmp_path, monkeypatch, direct):
    package = os.environ.get("AC_NATIVE_COPIED_PACKAGE")
    if not package:
        pytest.skip("Use the existing immutable native package; never call an external provider")
    root, root_id, comms, _initial, _people = _root(tmp_path, direct=direct)
    comms.registry.register(
        replace(
            comms.registry.require("beta"), model="selected-offline/fixture", thinking_level="off"
        )
    )
    owned = SelectedNativeJourney(root, root_id, comms, Path(package), direct)
    server = await asyncio.start_server(owned.serve, "127.0.0.1", 0)
    config = tmp_path / "local-config"
    config.mkdir(mode=0o700)
    (config / "models.json").write_text(
        json.dumps(
            {
                "providers": {
                    "selected-offline": {
                        "baseUrl": f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1",
                        "api": "openai-completions",
                        "models": [
                            {
                                "id": "fixture",
                                "name": "Local",
                                "contextWindow": 32768,
                                "maxTokens": 2048,
                            }
                        ],
                    }
                }
            }
        )
    )
    (config / "auth.json").write_text(
        json.dumps({"selected-offline": {"type": "api_key", "key": "offline-only-fixture"}})
    )
    (config / "settings.json").write_text(
        json.dumps(
            {
                "compaction": {"enabled": False},
                "retry": {"enabled": False, "maxRetries": 0, "provider": {"maxRetries": 0}},
            }
        )
    )
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(config))
    original = AttachedChild.start

    async def observe_child(*args, **kwargs):
        child = await original(*args, **kwargs)
        owned.children.append(child)
        return child

    monkeypatch.setattr(AttachedChild, "start", observe_child)
    try:
        async with asyncio.timeout(45):
            yield owned
    finally:
        for child in owned.children:
            await child.stop()
        server.close()
        await server.wait_closed()
        for task in tuple(owned.connections):
            task.cancel()
        await asyncio.gather(*owned.connections, return_exceptions=True)
        assert all(not child.alive() for child in owned.children)


@pytest.mark.parametrize("direct,triage_pass", [(True, False), (False, False), (False, True)])
async def test_actual_saved_terminal_failure_allows_only_new_input(journey, triage_pass):
    seed = await journey.seed()
    source = journey.send("This new input receives a terminal provider refusal")
    if triage_pass:
        journey.text('{"decision":"FULL"}')
    refusal = LoopbackProvider(status=400)
    journey.replies.append(refusal)
    failed = journey.execution()
    with pytest.raises(NativePiTerminalFailure) as caught:
        await failed.run()
    assert refusal.posts == 1
    assert caught.value.context.session_file == Path(seed.session_file)
    assert all(not child.alive() for child in journey.children)
    assert journey.comms.registry.require("beta").active_turn is None
    notice = journey.comms.views.full_history()[-1]
    assert notice.notice and "No automatic retry" in notice.body
    delivery = journey.comms.bus.log.read_delivery_cohort(journey.root_id, notice.seq)
    assert all(decision.wake_mode == PassiveWake() for decision in delivery.decisions)
    with Coordination(str(journey.root / "coordination.sqlite3")) as store:
        failed_input = NativeRuntimeInput.one(
            store.session._connection, input_id=caught.value.context.input_id
        )
        assignment = store.assignments.get(failed_input.assignment_id)
        if journey.direct or triage_pass:
            assert type(assignment.lifecycle) is FailedAssignment
            snapshot = store.snapshots.get(failed_input.execution_id)
            assert not snapshot.is_current and not snapshot.can_retry
            assert (
                snapshot.attempt.lifecycle.backend_done and snapshot.attempt.lifecycle.process_dead
            )
        else:
            assert assignment.lifecycle.deferred
    before = journey.records()
    received = len(journey.received)
    assert await journey.execution().run() is None
    assert journey.records() == before and len(journey.received) == received
    journey.send("A genuinely new independent input after refusal")
    if not journey.direct:
        journey.text('{"decision":"FULL"}')
    journey.text("NEW_INPUT_COMPLETED")
    result = await journey.execution().run()
    assert result.disposition is CompletedAssignment
    assert result.input_id != caught.value.context.input_id
    assert all(row in journey.records() for row in before)
    assert not journey.replies
    assert any(m.body == "NEW_INPUT_COMPLETED" for m in journey.comms.views.full_history())
    print(
        "SAVED_NATIVE_TERMINAL",
        "direct",
        journey.direct,
        "triage_pass",
        triage_pass,
        "source",
        source.seq,
        "provider_calls",
        sum(reply.posts for reply in journey.received),
    )


@pytest.mark.parametrize("direct", [True])
async def test_actual_saved_eof_preserves_unknown_and_never_replays(journey):
    await journey.seed()
    prior_ids = {row.input_id for row in journey.records()}
    journey.send("This new input loses its native transport after provider dispatch")
    pending_provider = LoopbackProvider(status=0)
    journey.replies.append(pending_provider)
    failed = journey.execution()
    running = asyncio.create_task(failed.run())
    try:
        while not pending_provider.posts:
            await asyncio.sleep(0.02)
        await journey.children[-1].stop()
        with pytest.raises(NativePiUnavailable) as error:
            await running
        assert not isinstance(error.value, NativePiTerminalFailure)
        (failed_input,) = [row for row in journey.records() if row.input_id not in prior_ids]
        with Coordination(str(journey.root / "coordination.sqlite3")) as store:
            snapshot = store.snapshots.get(failed_input.execution_id)
            assert not snapshot.is_current and not snapshot.can_retry
            assert snapshot.replay.facts & ReplayFact.UNKNOWN_EFFECTS
            assert not snapshot.replay.replay_safe
        before = journey.records()
        received = len(journey.received)
        assert await journey.execution().run() is None
        assert journey.records() == before and len(journey.received) == received
        journey.send("New independent input after transport loss")
        journey.text("AFTER_UNKNOWN_COMPLETED")
        result = await journey.execution().run()
        assert result.disposition is CompletedAssignment
        assert result.input_id != failed_input.input_id
        assert all(row in journey.records() for row in before)
        assert all(not child.alive() for child in journey.children)
        print("SAVED_NATIVE_EOF: UNKNOWN preserved, old input not replayed, new input completed")
    finally:
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)


@pytest.mark.parametrize("direct", [False])
async def test_actual_fresh_enrollment_retains_coverage_across_triage(journey):
    journey.text('{"decision":"FULL"}')
    journey.text("FRESH_TRIAGE_FULL_COMPLETED")
    result = await SelectedExecution(
        root=journey.root,
        wire_root_id=journey.root_id,
        owner_name="beta",
        native_package=journey.package,
        fresh_private_enrollment=True,
    ).run()
    assert result.disposition is CompletedAssignment
    assert result.fresh_session is not None
    rows = journey.records()
    assert {row.stage for row in rows} == {"triage", "full"}
    assert len(rows) == 2 and len(journey.received) == 2
    assert {Path(row.session_file) for row in rows} == {result.fresh_session.path}
    assert all(not child.alive() for child in journey.children)
    assert journey.comms.registry.require("beta").active_turn is None
    print(
        "FRESH_NATIVE: enrollment retained through triage and full, original input completed once"
    )


@pytest.mark.parametrize("direct", [False])
async def test_actual_selected_first_start_preserves_unreviewed_cli_refusal(journey):
    with pytest.raises(NativePiUnavailable, match="first-source CLI builtins are unreviewed"):
        await SelectedExecution(
            root=journey.root,
            wire_root_id=journey.root_id,
            owner_name="beta",
            native_package=journey.package,
            fresh_private_enrollment=True,
            selected_thinking_level="low",
        ).run()
    assert not journey.received
    assert journey.comms.registry.require("beta").active_turn is None
    before = journey.records()
    assert len(before) == 1
    assert await journey.execution().run() is None
    assert journey.records() == before and not journey.received
    print("FIRST_START_REFUSAL: no provider call, lease released, reserved input never replayed")
