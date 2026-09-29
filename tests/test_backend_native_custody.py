"""Saved-session launch fences exercised with the actual pinned Pi child."""

import json
import os
from uuid import uuid4

import pytest

pytest_plugins = ("test_backend_native_lifecycle",)


async def test_actual_native_attestation_refuses_foreign_expected_identity(native_backend):
    owner = native_backend
    first = await owner.run("Diagnostic input before attestation mismatch")
    assert first[-1].ok, first[-1]
    previous = owner.persistent.proc
    before = owner.session.read_bytes()
    # Corrupt only the expected witness in this disposable fixture. Pi still
    # reports its actual saved identity through the genuine get_state response.
    owner.persistent.session_id = str(uuid4())
    refused = await owner.run("Must remain unsent after witness mismatch")
    assert not refused[-1].ok and refused[-1].reason_code == "session_identity_uncertain"
    assert not previous.alive() and owner.persistent.proc is None
    assert len(owner.starts) == owner.provider.posts == 1
    assert owner.session.read_bytes() == before
    print("native_attestation_refusal", refused[-1], flush=True)


@pytest.mark.parametrize("changed", ["session", "credentials"])
async def test_actual_native_revision_change_retires_child_without_replay(native_backend, changed):
    owner = native_backend
    first = await owner.run("Diagnostic input before revision change")
    assert first[-1].ok, first[-1]
    previous = owner.persistent.proc
    assert previous is not None and previous.alive()
    path = owner.session if changed == "session" else owner.config / "auth.json"
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1000000))
    second = await owner.run("New diagnostic input after revision change")
    assert second[-1].ok, second[-1]
    assert len(owner.children) == 2 and owner.persistent.proc is not previous
    assert not previous.alive()
    assert len(owner.starts) == len(owner.saved_inputs()) == owner.provider.posts == 2
    print("native_revision_retirement", changed, second[-1], flush=True)


async def test_actual_native_strict_reopen_refuses_changed_identity_without_input(native_backend):
    owner = native_backend
    first = await owner.run("Diagnostic input before strict reopen")
    assert first[-1].ok, first[-1]
    original = owner.session.read_bytes()
    previous = owner.persistent.proc
    await owner.persistent.discard_for_external_write(str(owner.session))
    assert not previous.alive()
    rows = original.decode().splitlines()
    header = json.loads(rows[0])
    header["id"] = str(uuid4())
    altered = ("\n".join([json.dumps(header), *rows[1:]]) + "\n").encode()
    owner.session.write_bytes(altered)
    refused = await owner.run("Must remain unsent")
    assert not refused[-1].ok and refused[-1].reason_code == "compaction_reopen_invalid"
    assert owner.session.read_bytes() == altered
    assert owner.persistent.proc is None
    assert owner.persistent.reopen_required == str(owner.session)
    assert len(owner.starts) == owner.provider.posts == 1
    # Repair only this disposable fixture. Send a NEW explicit input, never the refused one.
    owner.session.write_bytes(original)
    restored = await owner.run("New diagnostic input after restoring fixture")
    assert restored[-1].ok, restored[-1]
    assert owner.persistent.reopen_required is None
    assert len(owner.starts) == len(owner.saved_inputs()) == owner.provider.posts == 2
    assert [row[2] for row in owner.starts] == [
        "Diagnostic input before strict reopen",
        "New diagnostic input after restoring fixture",
    ]
    print("native_strict_reopen", refused[-1], restored[-1], flush=True)
