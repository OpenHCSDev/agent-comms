"""A reserved input takes the digest stored for it in the retired binding file."""
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from cutover.carry_reserved_prompt_digest import ReservedPromptDigest

from agent_comms.coordination.database import CoordinationStore
from agent_comms.execution.native_prompt_binding import native_request_digest
from agent_comms.execution.native_runtime_input import NativeRuntimeInput
from agent_comms.nominal.field_codec import InputId, ThreadLookup
from agent_comms.storage.locked_store import LiveFilesRequest

FIRST, SECOND = InputId("a" * 32), InputId("b" * 32)
OWNER = ThreadLookup("c" * 32)
DIGESTS = {FIRST: "1" * 64, SECOND: "2" * 64}


def old_root(root: Path, bound: dict[str, str]) -> Path:
    """A coordinator whose input rows predate the digest, beside the file that held it."""
    root.mkdir(mode=0o700, parents=True)
    path = CoordinationStore.path_in(root)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(NativeRuntimeInput.schema_objects()[NativeRuntimeInput.declared_name])
        db.execute(f'ALTER TABLE "{NativeRuntimeInput.declared_name}"'
                   ' DROP COLUMN "expected_prompt_digest"')
        db.executemany(
            "INSERT INTO native_runtime_input (input_id, stage, owner_lookup, owner_generation,"
            " owner_token_digest) VALUES (?, 'triage', ?, 1, ?)",
            [(FIRST, OWNER, "0" * 64), (SECOND, OWNER, "0" * 64)])
    retired = path.with_name(ReservedPromptDigest.previous)
    with closing(sqlite3.connect(retired)) as db, db:
        db.execute('CREATE TABLE "prompt_binding" ("input_id" TEXT PRIMARY KEY,'
                   ' "expected_prompt_digest" TEXT NOT NULL, "wire_root_id" TEXT NOT NULL)')
        db.executemany('INSERT INTO "prompt_binding" VALUES (?, ?, ?)',
                       [(input_id, digest, "f" * 32) for input_id, digest in bound.items()])
    retired.with_name(f".{retired.name}.snapshot-lock").touch()
    return path


def test_each_input_takes_its_stored_digest_and_the_file_goes_after_acceptance(tmp_path):
    root = tmp_path / "root"
    path = old_root(root, DIGESTS)
    retired = path.with_name(ReservedPromptDigest.previous)
    step = ReservedPromptDigest(LiveFilesRequest(root), tmp_path / "spill")

    assert retired in step.input_files()
    assert step.carry(apply=False).detail == {"stores": 1, "inputs": 2, "unbound": 0}
    assert step.carry(apply=True).rewritten == (path.relative_to(root),)
    with closing(sqlite3.connect(path)) as db:
        assert {row.input_id: row.expected_prompt_digest
                for row in NativeRuntimeInput.select(db)} == DIGESTS
        # The rebuilt table has its guards back: the digest is written once.
        with pytest.raises(sqlite3.IntegrityError):
            db.execute('UPDATE native_runtime_input SET expected_prompt_digest=?', ("3" * 64,))
    assert step.carry(apply=True).changed == 0
    assert retired.is_file()  # A restore still needs it.
    assert step.after_acceptance().changed == 1
    assert not retired.exists()
    assert not retired.with_name(f".{retired.name}.snapshot-lock").exists()
    assert step.after_acceptance().changed == 0


def test_an_input_reserved_without_a_digest_stays_unsendable(tmp_path):
    root = tmp_path / "root"
    path = old_root(root, {FIRST: DIGESTS[FIRST]})
    step = ReservedPromptDigest(LiveFilesRequest(root), tmp_path / "spill")

    assert step.carry(apply=True).detail == {"stores": 1, "inputs": 2, "unbound": 1}
    with closing(sqlite3.connect(path)) as db:
        stored = {row.input_id: row.expected_prompt_digest
                  for row in NativeRuntimeInput.select(db)}
    # The digest of the empty request, which no reservation may send.
    assert stored == {FIRST: DIGESTS[FIRST], SECOND: native_request_digest("")}
    assert stored[SECOND] != native_request_digest("any prompt")
