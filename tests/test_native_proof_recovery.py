"""Saved, actual pinned native sessions with indexed proof; loopback provider only."""

import asyncio
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from agent_comms.native_pi import NativeContextJournal, NativeContextProof
from test_backend_native_lifecycle import native_backend as native_backend


def grow_proof(session, original, target_bytes):
    """Repeated context records backed by one real saved input; synthetic growth only.

    The same source/digest is retained. The actual CLI must recover and commit its
    next request beyond these generations. Never claim these are provider calls.
    """
    path = Path(str(session) + ".input-proof")
    contract = NativeContextJournal.native_contract()
    columns = NativeContextJournal.columns()
    row = NativeContextJournal(
        **{
            **{name: getattr(original, name) for name in columns if name not in {"schema", "type"}},
            "schema": 1,
            "type": "context_committed",
        }
    )
    query, values = row._insertion()
    position = columns.index("request_generation")
    with closing(sqlite3.connect(path)) as db:
        generation = db.execute(contract["head"]).fetchone()[0]
        while path.stat().st_size < target_bytes:
            end = generation + 20000
            with db:
                db.executemany(
                    query,
                    (
                        (*values[:position], number, *values[position + 1 :])
                        for number in range(generation + 1, end + 1)
                    ),
                )
            generation = end
    return generation, path.stat().st_size


async def test_actual_saved_native_reopens_after_increasing_proof_history(native_backend):
    owner = native_backend
    first = await owner.run("Seed a disposable proof-growth session")
    assert first[-1].ok, first[-1]
    input_id = owner.starts[0][1]
    original = NativeContextProof.read_evidence(owner.session, input_id, request_generation=1)
    history = owner.session.read_bytes()
    receipts = []
    for target in (1024**2, 32 * 1024**2, 129 * 1024**2):
        await owner.persistent.discard_for_external_write(str(owner.session))
        generation, size = await asyncio.to_thread(grow_proof, owner.session, original, target)
        assert (
            NativeContextProof.read_evidence(owner.session, input_id, request_generation=1)
            == original
        )
        before = time.monotonic()
        result = await owner.run(f"New diagnostic input after proof history {target}")
        elapsed = time.monotonic() - before
        assert result[-1].ok, result[-1]
        current_id = owner.starts[-1][1]
        proof = NativeContextProof.read_evidence(owner.session, current_id)
        assert proof.request_generation == generation + 1
        assert owner.session.read_bytes().startswith(history)
        child = owner.persistent.custody.child.proc
        status = Path(f"/proc/{child.pid}/status").read_text()
        high_water = (
            int(next(line for line in status.splitlines() if line.startswith("VmHWM:")).split()[1])
            * 1024
        )
        receipts.append(
            {"proof_bytes": size, "cold_turn_seconds": elapsed, "native_peak_rss": high_water}
        )
    assert len(owner.saved_inputs()) == len(owner.starts) == owner.provider.posts == 4
    assert len({row[1] for row in owner.starts}) == 4
    print("native_proof_growth=" + json.dumps(receipts))
