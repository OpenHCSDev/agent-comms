"""Real selected Pi send epoch, exact dead-owner recovery, and unrelated new input.

Only the localhost provider response and the owned native child exit are
controlled. Registry admission, release receipt, process identity, coordinator,
native input/proof journal and recovery monitor use their production owners.
"""

import asyncio
import hashlib
import json
import multiprocessing
import os
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.assignment_states import CompletedAssignment
from agent_comms.attempt_recovery import RecoveryMonitorCapability
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.coordination_errors import RecoveryBlocked
from agent_comms.coordination_tables.attempts import ReplayFact
from agent_comms.coordinator import Coordination
from agent_comms.native_admission_epoch import RecordedNativeAdmission
from agent_comms.native_pi import NativePiTerminalFailure, NativePiUnavailable
from compaction_loopback import LoopbackProvider
from test_coordinated_runtime import _root, tmp_path  # noqa: F401
from test_native_unknown_recovery import input_evidence
from test_selected_input_lifetime_native import (
    SelectedNativeJourney,
    configured_native_journey,
)


def released_native_owner(directory, package, output, exit_allowed):
    async def run():
        root, root_id, comms, *_ = _root(Path(directory), direct=True)
        comms.registry.register(
            replace(
                comms.registry.require("beta"),
                model="selected-offline/fixture",
                thinking_level="off",
            )
        )
        owned = SelectedNativeJourney(root, root_id, comms, Path(package), True)
        with pytest.MonkeyPatch.context() as patch:
            async with configured_native_journey(owned, Path(directory) / "owner-config", patch):
                await owned.seed()
                before = {row.input_id for row in owned.records()}
                owned.send("Independent fixture input with uncertain provider completion")
                provider = LoopbackProvider(status=0)
                owned.replies.append(provider)
                task = asyncio.create_task(owned.execution().run())
                try:
                    while not provider.posts:
                        await asyncio.sleep(0.02)
                    patch.setenv("AGENT_COMMS_THREAD", "beta")
                    comms.owners.release("beta")
                    await owned.children[-1].stop()
                    with pytest.raises(NativePiUnavailable) as error:
                        await task
                    assert not isinstance(error.value, NativePiTerminalFailure)
                    (source,) = [row for row in owned.records() if row.input_id not in before]
                    assert isinstance(
                        source.sent_owner_admission_generation, RecordedNativeAdmission
                    )
                    with Coordination(str(root / "coordination.sqlite3")) as store:
                        snapshot = store.snapshots.get(source.execution_id)
                        assert snapshot.is_current and not snapshot.can_retry
                    calls = sum(reply.posts for reply in owned.received)
                    assert calls == 2
                finally:
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        output.put(
            (
                str(root),
                root_id,
                source.execution_id,
                source.input_id,
                str(owned.session_file),
                calls,
            )
        )

    asyncio.run(run())
    if not exit_allowed.wait(15):
        raise TimeoutError("fixture must finish exact live-owner refusal before owner exit")


async def test_installed_native_epoch_release_unknown_recovery(tmp_path, monkeypatch):  # noqa: F811
    package = os.environ.get("AC_NATIVE_COPIED_PACKAGE")
    if not package:
        pytest.skip("Existing immutable native package required; never use a paid provider")
    evidence = Path(os.environ["AC_NATIVE_EPOCH_EVIDENCE"])
    evidence.mkdir(mode=0o700, parents=True, exist_ok=True)
    context = multiprocessing.get_context("fork")
    output, exit_allowed = context.Queue(), context.Event()
    process = context.Process(
        target=released_native_owner, args=(str(tmp_path), package, output, exit_allowed)
    )
    process.start()
    try:
        root, root_id, execution_id, input_id, saved_session, calls = await asyncio.to_thread(
            output.get, True, 35
        )
        root = Path(root)
        with Coordination(str(root / "coordination.sqlite3")) as store:
            with pytest.raises(RecoveryBlocked, match="release does not prove loss"):
                RecoveryMonitorCapability.abandon_released_native_attempt(store, execution_id)
            assert store.snapshots.get(execution_id).is_current
        exit_allowed.set()
        await asyncio.to_thread(process.join, 5)
        assert process.exitcode == 0
        comms = Comms(root, private_initial_writes=True)
        source = next(
            row
            for row in SelectedNativeJourney(root, root_id, comms, Path(package), True).records()
            if row.input_id == input_id
        )
        # The original successful seed acquired this saved session. UNKNOWN
        # deliberately has no reconstructed terminal/session-binding receipt.
        native_path = Path(saved_session)
        native_before = native_path.read_bytes()
        proof_path = Path(str(native_path) + ".input-proof")
        proof_before = proof_path.read_bytes()
        wire_before = (root / "bus.jsonl").read_bytes()
        with Coordination(str(root / "coordination.sqlite3")) as store:
            before = input_evidence(store)
            closed = RecoveryMonitorCapability.abandon_released_native_attempt(
                store, execution_id
            ).value
            assert not closed.is_current and not closed.can_retry
            assert closed.replay.facts & ReplayFact.UNKNOWN_EFFECTS
            assert not closed.replay.replay_safe and closed.replay.side_effects_possible
            assert closed.execution.reason_code == "released_native_unknown"
            assert input_evidence(store) == before
            assert native_path.read_bytes() == native_before
            assert proof_path.read_bytes() == proof_before
            assert (root / "bus.jsonl").read_bytes() == wire_before
            with pytest.raises(RecoveryBlocked):
                RecoveryMonitorCapability.abandon_released_native_attempt(store, execution_id)
        comms.registry.register(
            replace(
                comms.registry.require("beta"),
                process_identity=ProcessIdentity.capture(os.getpid()),
            ),
            new_owner=True,
        )
        owned = SelectedNativeJourney(root, root_id, comms, Path(package), True)
        owned.session_file = native_path
        async with configured_native_journey(owned, tmp_path / "successor-config", monkeypatch):
            rows = owned.records()
            assert await owned.execution().run() is None
            assert not owned.received and owned.records() == rows
            owned.send("A new independent request after explicit UNKNOWN abandonment")
            owned.text("NEW_NATIVE_EPOCH_COMPLETED")
            result = await owned.execution().run()
            assert result.disposition is CompletedAssignment and result.input_id != input_id
            assert sum(reply.posts for reply in owned.received) == 1
            assert source == next(row for row in owned.records() if row.input_id == input_id)
            assert native_path.read_bytes().startswith(native_before)
            assert any(
                row.body == "NEW_NATIVE_EPOCH_COMPLETED" for row in comms.views.full_history()
            )
        receipt = {
            "provider": "localhost-only",
            "provider_posts": calls + 1,
            "recorded_epoch": True,
            "exact_live_owner_refused": True,
            "exact_dead_owner_recovered": True,
            "unknown_retained": True,
            "input_rows_wire_native_proof_unchanged_by_recovery": True,
            "old_input_automatic_replay_posts": 0,
            "new_independent_input_completed": True,
            "native_prefix_sha256": hashlib.sha256(native_before).hexdigest(),
            "proof_preimage_sha256": hashlib.sha256(proof_before).hexdigest(),
            "native_children_closed": all(not child.alive() for child in owned.children),
            "public_effects": 0,
        }
        (evidence / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
        print(json.dumps(receipt, sort_keys=True))
    finally:
        exit_allowed.set()
        await asyncio.to_thread(process.join, 5)
        if process.is_alive():
            process.terminate()
            await asyncio.to_thread(process.join, 5)
        output.close()
        # Keep the genuine original UNKNOWN/native/proofs even on a refused gate.
        shutil.copytree(tmp_path, evidence / "private-originals")
