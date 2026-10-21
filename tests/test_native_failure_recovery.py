"""Released-owner recovery refuses damaged original evidence; it grants no replay."""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
from contextlib import closing
from dataclasses import dataclass
from multiprocessing.process import BaseProcess
from multiprocessing.synchronize import Event
from pathlib import Path
from typing import NamedTuple

import pytest

from agent_comms.attempt_recovery import RecoveryMonitorCapability
from agent_comms.comms import Comms
from agent_comms.coordination_errors import IdentityConflict, RecoveryBlocked
from agent_comms.coordination_tables.executions import ExecutionOrigin
from agent_comms.coordinator import Coordination
from agent_comms.cursor_publication import CursorDelivery
from agent_comms.declared_family import DeclaredFamily
from agent_comms.fresh_private_session import create_fresh_private_session
from agent_comms.launch_runtime import ThreadLaunch
from agent_comms.native_pi import NativePiUnavailable
from agent_comms.native_runtime_input import NativeRuntimeInput
from agent_comms.owner_lifecycle import OwnerReleaseEvidence
from agent_comms.selected_actions import CodingSelectedAction
from agent_comms.selected_participant import SelectedParticipant
from agent_comms.selected_session import SelectedSession
from agent_comms.selected_turn import SelectedAttempt
from agent_comms.selected_write_authority import NoSelectedWritePlans
from agent_comms.text_digest import TextDigest
from native_proof_cases import append_input_entry, recorded_context, write_proof_rows
from test_coordinated_runtime import _root, tmp_path  # noqa: F401


class FailedNativeInput(NamedTuple):
    root: Path
    execution_id: str
    input_id: str
    session_file: Path


async def reserve_failed_input(directory: Path) -> FailedNativeInput:
    """Author a historical crash fixture through the real reservation owners.

    No model or live acknowledgement is impersonated. The fixture places an
    original committed context before its unsettled failed attempt, so each
    damage case actually reaches the evidence check it describes.
    """
    root, root_id, comms, _initial, _people = _root(directory, direct=True)
    with closing(CursorDelivery()) as cursor_delivery:
        with Coordination(root / "coordination.sqlite3") as store:
            async with SelectedParticipant.select(
                comms, store, root_id, "beta", 0, cursor_delivery=cursor_delivery,
            ) as participant:
                assert participant is not None
                created = store.executions.create(
                    participant.batch.execution_id, ExecutionOrigin.WIRE,
                    participant.lookup, participant.owner.thread.name, 1,
                    sources=participant.batch.sources,
                ).value
                attempt = await SelectedAttempt.engage(participant, created)
                directory = participant.owner.thread.native_session_directory(root)
                identity = create_fresh_private_session(directory, worktree=Path(participant.owner.thread.worktree))
                participant.owner = comms.registry.attach_native_session(
                    participant.owner, identity.session_file,
                )
                session = SelectedSession.for_launch(directory, identity.path)
                request, _action = await attempt.prepare(
                    session, CodingSelectedAction(), NoSelectedWritePlans(),
                )
                claim = request.claim_scope

                def write_original():
                    with claim(identity):
                        pass
                    # An unresolved send consumes this token and this attempt.
                    # Neither the same writer nor a fresh reservation may retry it.
                    with pytest.raises(IdentityConflict):
                        with claim(identity):
                            pytest.fail("The original native send token was reused")
                    with Coordination(root / "coordination.sqlite3") as writer:
                        with pytest.raises(IdentityConflict):
                            claim.stage.reserve(writer, participant.identity, claim.token_digest)
                        assert len(NativeRuntimeInput.select(writer.session._connection)) == 1
                    entry_id = "original-failed-entry"
                    offset = append_input_entry(identity.path, {
                        "type": "message", "id": entry_id,
                        "timestamp": "2026-10-01T00:00:00.000Z",
                        "message": {"role": "user", "inputId": claim.input_id,
                                    "inputDigest": TextDigest.of(claim.prompt.text).value,
                                    "content": [{"type": "text", "text": claim.prompt.text}]},
                    })
                    record = {
                        "schema": 2, "type": "context_committed",
                        "inputId": claim.input_id, "sessionId": identity.session_id,
                        "sessionEntryId": entry_id, "requestGeneration": 1,
                        "llmContextDigest": "f" * 64, "committedAtMs": 1_791_964_800_001,
                    }
                    write_proof_rows(identity.path, (record,))
                    context = recorded_context(identity.path, record, session_entry_start=offset)
                    with Coordination(root / "coordination.sqlite3") as writer:
                        with writer.session.writing() as db:
                            NativeRuntimeInput.one(db, input_id=claim.input_id).commit_context(db, context)
                    append_input_entry(identity.path, {
                        "type": "message", "id": "error-terminal", "parentId": entry_id,
                        "message": {"role": "assistant", "stopReason": "error",
                                    "errorMessage": "Codex error: The usage limit has been reached",
                                    "content": []},
                    })

                await Coordination.run_worker(write_original)
                launch = ThreadLaunch.from_thread(participant.owner.thread)
                environment = launch.apply(dict(os.environ))
                os.environ.clear()
                os.environ.update(environment)
                comms.owners.release("beta")
                return FailedNativeInput(root, participant.batch.execution_id,
                                         claim.input_id, identity.path)


