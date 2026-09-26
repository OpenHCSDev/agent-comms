from __future__ import annotations

import json
import os
import pickle
import subprocess
from pathlib import Path

import pytest

from agent_comms.compaction_journal import (
    CompactionJournal,
    CompactionJournalError,
    CompactionJournalUnknownError,
)
from agent_comms.fresh_private_session import create_fresh_private_session
from agent_comms.native_pi import NativePiUnavailable, _read_private_file, _trusted_package


def test_explicit_fresh_session_has_durable_prewrite_inode(tmp_path: Path) -> None:
    session_dir = tmp_path / "native-sessions" / "owner"
    enrollment = create_fresh_private_session(session_dir, worktree=tmp_path)
    info = enrollment.path.lstat()
    assert (info.st_dev, info.st_ino, info.st_nlink, info.st_mode & 0o777) == (
        enrollment.device,
        enrollment.inode,
        1,
        0o600,
    )
    assert _read_private_file(enrollment.path) == [
        {
            "type": "session",
            "version": 3,
            "id": enrollment.session_id,
            "timestamp": json.loads(enrollment.path.read_text())["timestamp"],
            "cwd": str(tmp_path),
        }
    ]
    assert enrollment.path.parent == session_dir
    enrollment.verify_prewrite()
    with pytest.raises(TypeError, match="cannot cross"):
        pickle.dumps(enrollment)
    with enrollment.path.open("ab") as stream:
        stream.write(b"{}\n")
    enrollment.verify_saved_identity()  # Pi may append later without changing the inode.
    with pytest.raises(NativePiUnavailable, match="earlier input"):
        enrollment.verify_prewrite()


