"""Original-byte custody survives appends and refuses altered proof/source."""

import json
import os
from pathlib import Path

import pytest

from agent_comms.native_entries import NativeEntry, NativeEvidenceScope
from agent_comms.native_pi import NativeContextProof, NativePiUnavailable, PrivateEvidenceRead
from native_proof_cases import write_proof_rows
from test_native_pi import _evidence, INPUT_ID


def test_evidence_scope_switches_original_source_and_closes_previous_reader(tmp_path):
    first_dir, second_dir = tmp_path / "first", tmp_path / "second"
    first_dir.mkdir(mode=0o700)
    second_dir.mkdir(mode=0o700)
    first, second = _evidence(first_dir), _evidence(second_dir)
    with NativeEvidenceScope() as scope:
        old = scope.for_source(first)
        NativeContextProof.read_evidence(first, INPUT_ID, evidence=old)
        assert scope.for_source(first) is old
        current = scope.for_source(second)
        assert old.source.stream.closed and not old.entries
        assert current is not old
        NativeContextProof.read_evidence(second, INPUT_ID, evidence=current)
        assert len(scope.readers) == 1
    assert current.source.stream.closed and not current.entries and not scope.readers


def test_evidence_scope_does_not_reopen_original_after_refusal(tmp_path):
    session = _evidence(tmp_path)
    with NativeEvidenceScope() as scope:
        original = scope.for_source(session)
        NativeContextProof.read_evidence(session, INPUT_ID, evidence=original)
        session.write_bytes(session.read_bytes().replace(b"separate", b"changed!"))
        with pytest.raises(NativePiUnavailable):
            NativeContextProof.read_evidence(session, INPUT_ID, evidence=original)
        assert original.source.stream.closed and not original.entries
        with pytest.raises(NativePiUnavailable):
            NativeContextProof.read_evidence(session, INPUT_ID, evidence=scope.for_source(session))
    assert not scope.readers


def test_acquired_source_observes_new_context_generation(tmp_path):
    session = _evidence(tmp_path)
    with NativeEntry.open_evidence(session) as evidence:
        original = NativeContextProof.read_evidence(session, INPUT_ID, evidence=evidence)
        with session.open("a") as output:
            output.write(json.dumps({"type": "compaction", "id": "new", "summary": "observed append"}) + "\n")
        write_proof_rows(session, [{
            "schema": 1, "type": "context_committed", "sessionId": "sid",
            "inputId": INPUT_ID, "sessionEntryId": "entry",
            "requestGeneration": 2, "llmContextDigest": "d" * 64,
        }])
        current = NativeContextProof.read_evidence(session, INPUT_ID, evidence=evidence)
        assert current.request_generation == 2
        assert current.llm_context_digest != original.llm_context_digest
        assert evidence.entries[-1].id == "new"
    with pytest.raises(NativePiUnavailable):
        NativeContextProof.read_evidence(session, INPUT_ID, evidence=evidence)


@pytest.mark.parametrize("damage", [
    "prefix", "truncate", "replace", "symlink", "permissions", "ancestor_permissions",
    "duplicate_input", "non_user_input", "malformed_append", "proof",
])
def test_acquired_source_never_reuses_evidence_after_damage(tmp_path, damage):
    session = _evidence(tmp_path)
    with NativeEntry.open_evidence(session) as evidence:
        NativeContextProof.read_evidence(session, INPUT_ID, evidence=evidence)
        original = session.read_bytes()
        if damage == "prefix":
            observed = session.stat()
            session.write_bytes(original.replace(b"separate", b"changed!"))
            os.utime(session, ns=(observed.st_atime_ns, observed.st_mtime_ns))
        elif damage == "truncate":
            session.write_bytes(original[:original.index(b"\n") + 1])
        elif damage == "replace":
            replacement = session.with_suffix(".new")
            replacement.write_bytes(original)
            replacement.chmod(0o600)
            replacement.replace(session)
        elif damage == "symlink":
            original_path = session.with_suffix(".original")
            session.rename(original_path)
            session.symlink_to(original_path)
        elif damage == "permissions":
            session.chmod(0o644)
        elif damage == "ancestor_permissions":
            session.parent.chmod(0o777)
        elif damage in ("duplicate_input", "non_user_input"):
            entry = json.loads(original.splitlines()[1])
            entry["id"] = "duplicate"
            if damage == "non_user_input":
                entry["message"]["role"] = "assistant"
            with session.open("a") as output:
                output.write(json.dumps(entry) + "\n")
        elif damage == "malformed_append":
            with session.open("ab") as output:
                output.write(b'{"type":"message"')
        elif damage == "proof":
            Path(str(session) + ".input-proof").write_bytes(b"corrupt original proof")
        with pytest.raises(NativePiUnavailable):
            NativeContextProof.read_evidence(session, INPUT_ID, evidence=evidence)
        assert evidence.source.stream.closed
        assert not evidence.entries


def test_acquired_source_cannot_corroborate_another_file(tmp_path):
    session = _evidence(tmp_path)
    with NativeEntry.open_evidence(session) as evidence:
        with pytest.raises(NativePiUnavailable, match="another source"):
            NativeContextProof.read_evidence(session.with_name("foreign.jsonl"), INPUT_ID, evidence=evidence)
        assert evidence.source.stream.closed


@pytest.mark.parametrize("operation", ["append", "alter_prefix"])
def test_byte_snapshot_rechecks_original_prefix_after_decoding(tmp_path, operation):
    session = _evidence(tmp_path)
    with PrivateEvidenceRead.open(session) as source:
        rows = source.rows()
        assert next(rows)["type"] == "session"
        if operation == "append":
            with session.open("a") as output:
                output.write(json.dumps({"type": "compaction", "id": "new", "summary": "next snapshot"}) + "\n")
            assert len(tuple(rows)) == 1
            assert tuple(source.rows())[0]["id"] == "new"
        else:
            session.write_bytes(session.read_bytes().replace(b"separate", b"changed!"))
            with pytest.raises(NativePiUnavailable, match="prefix changed"):
                tuple(rows)
            assert source.stream.closed
