"""Current proof fixtures through the production row declaration, never a legacy reader."""

import os
import sqlite3
from contextlib import closing
from pathlib import Path

from agent_comms.field_codec import FieldCodec
from agent_comms.native_pi import NativeContextJournal


def write_proof_rows(session: Path, rows) -> None:
    path = Path(str(session) + ".input-proof")
    path.unlink(missing_ok=True)
    os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    with closing(sqlite3.connect(path)) as db:
        NativeContextJournal.create(db)
        with db:
            for row in rows:
                FieldCodec.decode(NativeContextJournal, row).insert(db)


def read_proof_rows(session: Path) -> list[dict]:
    with NativeContextJournal.open_evidence(session) as db:
        return [
            FieldCodec.encode(row)
            for row in NativeContextJournal.select(db, order_by=("request_generation", "input_id"))
        ]
