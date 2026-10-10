from __future__ import annotations

import json
import os
import pickle
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.compaction_errors import CompactionJournalError, CompactionJournalUnknownError
from agent_comms.compaction_journal import CompactionJournal
from agent_comms.compaction_records import SelectedSummarySource
from agent_comms.fresh_private_session import create_fresh_private_session
from agent_comms.native_pi import NativePiUnavailable, _read_private_file, _trusted_package
from agent_comms.thread_identity import ThreadIncarnation
from agent_comms.owner_compaction_settings import PiCompactionSettings
from agent_comms.pi_summary_payloads import SelectedModel
from selected_summary_cases import manual_summary_record


def test_explicit_fresh_session_has_durable_prewrite_inode(tmp_path: Path) -> None:
    session_dir = tmp_path / "native-sessions" / "owner"
    enrollment = create_fresh_private_session(session_dir, worktree=tmp_path)
    info = enrollment.path.lstat()
    assert (info.st_dev, info.st_ino, info.st_nlink, info.st_mode & 0o777) == (
        enrollment.file_identity.device,
        enrollment.file_identity.inode,
        1,
        0o600,
    )
    assert list(_read_private_file(enrollment.path)) == [
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


def test_explicit_selected_bootstrap_is_prewrite_durable_and_attested(tmp_path: Path) -> None:
    fresh = create_fresh_private_session(
        tmp_path / "native-sessions" / "a", worktree=tmp_path, selected_thinking_level="high"
    )
    rows = list(_read_private_file(fresh.path))
    assert len(rows) == 3
    assert rows[0]["id"] == fresh.session_id
    assert rows[0]["agentCommsSelectedFresh"] == {"schema": 1, "thinkingLevel": "high"}
    assert rows[1]["type"] == "model_change"
    assert (rows[1]["provider"], rows[1]["modelId"]) == ("openrouter", "z-ai/glm-5.3-flash")
    assert rows[2]["type"] == "thinking_level_change"
    assert rows[2]["thinkingLevel"] == "high"
    assert rows[2]["parentId"] == rows[1]["id"]
    assert rows[1]["parentId"] is None
    assert fresh.bootstrap_leaf_id == rows[2]["id"]
    fresh.verify_prewrite()
    with fresh.path.open("rb+") as stream:
        data = stream.read().replace(b'"thinkingLevel":"high"', b'"thinkingLevel":"low" ')
        stream.seek(0)
        stream.write(data)
        stream.truncate()
    with pytest.raises(NativePiUnavailable, match="bootstrap changed"):
        fresh.verify_saved_identity()


@pytest.mark.parametrize(
    "damage",
    [
        "none",
        "wrong_level",
        "wrong_parent",
        "extra_message",
        "partial",
        "duplicate_key",
        "fsync_failed",
        "revision_changed",
    ],
)
def test_selected_startup_requires_exact_two_durable_metadata_appends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    fresh = create_fresh_private_session(
        tmp_path / "native-sessions" / "a",
        worktree=tmp_path,
        selected_thinking_level="high",
    )
    model = {
        "type": "model_change",
        "id": "abc00001",
        "parentId": fresh.bootstrap_leaf_id,
        "timestamp": "2026-09-26T00:00:00.000Z",
        "provider": "openrouter",
        "modelId": "z-ai/glm-5.3-flash",
    }
    thinking = {
        "type": "thinking_level_change",
        "id": "abc00002",
        "parentId": model["id"],
        "timestamp": "2026-09-26T00:00:00.000Z",
        "thinkingLevel": "high",
    }
    if damage == "wrong_level":
        thinking["thinkingLevel"] = "low"
    elif damage == "wrong_parent":
        thinking["parentId"] = None
    rows = [model, thinking]
    if damage == "extra_message":
        rows.append(
            {
                "type": "message",
                "id": "abc00003",
                "parentId": thinking["id"],
                "timestamp": "2026-09-26T00:00:00.000Z",
                "message": {"role": "user"},
            }
        )
    with fresh.path.open("ab") as stream:
        for row in rows:
            encoded = json.dumps(row, separators=(",", ":"))
            if damage == "duplicate_key" and row is model:
                encoded = encoded.replace(
                    '"provider":"openrouter"',
                    '"provider":"openrouter","provider":"openrouter"',
                )
            stream.write(encoded.encode() + b"\n")
        if damage == "partial":
            stream.write(b"partial")
    if damage == "fsync_failed":
        from agent_comms import fresh_private_session as module

        monkeypatch.setattr(module.os, "fsync", lambda _: (_ for _ in ()).throw(OSError("EIO")))
    elif damage == "revision_changed":
        from agent_comms import fresh_private_session as module

        actual_fsync = module.os.fsync

        def append_during_fsync(descriptor: int) -> None:
            actual_fsync(descriptor)
            with fresh.path.open("ab") as stream:
                stream.write(b"{}\n")

        monkeypatch.setattr(module.os, "fsync", append_during_fsync)
    if damage == "none":
        revision = fresh.verify_selected_startup()
        assert revision.identity == fresh.file_identity
        assert revision.size > fresh.bootstrap_size
        with pytest.raises(NativePiUnavailable, match="earlier input"):
            fresh.verify_prewrite()
    else:
        with pytest.raises(NativePiUnavailable, match="startup"):
            fresh.verify_selected_startup()


@pytest.mark.parametrize("level", ["off", "medium", "", 0])
def test_selected_bootstrap_rejects_unsupported_level_before_creation(
    tmp_path: Path, level: object
) -> None:
    with pytest.raises(ValueError, match="supported selected thinking"):
        create_fresh_private_session(
            tmp_path / "native-sessions" / "a",
            worktree=tmp_path,
            selected_thinking_level=level,
        )
    assert not (tmp_path / "native-sessions").exists()


def test_optional_reviewed_copied_pi_reads_selected_bootstrap_level(tmp_path: Path) -> None:
    package = os.environ.get("AGENT_COMMS_TEST_COPIED_PIN")
    if package is None:
        pytest.skip("Explicit copied pinned Pi package path required; no download or provider")
    _trusted_package(Path(package))
    fresh = create_fresh_private_session(
        tmp_path / "native-sessions" / "a",
        worktree=tmp_path,
        selected_thinking_level="high",
    )
    module = (Path(package) / "dist/core/session-manager.js").as_uri()
    script = (
        f"import {{SessionManager,sessionEntryToContextMessages}} from {json.dumps(module)};"
        "const s=SessionManager.open(process.env.FRESH_FILE);"
        "console.log(JSON.stringify({id:s.getSessionId(),file:s.getSessionFile(),"
        "leaf:s.getLeafId(),context:{...s.entryStore.contextSettings(),messages:s.buildContextEntries().flatMap(sessionEntryToContextMessages).toArray()}}));"
    )
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=tmp_path,
        env={
            "HOME": str(tmp_path),
            "PATH": os.environ["PATH"],
            "PI_OFFLINE": "1",
            "FRESH_FILE": str(fresh.path),
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    parsed = json.loads(result.stdout)
    assert (parsed["id"], parsed["file"], parsed["leaf"]) == (
        fresh.session_id,
        str(fresh.path),
        fresh.bootstrap_leaf_id,
    )
    assert parsed["context"] == {
        "messages": [],
        "thinkingLevel": "high",
        "model": {"provider": "openrouter", "modelId": "z-ai/glm-5.3-flash"},
    }
    fresh.verify_prewrite()  # Native read cannot append to bootstrap.
    append_script = (
        f"import {{SessionManager,sessionEntryToContextMessages}} from {json.dumps(module)};"
        "const s=SessionManager.open(process.env.FRESH_FILE);"
        "const id=s.appendMessage({role:'user',content:'offline fixture',timestamp:1790460000000});"
        "console.log(JSON.stringify({id,leaf:s.getLeafId(),context:{...s.entryStore.contextSettings(),messages:s.buildContextEntries().flatMap(sessionEntryToContextMessages).toArray()}}));"
    )
    appended = subprocess.run(
        ["node", "--input-type=module", "-e", append_script],
        cwd=tmp_path,
        env={
            "HOME": str(tmp_path),
            "PATH": os.environ["PATH"],
            "PI_OFFLINE": "1",
            "FRESH_FILE": str(fresh.path),
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    new = json.loads(appended.stdout)
    assert new["leaf"] == new["id"]
    assert new["context"]["thinkingLevel"] == "high"
    assert new["context"]["model"] == parsed["context"]["model"]
    assert len(new["context"]["messages"]) == 1
    assert list(_read_private_file(fresh.path))[-1]["parentId"] == fresh.bootstrap_leaf_id
    fresh.verify_saved_identity()
    with pytest.raises(NativePiUnavailable, match="earlier input"):
        fresh.verify_prewrite()


def test_optional_reviewed_copied_pi_preserves_explicit_fresh_inode(tmp_path: Path) -> None:
    package = os.environ.get("AGENT_COMMS_TEST_COPIED_PIN")
    if package is None:
        pytest.skip("Explicit copied pinned Pi package path required; no download or provider")
    _trusted_package(Path(package))
    fresh = create_fresh_private_session(tmp_path / "native-sessions" / "a", worktree=tmp_path)
    module = (Path(package) / "dist/core/session-manager.js").as_uri()
    script = (
        f"import {{SessionManager,sessionEntryToContextMessages}} from {json.dumps(module)};"
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


@pytest.mark.parametrize("level", [None, "high"])
def test_uncertain_parent_fsync_does_not_return_enrollment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, level: str | None
) -> None:
    from agent_comms import fresh_private_session as module

    actual_fsync = module._fsync_directory

    def failed_session_fsync(path: Path) -> None:
        if path == tmp_path / "native-sessions" / "a":
            raise OSError("injected parent fsync uncertainty")
        actual_fsync(path)

    monkeypatch.setattr(module, "_fsync_directory", failed_session_fsync)
    with pytest.raises(NativePiUnavailable, match="durability UNKNOWN"):
        create_fresh_private_session(
            tmp_path / "native-sessions" / "a",
            worktree=tmp_path,
            selected_thinking_level=level,
        )
    # A visible header after uncertain fsync is NOT returned or enrolled.
    assert len(list((tmp_path / "native-sessions" / "a").glob("enrolled-*.jsonl"))) == 1


def _private_source(session) -> SelectedSummarySource:
    return manual_summary_record(
        session, "alice", selected=SelectedModel("openrouter", "z-ai/glm-5.3-flash", 1000),
        settings=PiCompactionSettings(100, 2000),
    )


def test_returned_enrollment_admits_only_exact_fresh_owner_without_raw_history(
    tmp_path: Path,
) -> None:
    root = tmp_path
    fresh = create_fresh_private_session(root / "native-sessions" / "alice-lookup", worktree=root)
    journal = CompactionJournal(root / "compaction-commits.sqlite3")
    with pytest.raises(CompactionJournalError, match="not covered by recorded inputs"):
        journal.summaries.reserve(str(fresh.path), _private_source(fresh.path))
    journal.private_inputs.enroll(
        fresh,
        incarnation=ThreadIncarnation("alice", 1.0),
        owner_lookup="alice-lookup",
        owner_generation=2,
        admission_generation=3,
    )
    with pytest.raises(CompactionJournalError, match="coverage differs"):
        journal.summaries.reserve(
            str(fresh.path),
            _private_source(fresh.path),
            fresh_session=fresh,
            admission_generation=4,
        )
    changed_owner = _private_source(fresh.path)
    changed_owner = replace(
        changed_owner, source=replace(
            changed_owner.source, incarnation=ThreadIncarnation("alice", 1.5)
        )
    )
    with pytest.raises(CompactionJournalError, match="coverage differs"):
        journal.summaries.reserve(
            str(fresh.path), changed_owner, fresh_session=fresh, admission_generation=3
        )
    next_turn = _private_source(fresh.path)
    attempt = journal.summaries.reserve(
        str(fresh.path), next_turn, fresh_session=fresh, admission_generation=3
    )
    assert journal.summaries.get(attempt).state.declared_name == "reserved"
    with pytest.raises(CompactionJournalError, match="blocks native input"):
        journal.private_inputs.reserve(fresh.path, "a" * 32)


def test_outward_hardlink_cannot_evade_private_floor(tmp_path: Path) -> None:
    fresh = create_fresh_private_session(
        tmp_path / "native-sessions" / "alice-lookup", worktree=tmp_path
    )
    outward = tmp_path / "outward.jsonl"
    os.link(fresh.path, outward)
    journal = CompactionJournal(tmp_path / "compaction-commits.sqlite3")
    with pytest.raises(CompactionJournalError, match="one private inode link"):
        journal.summaries.reserve(str(outward), _private_source(fresh.path))


def test_raw_unknown_even_on_returned_fresh_coverage_remains_selected_blocker(
    tmp_path: Path,
) -> None:
    fresh = create_fresh_private_session(
        tmp_path / "native-sessions" / "alice-lookup", worktree=tmp_path
    )
    journal = CompactionJournal(tmp_path / "compaction-commits.sqlite3")
    journal.private_inputs.enroll(
        fresh,
        incarnation=ThreadIncarnation("alice", 1.0),
        owner_lookup="alice-lookup",
        owner_generation=2,
        admission_generation=3,
    )
    journal.private_inputs.reserve(fresh.path, "b" * 32)
    with pytest.raises(CompactionJournalError, match="never replay"):
        journal.summaries.reserve(
            str(fresh.path),
            _private_source(fresh.path),
            fresh_session=fresh,
            admission_generation=3,
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
        journal.private_inputs.enroll(
            fresh,
            incarnation=ThreadIncarnation("alice", 1.0),
            owner_lookup="alice-lookup",
            owner_generation=2,
            admission_generation=3,
        )
    # The SQL row is visible on reopen, but no returned enrollment ACK exists.
    monkeypatch.setattr(module.os, "fsync", original_fsync)
    assert CompactionJournal(journal.path).path.exists()
    with pytest.raises(CompactionJournalError, match="coverage differs"):
        journal.summaries.reserve(
            str(fresh.path),
            _private_source(fresh.path),
            fresh_session=fresh,
            admission_generation=3,
        )


@pytest.mark.parametrize(
    "mutation, reason",
    [
        ({"model": None}, "model_changed"),
        ({"model": {"provider": "other", "id": "z-ai/glm-5.3-flash"}}, "model_changed"),
        ({"messageCount": 1}, "messages_present"),
        ({"pendingMessageCount": 1}, "pending_messages"),
        ({"isStreaming": True}, "streaming"),
        ({"isStreaming": None}, "streaming"),
        ({"isCompacting": True}, "compacting"),
        ({"isCompacting": None}, "compacting"),
    ],
)
def test_enrollment_refuses_nonempty_or_unattested_runtime(tmp_path, mutation, reason):
    from agent_comms.pi_payloads import StateData

    fresh = create_fresh_private_session(
        tmp_path / "sessions", worktree=tmp_path, selected_thinking_level="high"
    )
    observed = {
        "sessionId": fresh.session_id,
        "model": {"provider": "openrouter", "id": "z-ai/glm-5.3-flash"},
        "thinkingLevel": "high", "messageCount": 0, "pendingMessageCount": 0,
        "isStreaming": False, "isCompacting": False,
    }
    fresh.require_runtime(StateData.from_wire(observed))
    with pytest.raises(NativePiUnavailable, match=reason):
        fresh.require_runtime(StateData.from_wire(observed | mutation))
    fresh.verify_prewrite()
