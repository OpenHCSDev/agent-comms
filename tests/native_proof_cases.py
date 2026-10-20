"""Current proof fixtures through the production row declaration, never a legacy reader."""

import os
import sqlite3
from contextlib import closing
from pathlib import Path

from agent_comms.field_codec import FieldCodec
from agent_comms.native_pi import NativeContextJournal, NativeContextRecord
from agent_comms.pi_events import ContextCommitted


def emitted_context(row: dict) -> ContextCommitted:
    """The fake emits the same complete native fields it wrote to the journal."""
    return ContextCommitted.from_wire({
        ContextCommitted.wire_tag: ContextCommitted.wire_kind(),
        **{key: row[key] for _, key in FieldCodec.record_fields(NativeContextRecord)},
    })


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


def child_writer_imports() -> str:
    """Recorded-RPC children use the same current declaration as their parent."""
    import agent_comms.native_pi as native_pi

    return (
        "import sys\n"
        f"sys.path.insert(0, {str(Path(native_pi.__file__).parents[1])!r})\n"
        f"sys.path.insert(0, {str(Path(__file__).parent)!r})\n"
        "from native_proof_cases import write_proof_rows\n"
    )
