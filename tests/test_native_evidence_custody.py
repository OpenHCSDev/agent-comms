"""Original-byte custody survives appends and refuses altered proof/source."""

import json
import os
from pathlib import Path

import pytest

from agent_comms.native_entries import NativeEntry
from agent_comms.native_pi import NativeContextProof, NativePiUnavailable
from native_proof_cases import write_proof_rows
from test_native_pi import _evidence, INPUT_ID


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
    "prefix", "truncate", "replace", "symlink", "permissions",
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


def test_acquired_source_cannot_corroborate_another_file(tmp_path):
    session = _evidence(tmp_path)
    with NativeEntry.open_evidence(session) as evidence:
        with pytest.raises(NativePiUnavailable, match="another source"):
            NativeContextProof.read_evidence(session.with_name("foreign.jsonl"), INPUT_ID, evidence=evidence)
