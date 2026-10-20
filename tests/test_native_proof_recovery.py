"""Saved, actual pinned native sessions with indexed proof; loopback provider only."""

import asyncio
import json
import os
import sqlite3
import subprocess
import time
from contextlib import closing
from pathlib import Path
from typing import get_args, get_type_hints

import pytest

from agent_comms.native_pi import NativeContextJournal, NativeContextProof, NativePiUnavailable
from agent_comms.native_session_prepare import NativeSessionPreparation
from agent_comms.pi_commands import GetState, Prompt
from agent_comms.native_tool_call import OwnerToolConnection
from agent_comms.pi_events import ContextCommitted, InputCommitted, Response
from native_proof_cases import read_proof_rows, write_proof_rows


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
            # The journal envelope comes from its declaration, not a test copy.
            **{name: get_args(get_type_hints(NativeContextJournal)[name])[0] for name in ("schema", "type")},
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


@pytest.fixture(scope="module")
def crash_preload(tmp_path_factory):
    root = tmp_path_factory.mktemp("native-proof-crash-library")
    library = root / "crash.so"
    subprocess.run(
        [
            "cc",
            "-shared",
            "-fPIC",
            "-Wall",
            "-Werror",
            "-o",
            str(library),
            str(Path(__file__).with_name("native_proof_crash.c")),
            "-ldl",
        ],
        check=True,
    )
    return library


@pytest.mark.parametrize(
    "boundary,seeded",
    [
        ("before-journal-sync", True),
        ("before-database-sync", True),
        ("after-database-sync", True),
        ("after-journal-remove", True),
        ("before-directory-sync", True),
        ("before-schema-sync", False),
        ("after-schema-publish", False),
        ("after-schema-unlink", False),
    ],
)
async def test_actual_native_killed_context_commit_keeps_unknown(
    native_backend,
    crash_preload,
    monkeypatch,
    boundary,
    seeded,
):
    owner = native_backend
    if seeded:
        assert (await owner.run("Real seed before interrupted proof commit"))[-1].ok
        original_id = owner.starts[0][1]
        original = NativeContextProof.read_evidence(
            owner.session, original_id, request_generation=1
        )
        await owner.force_reopen()
    acknowledgement = owner.session.parent / "crash-boundary.txt"
    with monkeypatch.context() as crash:
        crash.setenv("LD_PRELOAD", str(crash_preload))
        crash.setenv("PROOF_CRASH_POINT", boundary)
        crash.setenv("PROOF_CRASH_FILE", str(owner.session) + ".input-proof")
        crash.setenv("PROOF_CRASH_RECEIPT", str(acknowledgement))
        interrupted = await owner.run("This diagnostic input is interrupted, never replay it")
    assert acknowledgement.read_text() == boundary
    assert not interrupted[-1].ok
    assert not owner.persistent.available
    assert len(owner.starts) == len(owner.saved_inputs()) == int(seeded) + 1
    assert owner.provider.posts == int(seeded)  # No request crossed the failed publication.
    unknown_id = owner.starts[-1][1]
    history = owner.session.read_bytes()
    if seeded:
        # Cold historical projection itself must recover; it cannot need a new
        # model input merely to read the previous committed generation.
        assert (
            NativeContextProof.read_evidence(owner.session, original_id, request_generation=1)
            == original
        )
        assert owner.provider.posts == 1
    # Actual new native process performs hot-journal recovery before claim_scope.
    resumed = await owner.run("A genuinely new diagnostic input after native crash recovery")
    assert resumed[-1].ok, resumed[-1]
    assert owner.session.read_bytes().startswith(history)
    assert len(owner.starts) == len(owner.saved_inputs()) == int(seeded) + 2
    assert len({row[1] for row in owner.starts}) == int(seeded) + 2
    assert owner.provider.posts == int(seeded) + 1
    if seeded:
        assert (
            NativeContextProof.read_evidence(owner.session, original_id, request_generation=1)
            == original
        )
    # Recovery did not re-emit/replay the uncertain user's start. It can appear
    # in a later explicit input's context, which does not settle the old attempt.
    assert [row[1] for row in owner.starts].count(unknown_id) == 1
    print(
        "native_crash_recovery="
        + json.dumps(
            {
                "boundary": boundary,
                "inputs": len(owner.starts),
                "provider_calls": owner.provider.posts,
                "first_generation_preserved": True if seeded else None,
                "uncertain_input_replayed": False,
            }
        )
    )