def test_optional_reviewed_copied_pi_preserves_explicit_fresh_inode(tmp_path: Path) -> None:
    package = os.environ.get("AGENT_COMMS_TEST_COPIED_PIN")
    if package is None:
        pytest.skip("Explicit copied pinned Pi package path required; no download or provider")
    _trusted_package(Path(package))
    fresh = create_fresh_private_session(tmp_path / "native-sessions" / "a", worktree=tmp_path)
    module = (Path(package) / "dist/core/session-manager.js").as_uri()
    script = (
        f"import {{SessionManager}} from {json.dumps(module)};"
        "const s=SessionManager.open(process.env.FRESH_FILE);"
        "if(s.getSessionId()!==process.env.FRESH_ID)throw Error('rebound session');"
        "s.appendMessage({role:'user',content:'provider-free fixture',timestamp:Date.now()});"
        "console.log(JSON.stringify({id:s.getSessionId(),file:s.getSessionFile()}));"
    )
    outcome = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=tmp_path,
        env={
            "HOME": str(tmp_path),
            "PATH": os.environ["PATH"],
            "PI_OFFLINE": "1",
            "FRESH_FILE": str(fresh.path),
            "FRESH_ID": fresh.session_id,
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    assert json.loads(outcome.stdout) == {"id": fresh.session_id, "file": str(fresh.path)}
    fresh.verify_saved_identity()
    assert len(fresh.path.read_text().splitlines()) == 2


def test_existing_file_and_hardlink_never_gain_fresh_authority(tmp_path: Path) -> None:
    enrollment = create_fresh_private_session(tmp_path / "native-sessions" / "a", worktree=tmp_path)
    alias = enrollment.path.with_suffix(".alias")
    os.link(enrollment.path, alias)
    with pytest.raises(NativePiUnavailable, match="inode changed"):
        enrollment.verify_prewrite()
    alias.unlink()
    enrollment.path.unlink()
    enrollment.path.write_text("{}\n")
    with pytest.raises(NativePiUnavailable, match="inode changed"):
        enrollment.verify_prewrite()
    second = create_fresh_private_session(enrollment.path.parent, worktree=tmp_path)
    assert second.path != enrollment.path
    assert second.session_id != enrollment.session_id


def test_uncertain_parent_fsync_does_not_return_enrollment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_comms import fresh_private_session as module

    actual_fsync = module._fsync_directory

    def failed_session_fsync(path: Path) -> None:
        if path == tmp_path / "native-sessions" / "a":
            raise OSError("injected parent fsync uncertainty")
        actual_fsync(path)

    monkeypatch.setattr(module, "_fsync_directory", failed_session_fsync)
    with pytest.raises(NativePiUnavailable, match="durability UNKNOWN"):
        create_fresh_private_session(tmp_path / "native-sessions" / "a", worktree=tmp_path)
    # A visible header after uncertain fsync is NOT returned or enrolled.
    assert len(list((tmp_path / "native-sessions" / "a").glob("enrolled-*.jsonl"))) == 1


def _private_source() -> dict:
    return {
        "source": {
            "ownerName": "alice",
            "ownerCreatedAt": "0x1.0000000000000p+0",
            "ownerPid": os.getpid(),
            "admissionGeneration": 2,
        },
        "selected": {"provider": "openrouter", "modelId": "z-ai/glm-5.3-flash"},
        "settings": {"keepRecentTokens": 2000},
    }


def test_returned_enrollment_admits_only_exact_fresh_owner_without_raw_history(
    tmp_path: Path,
) -> None:
    root = tmp_path
    fresh = create_fresh_private_session(root / "native-sessions" / "alice-lookup", worktree=root)
    journal = CompactionJournal(root / "compaction-commits.sqlite3")
    with pytest.raises(CompactionJournalError, match="coverage floor"):
        journal.reserve_selected_summary(str(fresh.path), _private_source())
    journal.enroll_fresh_private_session(
        fresh,
        owner_name="alice",
        owner_created_at="0x1.0000000000000p+0",
        owner_lookup="alice-lookup",
        owner_generation=2,
        admission_epoch=3,
    )
    with pytest.raises(CompactionJournalError, match="coverage differs"):
        journal.reserve_selected_summary(
            str(fresh.path), _private_source(), fresh_session=fresh, admission_epoch=4
        )
    changed_owner = _private_source()
    changed_owner["source"]["ownerCreatedAt"] = "0x1.8000000000000p+0"
    with pytest.raises(CompactionJournalError, match="coverage differs"):
        journal.reserve_selected_summary(
            str(fresh.path), changed_owner, fresh_session=fresh, admission_epoch=3
        )
    next_turn = _private_source()
    next_turn["source"]["admissionGeneration"] = 9  # same incarnation, later owner turn
    attempt = journal.reserve_selected_summary(
        str(fresh.path), next_turn, fresh_session=fresh, admission_epoch=3
    )
    assert journal.selected_summary(attempt).status == "reserved"
    with pytest.raises(CompactionJournalError, match="blocks native input"):
        journal.reserve_private_raw_input(fresh.path, "a" * 32)


def test_outward_hardlink_cannot_evade_private_floor(tmp_path: Path) -> None:
    fresh = create_fresh_private_session(
        tmp_path / "native-sessions" / "alice-lookup", worktree=tmp_path
    )
    outward = tmp_path / "outward.jsonl"
    os.link(fresh.path, outward)
    journal = CompactionJournal(tmp_path / "compaction-commits.sqlite3")
    with pytest.raises(CompactionJournalError, match="one private inode link"):
        journal.reserve_selected_summary(str(outward), _private_source())


def test_raw_unknown_even_on_returned_fresh_coverage_remains_selected_blocker(
    tmp_path: Path,
) -> None:
    fresh = create_fresh_private_session(
        tmp_path / "native-sessions" / "alice-lookup", worktree=tmp_path
    )
    journal = CompactionJournal(tmp_path / "compaction-commits.sqlite3")
    journal.enroll_fresh_private_session(
        fresh,
        owner_name="alice",
        owner_created_at="0x1.0000000000000p+0",
        owner_lookup="alice-lookup",
        owner_generation=2,
        admission_epoch=3,
    )
    journal.reserve_private_raw_input(fresh.path, "b" * 32)
    with pytest.raises(CompactionJournalError, match="never replay"):
        journal.reserve_selected_summary(
            str(fresh.path), _private_source(), fresh_session=fresh, admission_epoch=3
        )


def test_visible_enrollment_after_parent_fsync_unknown_does_not_authorize_selected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_comms import compaction_journal as module

    fresh = create_fresh_private_session(
        tmp_path / "native-sessions" / "alice-lookup", worktree=tmp_path
    )
    journal = CompactionJournal(tmp_path / "compaction-commits.sqlite3")
    original_fsync = module.os.fsync
    fail = True

    def uncertain_fsync(fd: int) -> None:
        nonlocal fail
        if fail and os.fstat(fd).st_ino == tmp_path.stat().st_ino:
            fail = False
            raise OSError("injected post-COMMIT directory fsync")
        original_fsync(fd)

    monkeypatch.setattr(module.os, "fsync", uncertain_fsync)
    with pytest.raises(CompactionJournalUnknownError, match="durability UNKNOWN"):
        journal.enroll_fresh_private_session(
            fresh,
            owner_name="alice",
            owner_created_at="0x1.0000000000000p+0",
            owner_lookup="alice-lookup",
            owner_generation=2,
            admission_epoch=3,
        )
    # The SQL row is visible on reopen, but no returned enrollment ACK exists.
    monkeypatch.setattr(module.os, "fsync", original_fsync)
    assert CompactionJournal(journal.path).path.exists()
    with pytest.raises(CompactionJournalError, match="coverage differs"):
        journal.reserve_selected_summary(
            str(fresh.path), _private_source(), fresh_session=fresh, admission_epoch=3
        )
