"""One-time terminal repair changes only its exact retained claim set."""

from dataclasses import replace
from pathlib import Path
import shutil

import pytest

from agent_comms.claim_admission import publish_selected_resource_claim
from agent_comms.coordination_errors import RecoveryBlocked
from agent_comms.envelope_claim_transitions import ExistingFileClaim
from agent_comms.private_bus_checkpoint import install_private_bus_checkpoint
from agent_comms.store_files import _store_lock
from agent_comms.wire_log import WireLog
from test_claim_bus_read import selected_turn, settle_failed
from tools.cutover.release_terminal_attempt_claims import apply, inspect


def test_terminal_claim_repair_refuses_live_attempt_then_releases_exact_terminal_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    with selected_turn() as (comms, store, selected, resource):
        first = publish_selected_resource_claim(
            comms, store, selected, "Alice", ExistingFileClaim(resource)
        )
        other = resource.parent / "other.py"
        other.write_text("unchanged\n")
        second = publish_selected_resource_claim(
            comms,
            store,
            replace(selected, operation_id="1" * 32),
            "Alice",
            ExistingFileClaim(other),
        )
        with pytest.raises(RecoveryBlocked, match="terminal attempt"):
            inspect(comms.root, "execution")

        # Reproduce the historical producer: terminal SQL committed before the
        # release behavior existed, leaving both exact generations on the bus.
        session_type = type(store.session)
        monkeypatch.setattr(session_type, "release_resources", lambda *_: None)
        settle_failed(store)
        monkeypatch.undo()
        assert store.snapshots.get("execution").attempt.lifecycle.terminal
        assert set(comms.bus.log.claim_projection()) == {first.resource, second.resource}

        # The first execution is deliberately against a copied store set. Its
        # bus inode changed, so author only the copy's derived checkpoint using
        # the existing private-checkpoint owner.
        copied = tmp_path / "copied-root"
        shutil.copytree(comms.root, copied)
        with _store_lock(copied / "wire"):
            log = WireLog(copied / "bus.jsonl")
            marker = log.read_metadata_unlocked()
            log.write_metadata_unlocked(
                replace(marker, checkpoint_version=None, checkpoint_seal=None)
            )
            (copied / "private_bus_checkpoint.sqlite3").unlink()
            install_private_bus_checkpoint(log)

        expected = inspect(copied, "execution")
        assert {(row.resource, row.generation) for row in expected.claims} == {
            (first.resource, first.generation),
            (second.resource, second.generation),
        }
        result = apply(copied, expected)
        assert result["released"] == 2
        assert result["sql_and_registry_unchanged"] is True
        assert dict(WireLog(copied / "bus.jsonl").claim_projection()) == {}
        assert set(comms.bus.log.claim_projection()) == {first.resource, second.resource}
