"""One-shot quiet-runtime conversion of durable proof; delete after reviewed cutover.

Stop native owners first. Native history is untouched, including inputs with no
context receipt. Conversion verifies every old proof against that history, then
publishes one indexed authority atomically. The prior bytes remain in an explicit
operator backup for rollback. Production has no reader for this old format.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from agent_comms.field_codec import FieldCodec
from agent_comms.native_entries import NativeEntry
from agent_comms.native_pi import NativeContextJournal, _fsync_directory, _read_private_file


def convert(session: Path, backup: Path) -> dict[str, int]:
    session = session.absolute()
    proof = Path(str(session) + ".input-proof")
    temporary = proof.with_name(f".{proof.name}.{uuid4().hex}.pending")
    lock = Path(str(session) + ".pr48-writer.lock")
    # This is SessionManager's existing exclusive writer fence. Never steal one.
    descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w") as held:
            held.write(f"{os.getpid()}\n")
            held.flush()
            os.fsync(held.fileno())
            source = session.stat()
            header, entries = NativeEntry.read_evidence(session)
            tracked = NativeEntry.tracked_users(entries)
            before = proof.stat()
            # Independent owner-only backup, never a hardlink into either format.
            backup_fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(backup_fd, "wb") as output, proof.open("rb") as original:
                shutil.copyfileobj(original, output, length=1024**2)
                output.flush()
                os.fsync(output.fileno())
            _fsync_directory(backup.parent)
            os.close(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
            count, accepted = 0, set()
            with closing(sqlite3.connect(temporary)) as db:
                db.execute("PRAGMA journal_mode=DELETE")
                db.execute("PRAGMA synchronous=EXTRA")
                with db:
                    NativeContextJournal.create(db)
                    for raw in _read_private_file(proof):
                        row = FieldCodec.decode(NativeContextJournal, raw)
                        row.corroborate(session, header, tracked)
                        row.insert(db)  # Same append/generation/digest constraints as native.
                        count += 1
                        accepted.add(row.input_id)
                if db.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                    raise ValueError("Converted proof failed SQLite integrity verification")
            # Require the unchanged native input authority and exact old proof
            # snapshot. A successful conversion grants no live acceptance.
            for path, snapshot in ((session, source), (proof, before)):
                after = path.stat()
                if (
                    not os.path.samestat(snapshot, after)
                    or snapshot.st_mtime_ns != after.st_mtime_ns
                ):
                    raise ValueError("Native history/proof changed during quiet conversion")
            with temporary.open("rb") as complete:
                os.fsync(complete.fileno())
            os.replace(temporary, proof)
            _fsync_directory(proof.parent)
            return {
                "proof_rows": count,
                "tracked_inputs": len(tracked),
                "inputs_without_context_proof": len(tracked.keys() - accepted),
            }
    finally:
        temporary.unlink(missing_ok=True)
        lock.unlink()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", type=Path)
    parser.add_argument("backup", type=Path)
    args = parser.parse_args()
    print(convert(args.session, args.backup))


if __name__ == "__main__":
    main()
