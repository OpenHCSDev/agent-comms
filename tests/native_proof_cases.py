"""Current proof fixtures through the production row declaration, never a legacy reader."""

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from agent_comms.field_codec import FieldCodec
from agent_comms.native_pi import NativeContextJournal, NativeContextProof, NativeContextRecord
from agent_comms.pi_events import ContextCommitted


def _context_fields(row: dict) -> dict:
    return {key: row[key] for _, key in FieldCodec.record_fields(NativeContextRecord)}


def emitted_context(row: dict) -> ContextCommitted:
    """The fake emits the same complete native fields it wrote to the journal."""
    return ContextCommitted.from_wire({
        ContextCommitted.wire_tag: ContextCommitted.wire_kind(), **_context_fields(row),
    })


def append_input_entry(session: Path, entry: dict) -> int:
    """Append the saved input once and return its original byte position."""
    with session.open("ab") as output:
        start = output.tell()
        output.write((json.dumps(entry) + "\n").encode())
    session.chmod(0o600)
    return start


def recorded_context(session: Path, row: dict, *, session_entry_start: int) -> NativeContextProof:
    """Locate the fake's recorded context through the production declaration."""
    record = FieldCodec.decode(NativeContextRecord, _context_fields(row))
    return record.at(session, session_entry_start=session_entry_start)


def write_proof_rows(session: Path, rows) -> None:
    path = Path(str(session) + ".input-proof")
    path.unlink(missing_ok=True)
    os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    with closing(sqlite3.connect(path)) as db:
        NativeContextJournal.create(db)
        with db:
            for row in rows:
                FieldCodec.decode(
                    NativeContextJournal, {NativeContextJournal.family_discriminator: NativeContextJournal.declared_name, **row}
                ).insert(db)


def read_proof_rows(session: Path) -> list[dict]:
    with NativeContextJournal.open_evidence(session) as db:
        return [
            FieldCodec.encode(row)
            for row in NativeContextJournal.select(db, order_by=("request_generation", "input_id"))
        ]


