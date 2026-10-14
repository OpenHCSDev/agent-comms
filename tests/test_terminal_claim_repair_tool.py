"""One-time terminal repair changes only its exact retained claim set."""

from dataclasses import replace
import hashlib
from pathlib import Path
import shutil

import pytest

from agent_comms.checkpoint_seals import PendingSeal
from agent_comms.claim_admission import publish_selected_resource_claim
from agent_comms.coordination_errors import RecoveryBlocked
from agent_comms.envelope_claim_transitions import ExistingFileClaim
from agent_comms.private_bus_checkpoint import install_private_bus_checkpoint
from agent_comms.store_files import _store_lock
from agent_comms.wire_log import WireLog
from test_claim_bus_read import selected_turn, settle_failed
from tools.cutover.release_terminal_attempt_claims import plan, repair


def _hashes(root: Path) -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.iterdir()
        if path.is_file()
    }


def test_terminal_claim_repair_refuses_before_recovery_then_releases_exact_set(
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
            plan(comms.root, "execution")

        # Reproduce the historical producer: terminal SQL committed before the
        # release behavior existed, leaving both exact generations on the bus.
        session_type = type(store.session)
        monkeypatch.setattr(session_type, "release_resources", lambda *_: None)
        settle_failed(store)
        monkeypatch.undo()
        assert store.snapshots.get("execution").attempt.lifecycle.terminal
        assert set(comms.bus.log.claim_projection()) == {first.resource, second.resource}

        # A copied synthetic store must get its own checkpoint because the bus
        # inode changed. Production roots are never resealed by this repair.
        def copied_root(name: str) -> Path:
            copied = tmp_path / name
            shutil.copytree(comms.root, copied)
            with _store_lock(copied / "wire"):
                log = WireLog(copied / "bus.jsonl")
                marker = log.read_metadata_unlocked()
                log.write_metadata_unlocked(
                    replace(marker, checkpoint_version=None, checkpoint_seal=None)
                )
                (copied / "private_bus_checkpoint.sqlite3").unlink()
                install_private_bus_checkpoint(log)
            return copied

        refusal_root = copied_root("refusal-root")
        pending_root = copied_root("pending-root")
        apply_root = copied_root("apply-root")
        expected = plan(refusal_root, "execution")
        assert {(row.resource, row.generation) for row in expected.claims} == {
            (first.resource, first.generation),
            (second.resource, second.generation),
        }
        stale = replace(expected, claims=expected.claims[:1])

        before = _hashes(refusal_root)
        with pytest.raises(RecoveryBlocked, match="complete release set"):
            repair(refusal_root, "execution", stale)
        assert _hashes(refusal_root) == before

        # Even a checkpoint that an exclusive opener could recover remains
        # byte-for-byte unchanged when the submitted release set is stale.
        pending_log = WireLog(pending_root / "bus.jsonl")
        with pending_log.locked() as lock:
            witness = lock.certified_read().witness
            marker = pending_log._private_marker_unlocked()
            marker.seal_with(PendingSeal.capture(
                witness, witness, pending_root / "private_bus_checkpoint.sqlite3"
            ))
            pending_log.write_metadata_unlocked(marker)
        before = _hashes(pending_root)
        with pytest.raises(RecoveryBlocked, match="read-only preflight"):
            repair(pending_root, "execution", stale)
        assert _hashes(pending_root) == before

        actual, result = repair(apply_root, "execution", expected)
        assert actual == expected
        assert result["released"] == 2
        assert result["protected_stores_unchanged"] is True
        assert dict(WireLog(apply_root / "bus.jsonl").claim_projection()) == {}
        assert set(comms.bus.log.claim_projection()) == {first.resource, second.resource}
