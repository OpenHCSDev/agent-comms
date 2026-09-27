"""Provider-free native source snapshot; never a model/terminal receipt."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from agent_comms.fresh_private_session import create_fresh_private_session
from agent_comms.selected_source_snapshot import (
    SelectedSourceSnapshotError,
    capture_selected_source_snapshot,
)


def _populated_source(tmp_path: Path, *, thinking_level: str | None = "high"):
    fresh = create_fresh_private_session(
        tmp_path / "native-sessions" / "recipient",
        worktree=tmp_path,
        selected_thinking_level=thinking_level,
    )
    entries = []
    previous = fresh.bootstrap_leaf_id
    for turn in range(2):
        for role in ("user", "assistant"):
            entry_id = f"{turn * 2 + int(role == 'assistant'):08x}"
            message = (
                {
                    "role": "user",
                    "content": ("earlier context " * 50) if turn == 0 else "latest " * 30,
                    "timestamp": 1790460000000 + turn,
                }
                if role == "user"
                else {
                    "role": "assistant",
                    "content": [{"type": "text", "text": ("older " * 50) if turn == 0 else "OK"}],
                    "provider": "openrouter",
                    "model": "z-ai/glm-5.3-flash",
                    "api": "openai-completions",
                    "stopReason": "stop",
                    "timestamp": 1790460000000 + turn,
                }
            )
            entries.append(
                {
                    "type": "message",
                    "id": entry_id,
                    "parentId": previous,
                    "timestamp": "2026-09-26T00:00:00.000Z",
                    "message": message,
                }
            )
            previous = entry_id
    with fresh.path.open("a", encoding="utf-8") as stream:
        for entry in entries:
            stream.write(json.dumps(entry, separators=(",", ":")) + "\n")
    proof = Path(str(fresh.path) + ".input-proof")
    proof.write_text(
        json.dumps(
            {
                "schema": 1,
                "type": "context_committed",
                "sessionId": fresh.session_id,
                "inputId": "a" * 32,
                "sessionEntryId": entries[0]["id"],
                "requestGeneration": 1,
                "llmContextDigest": "b" * 64,
            }
        )
        + "\n"
    )
    proof.chmod(0o600)
    return fresh, proof


def test_optional_pinned_source_snapshot_binds_two_inodes_and_exact_context(
    tmp_path: Path,
) -> None:
    package = os.environ.get("AGENT_COMMS_TEST_COPIED_PIN")
    if package is None:
        pytest.skip("Explicit copied pinned Pi package required; no download/provider")
    fresh, proof = _populated_source(tmp_path)
    outcome = capture_selected_source_snapshot(Path(package), fresh, keep_recent_tokens=20)
    assert outcome is not None
    assert outcome.source["sessionId"] == fresh.session_id
    assert outcome.source["leafId"]
    assert outcome.source["firstKeptEntryId"]
    assert outcome.source["revision"].split(":")[:2] == [str(fresh.device), str(fresh.inode)]
    assert outcome.source["contextDigest"] == hashlib.sha256(outcome.context_bytes).hexdigest()
    assert outcome.source["selectedProvider"] == "openrouter"
    assert outcome.source["selectedModelId"] == "z-ai/glm-5.3-flash"
    assert outcome.source["selectedThinkingLevel"] == "high"
    assert json.loads(outcome.context_bytes)["thinkingLevel"] == "high"
    assert outcome.sidecar_revision[:2] == (proof.stat().st_dev, proof.stat().st_ino)
    assert len(json.loads(outcome.context_bytes)["messages"]) == 4
    # Even a prior returned snapshot is only an observation; a later source
    # change cannot be re-captured under the same old revision.
    proof.unlink()
    with pytest.raises(SelectedSourceSnapshotError, match="unavailable"):
        capture_selected_source_snapshot(Path(package), fresh, keep_recent_tokens=20)


def test_optional_pinned_off_source_is_not_silently_clamped(
    tmp_path: Path,
) -> None:
    package = os.environ.get("AGENT_COMMS_TEST_COPIED_PIN")
    if package is None:
        pytest.skip("Explicit copied pinned Pi package required; no download/provider")
    fresh, _proof = _populated_source(tmp_path, thinking_level=None)
    with pytest.raises(SelectedSourceSnapshotError, match="not explicitly supported"):
        capture_selected_source_snapshot(Path(package), fresh, keep_recent_tokens=20)


def test_sidecar_change_during_provider_free_capture_refuses_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_comms import selected_source_snapshot as module

    fresh, proof = _populated_source(tmp_path)
    package = tmp_path / "fixture-package"
    compaction = package / "dist/core/compaction/compaction.js"
    compaction.parent.mkdir(parents=True)
    compaction.write_bytes(b"fixture only; never executed")
    monkeypatch.setattr(module, "_trusted_package", lambda _: None)
    monkeypatch.setattr(
        module, "_COMPACTION_SHA256", hashlib.sha256(compaction.read_bytes()).hexdigest()
    )
    monkeypatch.setattr(module.shutil, "which", lambda _: "/fake/node")

    def changed_proof(*_args, **_kwargs):
        with proof.open("ab") as stream:
            stream.write(b"{}\n")
        return subprocess.CompletedProcess([], 0, b'{"status":"skip","reason":"no_cut"}\n', b"")

    monkeypatch.setattr(module.subprocess, "run", changed_proof)
    with pytest.raises(SelectedSourceSnapshotError, match="changed during native snapshot"):
        capture_selected_source_snapshot(package, fresh, keep_recent_tokens=20)


def test_missing_or_changed_sidecar_rejected_before_any_pi_source_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_comms import selected_source_snapshot as module

    fresh, proof = _populated_source(tmp_path)
    monkeypatch.setattr(module, "_trusted_package", lambda _: None)
    # Package preparation hash must be checked even when an apparent input
    # sidecar exists; a fake source module cannot establish canonical context.
    with pytest.raises(SelectedSourceSnapshotError, match="cannot be safely captured"):
        capture_selected_source_snapshot(tmp_path, fresh, keep_recent_tokens=20)
    proof.unlink()
    with pytest.raises(SelectedSourceSnapshotError, match="cannot be safely captured"):
        capture_selected_source_snapshot(tmp_path, fresh, keep_recent_tokens=20)
