"""Saved, actual pinned native sessions with indexed proof; loopback provider only."""

import asyncio
import json
import os
import sqlite3
import subprocess
import time
from contextlib import closing
from pathlib import Path

import pytest

from agent_comms.native_pi import NativeContextJournal, NativeContextProof, NativePiUnavailable
from agent_comms.native_session_prepare import NativeSessionPreparation
from agent_comms.pi_commands import GetState, Prompt
from agent_comms.pi_events import ContextCommitted, InputCommitted, Response
from native_proof_cases import read_proof_rows, write_proof_rows
from test_backend_native_lifecycle import native_backend as native_backend
from tools.cutover.native_proof_journal import convert


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
        await owner.persistent.discard_for_external_write(str(owner.session))
    receipt = owner.session.parent / "crash-boundary.txt"
    with monkeypatch.context() as crash:
        crash.setenv("LD_PRELOAD", str(crash_preload))
        crash.setenv("PROOF_CRASH_POINT", boundary)
        crash.setenv("PROOF_CRASH_FILE", str(owner.session) + ".input-proof")
        crash.setenv("PROOF_CRASH_RECEIPT", str(receipt))
        interrupted = await owner.run("This diagnostic input is interrupted, never replay it")
    assert receipt.read_text() == boundary
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
    # Actual new native process performs hot-journal recovery before admission.
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


async def test_actual_native_conversion_preserves_accepted_and_unknown(
    native_backend,
    crash_preload,
    monkeypatch,
):
    owner = native_backend
    assert (await owner.run("Real seed for durable conversion"))[-1].ok
    accepted_id = owner.starts[-1][1]
    accepted = NativeContextProof.read_evidence(owner.session, accepted_id)
    await owner.persistent.discard_for_external_write(str(owner.session))
    receipt = owner.session.parent / "conversion-crash.txt"
    with monkeypatch.context() as crash:
        crash.setenv("LD_PRELOAD", str(crash_preload))
        crash.setenv("PROOF_CRASH_POINT", "before-journal-sync")
        crash.setenv("PROOF_CRASH_FILE", str(owner.session) + ".input-proof")
        crash.setenv("PROOF_CRASH_RECEIPT", str(receipt))
        failed = await owner.run("Uncertain native input retained across proof conversion")
    assert not failed[-1].ok and receipt.read_text() == "before-journal-sync"
    unknown_id = owner.starts[-1][1]
    # Let the ACTUAL native owner recover the hot database, with get_state only.
    await NativeSessionPreparation.open(
        owner.persistent,
        "pi",
        [
            "--provider",
            "response-local",
            "--model",
            "fixture",
            "--thinking",
            "off",
            "--offline",
            "--no-extensions",
            "--no-skills",
            "--no-context-files",
            "--no-prompt-templates",
            "--no-tools",
        ],
        worktree=str(owner.project),
        environment=dict(os.environ),
        session_file=str(owner.session),
    )
    await owner.persistent.discard_for_external_write(str(owner.session))
    assert len(owner.saved_inputs()) == 2 and owner.provider.posts == 1
    before = owner.session.read_bytes()
    # Reconstruct ONLY the retired proof representation from actual native
    # receipts; native saved history and its uncertain input are never fabricated.
    rows = read_proof_rows(owner.session)
    old = b"".join(
        (json.dumps({key: value for key, value in row.items() if key != "kind"}) + "\n").encode()
        for row in rows
    )
    proof = Path(str(owner.session) + ".input-proof")
    proof.write_bytes(old)
    backup = owner.session.parent / "original-proof.jsonl.backup"
    converted = await asyncio.to_thread(convert, owner.session, backup)
    assert converted == {"proof_rows": 1, "tracked_inputs": 2, "inputs_without_context_proof": 1}
    assert backup.read_bytes() == old
    assert owner.session.read_bytes() == before
    assert NativeContextProof.read_evidence(owner.session, accepted_id) == accepted
    with NativeContextJournal.open_evidence(owner.session) as db:
        assert NativeContextJournal.for_input(db, unknown_id) is None
    await NativeSessionPreparation.open(
        owner.persistent,
        "pi",
        [
            "--provider",
            "response-local",
            "--model",
            "fixture",
            "--thinking",
            "off",
            "--offline",
            "--no-extensions",
            "--no-skills",
            "--no-context-files",
            "--no-prompt-templates",
            "--no-tools",
        ],
        worktree=str(owner.project),
        environment=dict(os.environ),
        session_file=str(owner.session),
    )
    child = owner.persistent.custody.child

    async def probe(command):
        future = child.reader.track(command)
        await child.proc.write(child.reader.command_bytes(command))
        async with asyncio.timeout(5):
            while not future.done():
                event = await child.reader.receive(strict=True)
                assert event is not None
                assert not isinstance(event, (ContextCommitted, InputCommitted)), event
                if isinstance(event, Response):
                    child.reader.correlate(event)
        return future.result()

    # Deliberate duplicates of this disposable test's IDs prove native dedup;
    # they must not append, produce receipts or invoke the local provider.
    for identifier, text in (
        (accepted_id, "Real seed for durable conversion"),
        (unknown_id, "Uncertain native input retained across proof conversion"),
    ):
        assert (
            await probe(Prompt(id="duplicate-" + identifier, input_id=identifier, message=text))
        ).success
        refused = await probe(
            Prompt(id="conflict-" + identifier, input_id=identifier, message="different")
        )
        assert not refused.success and "Conflicting replay" in refused.error
    idle = await probe(GetState(id="after-dedup"))
    assert idle.success and idle.data.is_streaming is False and idle.data.pending_message_count == 0
    assert owner.session.read_bytes() == before and owner.provider.posts == 1
    with NativeContextJournal.open_evidence(owner.session) as db:
        assert NativeContextJournal.for_input(db, unknown_id) is None
    result = await owner.run("One genuinely new diagnostic input after offline conversion")
    assert result[-1].ok, result[-1]
    assert len(owner.saved_inputs()) == len(owner.starts) == 3
    assert len({row[1] for row in owner.starts}) == 3
    assert owner.provider.posts == 2
    assert owner.session.read_bytes().startswith(before)
    assert (
        NativeContextProof.read_evidence(owner.session, accepted_id, request_generation=1)
        == accepted
    )
    print("native_proof_conversion=" + json.dumps(converted))


