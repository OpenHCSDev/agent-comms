"""The real carry validates old claim ids before changing stored version stamps."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from cutover.carry_wake_claim_turn_ids import WakeClaimTurnIds

from agent_comms.bus import Bus
from agent_comms.envelope_claim_transitions import ClaimTransition, WakeClaim, WakeClaims
from agent_comms.field_codec import ClaimId, FieldCodec, MalformedRecord, MessageId, TurnId
from agent_comms.messages import BusRow, Claimed


def old_claim_bus(root: Path, *, turn_id: str = "a" * 32) -> Path:
    root.mkdir(mode=0o700, parents=True)
    path = Bus.path_in(root)
    path.touch(mode=0o600)
    claim = WakeClaims(
        "owner", "epoch-1", 1, "message-1",
        claims=(str((root / "src" / "worker.py").resolve()),),
        generation=ClaimId("b" * 32),
        resource_scope=WakeClaim(
            owner_claimed_generation=1, operation_id=ClaimId("c" * 32), turn_id=TurnId("a" * 32),
            wire_root_id="d" * 32, source_seq=1, source_message_id=MessageId.mint("source-1"),
            wake_assignment_id="cohort-v1:" + "e" * 64, wake_revision=1,
            recipient_lookup="f" * 32, execution_id="execution-1",
            participant_generation=1, attempt_ordinal=1,
        ),
    )
    raw = FieldCodec.encode(claim, ClaimTransition)
    raw["resource_scope"].update(version=1, turn_id=turn_id)
    table = Claimed.table
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("PRAGMA journal_mode=WAL")
        BusRow.create(db)
        table.create(db)
        db.execute(table._sql(
            f'INSERT INTO "{table.declared_name}" ({{seq}}, {{claim_transition}}) VALUES (?, ?)'
        ), (1, FieldCodec.compact(raw)))
    return path


def stored_claim(path: Path) -> dict:
    table = Claimed.table
    with closing(sqlite3.connect(path)) as db:
        value, = db.execute(table._sql(
            f'SELECT {{claim_transition}} FROM "{table.declared_name}" WHERE {{seq}}=1'
        )).fetchone()
    return json.loads(value)


def test_carry_validates_and_upgrades_main_and_archived_claims(tmp_path):
    root = tmp_path / "root"
    paths = (old_claim_bus(root), old_claim_bus(root / "history" / "old"))
    step = WakeClaimTurnIds(root, root, tmp_path / "spill")
    before = tuple(stored_claim(path) for path in paths)
    assert step.carry(apply=False).changed == 2
    assert tuple(stored_claim(path) for path in paths) == before
    result = step.carry(apply=True)
    assert result.changed == 2
    assert set(result.rewritten) == {path.relative_to(root) for path in paths}
    for path, original in zip(paths, before, strict=True):
        expected = original.copy()
        expected["resource_scope"] = {**original["resource_scope"], "version": WakeClaim.version}
        current = stored_claim(path)
        assert current == expected
        decoded = FieldCodec.decode(ClaimTransition, current)
        assert type(decoded.resource_scope.turn_id) is TurnId
        assert decoded.resource_scope.version == WakeClaim.version
    assert step.carry(apply=False).changed == 0


@pytest.mark.parametrize("turn_id", ["not-a-turn-id", "A" * 32])
def test_carry_refuses_bad_id_without_writing_and_names_the_old_value(tmp_path, turn_id):
    root = tmp_path / "root"
    path = old_claim_bus(root, turn_id=turn_id)
    before = stored_claim(path)
    step = WakeClaimTurnIds(root, root, tmp_path / "spill")
    with pytest.raises(MalformedRecord) as failed:
        step.carry(apply=True)
    assert str(path) in "\n".join(failed.value.__notes__)
    assert repr(turn_id) in "\n".join(failed.value.__notes__)
    assert stored_claim(path) == before
