"""Provider-free native source snapshot; never a model/terminal receipt."""

from __future__ import annotations

import base64
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


@pytest.fixture
def fixture_utils_pin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake bytes test only; never import an unpinned copied Pi module."""
    from agent_comms import selected_source_snapshot as module

    utils = tmp_path / "fixture-package" / "dist/core/compaction/utils.js"
    utils.parent.mkdir(parents=True, exist_ok=True)
    utils.write_bytes(b"fixture utilities only; never executed")
    monkeypatch.setattr(
        module, "_COMPACTION_UTILS_SHA256", hashlib.sha256(utils.read_bytes()).hexdigest()
    )


def _require_old_reviewed_pin(package: str) -> None:
    from agent_comms.native_pi import NativePiUnavailable, _trusted_package
    from agent_comms.selected_source_snapshot import _COMPACTION_SHA256

    compaction = Path(package) / "dist/core/compaction/compaction.js"
    if hashlib.sha256(compaction.read_bytes()).hexdigest() != _COMPACTION_SHA256:
        pytest.skip("Live copied preparation differs; it must remain denied before Node")
    try:
        _trusted_package(Path(package))
    except NativePiUnavailable:
        pytest.skip("Old compaction pin alone does not establish trusted package closure")


def test_live_copied_source_mismatch_denies_before_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = os.environ.get("AGENT_COMMS_TEST_COPIED_PIN")
    if package is None:
        pytest.skip("Explicit copied pinned Pi package required; no download/provider")
    from agent_comms import selected_source_snapshot as module

    compaction = Path(package) / "dist/core/compaction/compaction.js"
    if hashlib.sha256(compaction.read_bytes()).hexdigest() == module._COMPACTION_SHA256:
        pytest.skip("This copied source is already the old reviewed pin")
    fresh, _proof = _populated_source(tmp_path)

    def forbidden_node(*_args, **_kwargs):
        raise AssertionError("Live copied source launched Node despite pin mismatch")

    monkeypatch.setattr(module.subprocess, "run", forbidden_node)
    with pytest.raises(SelectedSourceSnapshotError, match="preparation module differs"):
        capture_selected_source_snapshot(Path(package), fresh, keep_recent_tokens=20)


def test_unpinned_file_operations_module_denies_before_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_comms import selected_source_snapshot as module

    fresh, _proof = _populated_source(tmp_path)
    package = tmp_path / "fixture-package"
    compaction = package / "dist/core/compaction/compaction.js"
    compaction.parent.mkdir(parents=True)
    compaction.write_bytes(b"fixture only; never executed")
    utils = compaction.with_name("utils.js")
    utils.write_bytes(b"unreviewed helper; never executed")
    monkeypatch.setattr(module, "_trusted_package", lambda _: None)
    monkeypatch.setattr(
        module, "_COMPACTION_SHA256", hashlib.sha256(compaction.read_bytes()).hexdigest()
    )
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("Unpinned fileOps helper launched Node"),
    )
    with pytest.raises(SelectedSourceSnapshotError, match="file-operations module differs"):
        capture_selected_source_snapshot(package, fresh, keep_recent_tokens=20)


def test_optional_pinned_source_snapshot_binds_two_inodes_and_exact_context(
    tmp_path: Path,
) -> None:
    package = os.environ.get("AGENT_COMMS_TEST_COPIED_PIN")
    if package is None:
        pytest.skip("Explicit copied pinned Pi package required; no download/provider")
    _require_old_reviewed_pin(package)
    fresh, proof = _populated_source(tmp_path)
    outcome = capture_selected_source_snapshot(Path(package), fresh, keep_recent_tokens=20)
    assert outcome is not None
    assert outcome.source["sessionId"] == fresh.session_id
    assert outcome.source["leafId"]
    assert outcome.source["firstKeptEntryId"]
    assert outcome.source["revision"].split(":")[:2] == [str(fresh.device), str(fresh.inode)]
    assert outcome.source["contextDigest"] == hashlib.sha256(outcome.context_bytes).hexdigest()
    assert (
        outcome.source["preparedHistoryDigest"]
        == hashlib.sha256(outcome.prepared_history_bytes).hexdigest()
    )
    assert outcome.source["sourceDigest"] == hashlib.sha256(fresh.path.read_bytes()).hexdigest()
    assert outcome.source["sidecarDigest"] == hashlib.sha256(proof.read_bytes()).hexdigest()
    assert json.loads(outcome.prepared_history_bytes)["messagesToSummarize"]
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
    _require_old_reviewed_pin(package)
    fresh, _proof = _populated_source(tmp_path, thinking_level=None)
    with pytest.raises(SelectedSourceSnapshotError, match="not explicitly supported"):
        capture_selected_source_snapshot(Path(package), fresh, keep_recent_tokens=20)


@pytest.mark.usefixtures("fixture_utils_pin")
def test_sidecar_change_during_provider_free_capture_refuses_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_comms import selected_source_snapshot as module

    fresh, proof = _populated_source(tmp_path)
    package = tmp_path / "fixture-package"
    compaction = package / "dist/core/compaction/compaction.js"
    compaction.parent.mkdir(parents=True, exist_ok=True)
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


@pytest.mark.usefixtures("fixture_utils_pin")
def test_fake_split_cut_declines_without_any_prepared_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_comms import selected_source_snapshot as module

    fresh, _proof = _populated_source(tmp_path)
    package = tmp_path / "fixture-package"
    compaction = package / "dist/core/compaction/compaction.js"
    compaction.parent.mkdir(parents=True, exist_ok=True)
    compaction.write_bytes(b"fixture only; never executed")
    monkeypatch.setattr(module, "_trusted_package", lambda _: None)
    monkeypatch.setattr(
        module, "_COMPACTION_SHA256", hashlib.sha256(compaction.read_bytes()).hexdigest()
    )

    def split(*_args, **kwargs):
        assert kwargs["input"] == fresh.path.read_bytes()
        return subprocess.CompletedProcess([], 0, b'{"status":"skip","reason":"split_turn"}\n', b"")

    monkeypatch.setattr(module.subprocess, "run", split)
    assert capture_selected_source_snapshot(package, fresh, keep_recent_tokens=20) is None


@pytest.mark.usefixtures("fixture_utils_pin")
def test_same_length_sidecar_overwrite_refuses_fake_node_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_comms import selected_source_snapshot as module

    fresh, proof = _populated_source(tmp_path)
    package = tmp_path / "fixture-package"
    compaction = package / "dist/core/compaction/compaction.js"
    compaction.parent.mkdir(parents=True, exist_ok=True)
    compaction.write_bytes(b"fixture only; never executed")
    monkeypatch.setattr(module, "_trusted_package", lambda _: None)
    monkeypatch.setattr(
        module, "_COMPACTION_SHA256", hashlib.sha256(compaction.read_bytes()).hexdigest()
    )

    def changed_proof(*_args, **kwargs):
        assert kwargs["input"] == fresh.path.read_bytes()
        before = proof.read_bytes()
        proof.write_bytes(before[:-2] + b"Z\n")
        assert len(proof.read_bytes()) == len(before)
        return subprocess.CompletedProcess([], 0, b'{"status":"skip","reason":"no_cut"}\n', b"")

    monkeypatch.setattr(module.subprocess, "run", changed_proof)
    with pytest.raises(SelectedSourceSnapshotError, match="changed during native snapshot"):
        capture_selected_source_snapshot(package, fresh, keep_recent_tokens=20)


@pytest.mark.usefixtures("fixture_utils_pin")
def test_same_length_source_overwrite_rejected_even_if_revision_clock_collides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_comms import selected_source_snapshot as module

    fresh, _proof = _populated_source(tmp_path)
    package = tmp_path / "fixture-package"
    compaction = package / "dist/core/compaction/compaction.js"
    compaction.parent.mkdir(parents=True, exist_ok=True)
    compaction.write_bytes(b"fixture only; never executed")
    monkeypatch.setattr(module, "_trusted_package", lambda _: None)
    monkeypatch.setattr(
        module, "_COMPACTION_SHA256", hashlib.sha256(compaction.read_bytes()).hexdigest()
    )
    real_revision = module._revision
    monkeypatch.setattr(module, "_revision", lambda st: (*real_revision(st)[:3], 0, 0))

    def changed_source(*_args, **kwargs):
        data = kwargs["input"]
        fresh.path.write_bytes(data[:-2] + b"Z\n")
        assert len(fresh.path.read_bytes()) == len(data)
        return subprocess.CompletedProcess([], 0, b'{"status":"skip","reason":"no_cut"}\n', b"")

    monkeypatch.setattr(module.subprocess, "run", changed_source)
    with pytest.raises(SelectedSourceSnapshotError, match="changed during native snapshot"):
        capture_selected_source_snapshot(package, fresh, keep_recent_tokens=20)


@pytest.mark.usefixtures("fixture_utils_pin")
def test_replaced_source_path_refuses_fake_node_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_comms import selected_source_snapshot as module

    fresh, _proof = _populated_source(tmp_path)
    package = tmp_path / "fixture-package"
    compaction = package / "dist/core/compaction/compaction.js"
    compaction.parent.mkdir(parents=True, exist_ok=True)
    compaction.write_bytes(b"fixture only; never executed")
    monkeypatch.setattr(module, "_trusted_package", lambda _: None)
    monkeypatch.setattr(
        module, "_COMPACTION_SHA256", hashlib.sha256(compaction.read_bytes()).hexdigest()
    )

    def replaced(*_args, **kwargs):
        captured = kwargs["input"]
        assert captured == fresh.path.read_bytes()
        fresh.path.rename(fresh.path.with_suffix(".old"))
        fresh.path.write_bytes(captured)
        fresh.path.chmod(0o600)
        return subprocess.CompletedProcess([], 0, b'{"status":"skip","reason":"no_cut"}\n', b"")

    monkeypatch.setattr(module.subprocess, "run", replaced)
    with pytest.raises(SelectedSourceSnapshotError, match="changed during native snapshot"):
        capture_selected_source_snapshot(package, fresh, keep_recent_tokens=20)


@pytest.mark.usefixtures("fixture_utils_pin")
def test_tampered_prepared_history_digest_refuses_fake_node_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_comms import selected_source_snapshot as module

    fresh, _proof = _populated_source(tmp_path)
    package = tmp_path / "fixture-package"
    compaction = package / "dist/core/compaction/compaction.js"
    compaction.parent.mkdir(parents=True, exist_ok=True)
    compaction.write_bytes(b"fixture only; never executed")
    monkeypatch.setattr(module, "_trusted_package", lambda _: None)
    monkeypatch.setattr(
        module, "_COMPACTION_SHA256", hashlib.sha256(compaction.read_bytes()).hexdigest()
    )
    context = json.dumps(
        {
            "messages": [{"role": "user", "content": "fixture"}],
            "model": {"provider": "openrouter", "modelId": "z-ai/glm-5.3-flash"},
            "thinkingLevel": "high",
        }
    ).encode()
    history = json.dumps(
        {
            "messagesToSummarize": [{"role": "user", "content": "fixture"}],
            "previousSummary": None,
            "turnPrefixMessages": [],
            "firstKeptEntryId": "kept",
        }
    ).encode()

    def tampered(*_args, **kwargs):
        assert kwargs["input"] == fresh.path.read_bytes()
        payload = {
            "status": "ready",
            "sessionId": fresh.session_id,
            "leafId": "leaf",
            "firstKeptEntryId": "kept",
            "contextDigest": hashlib.sha256(context).hexdigest(),
            "contextBase64": base64.b64encode(context).decode(),
            "preparedHistoryDigest": "a" * 64,
            "preparedHistoryBase64": base64.b64encode(history).decode(),
            "fileOpsDigest": hashlib.sha256(b'{"readFiles":[],"modifiedFiles":[]}').hexdigest(),
            "fileOpsByteLength": len(b'{"readFiles":[],"modifiedFiles":[]}'),
            "fileOpsBase64": base64.b64encode(b'{"readFiles":[],"modifiedFiles":[]}').decode(),
            "footerDigest": hashlib.sha256(b"").hexdigest(),
            "footerByteLength": 0,
            "footerBase64": "",
        }
        return subprocess.CompletedProcess([], 0, (json.dumps(payload) + "\n").encode(), b"")

    monkeypatch.setattr(module.subprocess, "run", tampered)
    with pytest.raises(SelectedSourceSnapshotError, match="prepared history differs"):
        capture_selected_source_snapshot(package, fresh, keep_recent_tokens=20)


@pytest.mark.usefixtures("fixture_utils_pin")
def test_file_ops_and_footer_must_match_one_prepared_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_comms.selected_source_snapshot import _read_snapshot_result

    fresh, _proof = _populated_source(tmp_path)
    context = json.dumps(
        {
            "messages": [{"role": "user", "content": "fixture"}],
            "model": {"provider": "openrouter", "modelId": "z-ai/glm-5.3-flash"},
            "thinkingLevel": "high",
        },
        separators=(",", ":"),
    ).encode()
    history = json.dumps(
        {
            "messagesToSummarize": [{"role": "user", "content": "fixture"}],
            "previousSummary": None,
            "turnPrefixMessages": [],
            "firstKeptEntryId": "kept",
        },
        separators=(",", ":"),
    ).encode()
    file_ops = b'{"readFiles":["prior.txt"],"modifiedFiles":[]}'
    footer = b"\n\n<read-files>\nprior.txt\n</read-files>"
    payload = {
        "status": "ready",
        "sessionId": fresh.session_id,
        "leafId": "leaf",
        "firstKeptEntryId": "kept",
        "contextDigest": hashlib.sha256(context).hexdigest(),
        "contextBase64": base64.b64encode(context).decode(),
        "preparedHistoryDigest": hashlib.sha256(history).hexdigest(),
        "preparedHistoryBase64": base64.b64encode(history).decode(),
        "fileOpsDigest": hashlib.sha256(file_ops).hexdigest(),
        "fileOpsByteLength": len(file_ops),
        "fileOpsBase64": base64.b64encode(file_ops).decode(),
        "footerDigest": hashlib.sha256(footer).hexdigest(),
        "footerByteLength": len(footer),
        "footerBase64": base64.b64encode(footer).decode(),
    }

    def parse() -> dict:
        return _read_snapshot_result((json.dumps(payload) + "\n").encode(), fresh, "revision")

    ready = parse()
    assert ready["source"]["fileOpsDigest"] == payload["fileOpsDigest"]
    assert ready["source"]["footerDigest"] == payload["footerDigest"]
    assert ready["fileOpsBytes"] == file_ops
    assert ready["footerBytes"] == footer

    # The full Python FD/sidecar path carries both validated byte streams,
    # using a fake subprocess only; it never imports a different copied Pi.
    from agent_comms import selected_source_snapshot as module

    package = tmp_path / "fixture-package"
    compaction = package / "dist/core/compaction/compaction.js"
    compaction.parent.mkdir(parents=True, exist_ok=True)
    compaction.write_bytes(b"fixture only; never executed")
    monkeypatch.setattr(module, "_trusted_package", lambda _: None)
    monkeypatch.setattr(
        module, "_COMPACTION_SHA256", hashlib.sha256(compaction.read_bytes()).hexdigest()
    )
    monkeypatch.setattr(module.shutil, "which", lambda _: "/fake/node")
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 0, (json.dumps(payload) + "\n").encode(), b""
        ),
    )
    snapshot = capture_selected_source_snapshot(package, fresh, keep_recent_tokens=20)
    assert snapshot is not None
    assert snapshot.file_ops_bytes == file_ops
    assert snapshot.footer_bytes == footer
    assert snapshot.source["fileOpsDigest"] == payload["fileOpsDigest"]
    assert snapshot.source["footerDigest"] == payload["footerDigest"]

    # A well-formed false footer digest must not attest a different final summary.
    payload["footerDigest"] = "a" * 64
    with pytest.raises(SelectedSourceSnapshotError, match="file operations or footer differs"):
        parse()
    payload["footerDigest"] = hashlib.sha256(footer).hexdigest()
    payload["footerBase64"] = base64.b64encode(
        b"\n\n<modified-files>\nprior.txt\n</modified-files>"
    ).decode()
    payload["footerByteLength"] = len(base64.b64decode(payload["footerBase64"]))
    payload["footerDigest"] = hashlib.sha256(base64.b64decode(payload["footerBase64"])).hexdigest()
    with pytest.raises(SelectedSourceSnapshotError, match="file operations or footer differs"):
        parse()
    payload["footerBase64"] = base64.b64encode(footer).decode()
    payload["footerByteLength"] = len(footer)
    payload["footerDigest"] = hashlib.sha256(footer).hexdigest()
    payload["fileOpsBase64"] = base64.b64encode(b'{"readFiles":[],"modifiedFiles":[]}').decode()
    payload["fileOpsByteLength"] = len(base64.b64decode(payload["fileOpsBase64"]))
    payload["fileOpsDigest"] = hashlib.sha256(
        base64.b64decode(payload["fileOpsBase64"])
    ).hexdigest()
    with pytest.raises(SelectedSourceSnapshotError, match="file operations or footer differs"):
        parse()

    def set_file_ops(raw: bytes, formatted: bytes) -> None:
        payload["fileOpsBase64"] = base64.b64encode(raw).decode()
        payload["fileOpsByteLength"] = len(raw)
        payload["fileOpsDigest"] = hashlib.sha256(raw).hexdigest()
        payload["footerBase64"] = base64.b64encode(formatted).decode()
        payload["footerByteLength"] = len(formatted)
        payload["footerDigest"] = hashlib.sha256(formatted).hexdigest()

    # JS sorts UTF-16 code units: supplementary U+10000 precedes BMP U+E000.
    # Python scalar-order sort would falsely decline this exact valid Pi order.
    paths = ["\U00010000", "\ue000"]
    sorted_ops = json.dumps(
        {"readFiles": paths, "modifiedFiles": []}, ensure_ascii=False, separators=(",", ":")
    ).encode()
    sorted_footer = ("\n\n<read-files>\n" + "\n".join(paths) + "\n</read-files>").encode()
    set_file_ops(sorted_ops, sorted_footer)
    assert parse()["fileOpsBytes"] == sorted_ops
    reversed_ops = json.dumps(
        {"readFiles": paths[::-1], "modifiedFiles": []},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    set_file_ops(reversed_ops, sorted_footer)
    with pytest.raises(SelectedSourceSnapshotError, match="file operations or footer differs"):
        parse()

    # JS can stringify a lone surrogate as an escape; Python must return the
    # nominal typed decline, not leak UnicodeEncodeError from canonical JSON.
    surrogate_ops = b'{"readFiles":["\\ud800"],"modifiedFiles":[]}'
    set_file_ops(surrogate_ops, b"\n\n<read-files>\n\\ud800\n</read-files>")
    with pytest.raises(SelectedSourceSnapshotError, match="file operations or footer differs"):
        parse()


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