@pytest.mark.parametrize("damage", ["malformed", "torn", "foreign_source"])
async def test_actual_native_bad_proof_refuses_before_any_input(native_backend, damage):
    owner = native_backend
    assert (await owner.run("Real seed before proof damage"))[-1].ok
    await owner.persistent.discard_for_external_write(str(owner.session))
    proof = Path(str(owner.session) + ".input-proof")
    complete, history = proof.read_bytes(), owner.session.read_bytes()
    if damage == "malformed":
        proof.write_bytes(b"incomplete SQLite header")
    elif damage == "torn":
        proof.write_bytes(complete[: len(complete) // 2])
    else:
        rows = read_proof_rows(owner.session)
        rows[0]["sessionEntryId"] = "foreign-native-entry"
        write_proof_rows(owner.session, rows)
    damaged = proof.read_bytes()
    try:
        with pytest.raises(NativePiUnavailable):
            await NativeSessionPreparation.open(
                owner.persistent,
                "pi",
                [
                    "--provider",
                    "response-local",
                    "--model",
                    "fixture",
                    "--thinking",
                    "off",
                    "--offline",
                    "--no-extensions",
                    "--no-skills",
                    "--no-context-files",
                    "--no-prompt-templates",
                    "--no-tools",
                ],
                worktree=str(owner.project),
                environment=dict(os.environ),
                session_file=str(owner.session),
            )
        assert owner.session.read_bytes() == history
        assert proof.read_bytes() == damaged
        assert len(owner.starts) == len(owner.saved_inputs()) == owner.provider.posts == 1
    finally:
        await owner.persistent.close()
        proof.write_bytes(complete)  # Restore only this disposable fixture's exact proof.
    recovered = await owner.run("New diagnostic input after explicit fixture restoration")
    assert recovered[-1].ok, recovered[-1]
    assert len(owner.starts) == len(owner.saved_inputs()) == owner.provider.posts == 2
