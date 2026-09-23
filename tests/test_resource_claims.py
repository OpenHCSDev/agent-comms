"""Isolated, default-off cooperative claim primitive; no MessageBus integration."""

from __future__ import annotations

import os
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context
from pathlib import Path

import pytest

from agent_comms.resource_claims import (
    ClaimError,
    ClaimNotOwned,
    ResourceClaims,
    UncertainClaim,
)


def _fixture(tmp_path: Path) -> tuple[ResourceClaims, Path, Path]:
    root = tmp_path / "work"
    root.mkdir()
    resource = root / "backend.py"
    resource.write_text("# code\n")
    claims = tmp_path / "claims"
    claims.mkdir(mode=0o700)
    return ResourceClaims(claims, root), root, resource


def _compete(claims: str, root: str, index: int) -> tuple[int, bool, str]:
    result = ResourceClaims(Path(claims), Path(root)).reserve_for_envelope(
        "backend.py", owner=f"worker-{index}", incarnation=f"run-{index}", message_id=f"msg-{index}"
    )
    return index, result.acquired, result.record.owner


def test_separate_processes_have_one_synchronous_winner(tmp_path: Path) -> None:
    store, root, _ = _fixture(tmp_path)
    with ProcessPoolExecutor(max_workers=5, mp_context=get_context("spawn")) as pool:
        attempts = list(pool.map(_compete, [str(store._dir)] * 5, [str(root)] * 5, range(5)))
    winners = [index for index, won, _ in attempts if won]
    assert len(winners) == 1
    assert {owner for _, _, owner in attempts} == {f"worker-{winners[0]}"}


def test_crash_after_exclusive_create_is_not_repaired(tmp_path: Path) -> None:
    store, _, resource = _fixture(tmp_path)
    path = store._path_for(str(resource.resolve()))
    died = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os,sys; os.open(sys.argv[1], os.O_CREAT|os.O_EXCL|os.O_WRONLY, 0o600); "
            "os._exit(9)",
            str(path),
        ],
        check=False,
    )
    assert died.returncode == 9
    with pytest.raises(UncertainClaim, match="incomplete"):
        store.reserve_for_envelope(resource, owner="a", incarnation="a1", message_id="msg")
    assert path.exists() and path.stat().st_size == 0
    with pytest.raises(UncertainClaim):
        store.release_for_envelope(resource, owner="a", incarnation="a1", generation="0" * 32)


def test_stale_owner_generation_cannot_release_a_new_owner(tmp_path: Path) -> None:
    store, _, resource = _fixture(tmp_path)
    first = store.reserve_for_envelope(resource, owner="a", incarnation="a1", message_id="m1")
    assert first.acquired and first.record.wire_seq is None
    with pytest.raises(ClaimNotOwned):
        store.release_for_envelope(
            resource, owner="a", incarnation="old", generation=first.record.generation
        )
    with pytest.raises(ClaimNotOwned):
        store.release_for_envelope(resource, owner="a", incarnation="a1", generation="0" * 32)
    store.release_for_envelope(
        resource, owner="a", incarnation="a1", generation=first.record.generation
    )
    second = store.reserve_for_envelope(resource, owner="b", incarnation="b1", message_id="m2")
    assert second.acquired and second.record.generation != first.record.generation
    with pytest.raises(ClaimNotOwned):
        store.release_for_envelope(
            resource, owner="a", incarnation="a1", generation=first.record.generation
        )
    assert not store.reserve_for_envelope(
        resource, owner="c", incarnation="c1", message_id="m3"
    ).acquired


def test_interrupted_release_stays_blocked(tmp_path: Path) -> None:
    store, _, resource = _fixture(tmp_path)
    first = store.reserve_for_envelope(resource, owner="a", incarnation="a1", message_id="m1")
    path = store._path_for(first.record.resource)
    os.rename(path, store._dir / f".released-{path.name}")  # Crash before directory fsync.
    with pytest.raises(UncertainClaim, match="interrupted release"):
        store.reserve_for_envelope(resource, owner="b", incarnation="b1", message_id="m2")


def test_file_paths_normalize_symlinks_but_not_different_case(tmp_path: Path) -> None:
    store, root, resource = _fixture(tmp_path)
    alias = root / "alias.py"
    alias.symlink_to(resource.name)
    first = store.reserve_for_envelope("./backend.py", owner="a", incarnation="a1", message_id="m1")
    second = store.reserve_for_envelope(alias, owner="b", incarnation="b1", message_id="m2")
    assert first.acquired and not second.acquired and second.record == first.record
    other = root / "BACKEND.PY"
    other.write_text("# distinct on a case-sensitive filesystem\n")
    if not os.path.samefile(resource, other):
        assert store.reserve_for_envelope(
            other, owner="b", incarnation="b1", message_id="m3"
        ).acquired
    outside = tmp_path / "outside.py"
    outside.write_text("# outside\n")
    (root / "escape.py").symlink_to(outside)
    with pytest.raises(ClaimError, match="inside"):
        store.reserve_for_envelope("escape.py", owner="b", incarnation="b1", message_id="m4")
    os.link(resource, root / "hardlink.py")
    with pytest.raises(ClaimError, match="singly-linked"):
        store.reserve_for_envelope("hardlink.py", owner="b", incarnation="b1", message_id="m5")


def test_failed_claim_directory_sync_does_not_allow_a_second_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _, resource = _fixture(tmp_path)

    def fail_sync(_: Path) -> None:
        raise OSError("injected directory fsync failure")

    with monkeypatch.context() as patch:
        patch.setattr("agent_comms.resource_claims._fsync_directory", fail_sync)
        with pytest.raises(OSError, match="injected"):
            store.reserve_for_envelope(resource, owner="a", incarnation="a1", message_id="m1")
    # Caller saw failure. The possibly committed record blocks competing work;
    # no fabricated claim/message success is returned for the original send.
    assert not store.reserve_for_envelope(
        resource, owner="b", incarnation="b1", message_id="m2"
    ).acquired


def test_root_and_record_must_be_private_and_complete(tmp_path: Path) -> None:
    store, root, resource = _fixture(tmp_path)
    store._dir.chmod(0o755)
    with pytest.raises(ClaimError, match="0700"):
        store.reserve_for_envelope(resource, owner="a", incarnation="a1", message_id="m1")
    store._dir.chmod(0o700)
    first = store.reserve_for_envelope(resource, owner="a", incarnation="a1", message_id="m1")
    record_path = store._path_for(first.record.resource)
    record_path.write_text('{"owner":"a"}')
    with pytest.raises(UncertainClaim):
        store.reserve_for_envelope(resource, owner="b", incarnation="b1", message_id="m2")
    with pytest.raises(ClaimError, match="existing file"):
        store.reserve_for_envelope(
            root / "not-created.py", owner="b", incarnation="b1", message_id="m3"
        )