def failed_owner(directory, output, exit_allowed):
    output.put(asyncio.run(reserve_failed_input(Path(directory))))
    if not exit_allowed.wait(10):
        raise TimeoutError("test failed to release fixture process")


@dataclass
class ReleasedNativeFailure:
    source: FailedNativeInput
    process: BaseProcess
    exit_allowed: Event

    def leave(self):
        self.exit_allowed.set()
        self.process.join(5)
        assert self.process.exitcode == 0


@pytest.fixture
def released_failure(tmp_path):  # noqa: F811
    context = multiprocessing.get_context("spawn")
    output, exit_allowed = context.Queue(), context.Event()
    process = context.Process(target=failed_owner, args=(str(tmp_path), output, exit_allowed))
    process.start()
    try:
        yield ReleasedNativeFailure(output.get(timeout=10), process, exit_allowed)
    finally:
        exit_allowed.set()
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(5)
        output.close()
        assert process.exitcode == 0


def test_recovery_refuses_released_but_live_owner(released_failure):
    source = released_failure.source
    with Coordination(source.root / "coordination.sqlite3") as store:
        with pytest.raises(RecoveryBlocked):
            RecoveryMonitorCapability.recover_native_failure(store, source.execution_id, source.session_file)
        assert store.snapshots.get(source.execution_id).is_current


class EvidenceDamage(DeclaredFamily, affix="Damage"):
    refusal = (RecoveryBlocked, NativePiUnavailable)

    def alter(self, source):
        rows = [json.loads(line) for line in source.session_file.read_text().splitlines()]
        self.alter_rows(rows)
        source.session_file.write_text("".join(json.dumps(row) + "\n" for row in rows))
        return source.session_file

    def alter_rows(self, rows):
        raise NotImplementedError


class UnfinishedDamage(EvidenceDamage):
    def alter_rows(self, rows):
        rows.pop()


class SuccessDamage(EvidenceDamage):
    def alter_rows(self, rows):
        rows[-1]["message"]["stopReason"] = "stop"


class DifferentParentDamage(EvidenceDamage):
    def alter_rows(self, rows):
        rows[-1]["parentId"] = "other"


class AdditionalInputDamage(EvidenceDamage):
    def alter_rows(self, rows):
        rows.append({"type": "message", "message": {"role": "user", "content": []}})


class ToolDamage(EvidenceDamage):
    def alter_rows(self, rows):
        rows[-1]["message"]["content"] = [{"type": "toolCall", "name": "bash"}]


class WrongEpochDamage(EvidenceDamage):
    def alter(self, source):
        releases = Comms(source.root).owners.releases
        original = releases.get("beta")
        with releases.writing() as db:
            OwnerReleaseEvidence.update(db, where="name=?", parameters=("beta",), before=original.after + 1)
        return source.session_file


class WrongSessionDamage(EvidenceDamage):
    refusal = ValueError

    def alter(self, source):
        return source.session_file.parent.parent / "other.jsonl"


@pytest.mark.parametrize("damage", EvidenceDamage.members_with(EvidenceDamage),
                         ids=lambda damage: damage.declared_name)
def test_recovery_refuses_ambiguous_evidence(released_failure, damage):
    released_failure.leave()
    source = released_failure.source
    case = damage()
    session_file = case.alter(source)
    before = source.session_file.read_bytes()
    with Coordination(source.root / "coordination.sqlite3") as store:
        with pytest.raises(case.refusal):
            RecoveryMonitorCapability.recover_native_failure(store, source.execution_id, session_file)
        assert store.snapshots.get(source.execution_id).is_current
        assert len(NativeRuntimeInput.select(store.session._connection)) == 1
    assert source.session_file.read_bytes() == before
