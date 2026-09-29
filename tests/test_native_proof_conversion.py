"""One-shot old-format validation and interrupted publication; no runtime fallback."""

import json
import os
import signal
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from agent_comms.native_pi import NativeContextJournal, NativeContextProof, NativePiUnavailable
from native_proof_cases import read_proof_rows
from test_native_pi import INPUT_ID, _evidence
from tools.cutover.native_proof_journal import convert


def legacy_fixture(tmp_path):
    session = _evidence(tmp_path)
    records = read_proof_rows(session)
    prior = b"".join(
        (json.dumps({key: value for key, value in row.items() if key != "kind"}) + "\n").encode()
        for row in records
    )
    proof = Path(str(session) + ".input-proof")
    proof.write_bytes(prior)
    return session, proof, prior


@pytest.mark.parametrize("boundary", ["before", "after"])
def test_converter_killed_around_atomic_replace_preserves_both_authorities(tmp_path, boundary):
    session, proof, prior = legacy_fixture(tmp_path)
    history = session.read_bytes()
    backup = tmp_path / "prior-proof.backup"
    env = {**os.environ, "PYTHONPATH": os.pathsep.join((str(Path.cwd()), os.environ["PYTHONPATH"]))}
    child = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("native_proof_convert_child.py")),
            boundary,
            str(session),
            str(backup),
        ],
        env=env,
        timeout=10,
    )
    assert child.returncode == -signal.SIGKILL
    assert backup.read_bytes() == prior and session.read_bytes() == history
    # Converter death retains the existing writer fence. This test explicitly
    # disposes only its own killed child's fence before a controlled retry.
    lock = Path(str(session) + ".pr48-writer.lock")
    assert lock.is_file()
    if boundary == "before":
        assert proof.read_bytes() == prior
        with pytest.raises(NativePiUnavailable):
            NativeContextProof.read_evidence(session, INPUT_ID)
        lock.unlink()
        convert(session, tmp_path / "second-backup")
    else:
        assert NativeContextProof.read_evidence(session, INPUT_ID).request_generation == 1
        lock.unlink()
    assert NativeContextProof.read_evidence(session, INPUT_ID).request_generation == 1
    assert session.read_bytes() == history and backup.read_bytes() == prior


@pytest.mark.parametrize(
    "damage", ["truncated", "duplicate", "foreign", "digest_conflict", "bad_schema"]
)
def test_conversion_refuses_invalid_prior_proof_without_publishing(tmp_path, damage):
    session, proof, prior = legacy_fixture(tmp_path)
    row = json.loads(prior)
    if damage == "truncated":
        prior = prior.rstrip(b"\n")
    elif damage == "duplicate":
        prior += prior
    elif damage == "foreign":
        prior = (json.dumps({**row, "sessionEntryId": "foreign"}) + "\n").encode()
    elif damage == "digest_conflict":
        prior += (json.dumps({**row, "llmContextDigest": "f" * 64}) + "\n").encode()
    else:
        prior = (json.dumps({**row, "schema": 2}) + "\n").encode()
    proof.write_bytes(prior)
    history = session.read_bytes()
    with pytest.raises((NativePiUnavailable, ValueError, sqlite3.IntegrityError)):
        convert(session, tmp_path / "backup")
    assert proof.read_bytes() == prior and session.read_bytes() == history
    assert not Path(str(session) + ".pr48-writer.lock").exists()
    assert not list(proof.parent.glob("*.pending"))


def test_generated_native_schema_is_exactly_the_existing_declaration():
    generated = (
        Path("stack/native-proof-schema.mjs").read_text().split(" = ", 1)[1].removesuffix(";\n")
    )
    assert json.loads(generated) == NativeContextJournal.native_contract()
