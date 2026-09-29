"""Bounded, provider-free current-owner cursor pagination; no native ACK inference."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from agent_comms import coordinated_runtime as runtime
from agent_comms.bus_publication import stable_thread_lookup
from agent_comms.child_process import ProcessIdentity
from agent_comms.coordination_cohort import accept_initial_cohort
from agent_comms.coordination_errors import IdentityConflict, StaleFence
from agent_comms.coordinator import Coordination
from agent_comms.native_runtime_input import CurrentNativeCursor, NativeRuntimeInput
from agent_comms.native_source_cursor import NativeSourceCursor
from agent_comms.proven_source_coverage import SourceCoverage
from agent_comms.threads import Thread
from agent_comms.tracked_turn import TrackedTurnSession
from test_native_prompt_binding import _fake_model, _root


@pytest.fixture
def tmp_path():
    if os.name != "posix" or not Path("/var/tmp").is_dir() or Path("/var").is_symlink():
        pytest.skip("private sealed runtime needs a disposable /var/tmp")
    with tempfile.TemporaryDirectory(prefix="ac-cursor-scale-", dir="/var/tmp") as name:
        yield Path(name)


@pytest.mark.parametrize("recipients", [10, 150])
async def test_101_unrelated_initials_and_frozen_n_keeps_exact_native_cursor(
    tmp_path, monkeypatch, recipients
):
    root, root_id, comms, initial, people = _root(tmp_path)
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    fake, calls = _fake_model(decision="IGNORE")
    monkeypatch.setattr(TrackedTurnSession, "execute", fake)
    first = await runtime.SelectedExecution(
        root=root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path
    ).run()
    assert first is not None and first.cursor_status == "proven"
    for number in range(recipients - 1):
        member = Thread(
            f"member{number:03}",
            frozenset({"team"}),
            str(tmp_path),
            process_identity=ProcessIdentity.capture(os.getpid()),
        )
        comms.threads.register(member)
        with Coordination(str(root / "coordination.sqlite3")) as store:
            store.participants.register(
                stable_thread_lookup(member.created_at),
                member.name,
                member.name,
                committed=True,
            )
    for number in range(101):
        comms.messaging.send_initial_cohort("sender", "member000", f"unrelated-{number}")
    selected = comms.messaging.send_initial_cohort(
        "sender", "#team", "@alpha answer this exact source"
    )
    frozen = comms.bus.log.read_initial_cohort(root_id, selected.seq)
    assert len(frozen.audience.recipients) == recipients
    with Coordination(str(root / "coordination.sqlite3")) as store:
        receipt = accept_initial_cohort(comms.bus, root_id, selected.seq, store).value
        assert len(receipt.assignments) == 1
        coverage = SourceCoverage(
            comms.bus,
            store,
            wire_root_id=root_id,
            recipient_lookup=stable_thread_lookup(people[1].created_at),
        ).read()
        assert coverage.blocked_seq == selected.seq
    second = await runtime.SelectedExecution(
        root=root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path
    ).run()
    assert second is not None and second.cursor_status == "proven"
    assert second.input_id != first.input_id and len(calls) == 2
    with Coordination(str(root / "coordination.sqlite3")) as store:
        current = NativeSourceCursor(comms.bus, store, wire_root_id=root_id).read(
            owner_name="alpha"
        )
        assert current is not None
        assert current.covered_seq == current.injected_seq == selected.seq
        assert current.input_id == second.input_id
        assert (
            current.owner_admission_generation
            == store.session._connection.execute(
                f"SELECT sent_owner_admission_generation FROM {NativeRuntimeInput.declared_name} WHERE input_id=?",
                (second.input_id,),
            ).fetchone()[0]
        )


async def test_page_budget_refuses_progress_but_original_is_not_replayed(tmp_path, monkeypatch):
    root, root_id, comms, _first, _people = _root(tmp_path)
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    fake, calls = _fake_model(decision="IGNORE")
    monkeypatch.setattr(TrackedTurnSession, "execute", fake)
    comms.threads.register(
        Thread(
            "other",
            frozenset({"team"}),
            str(tmp_path),
            process_identity=ProcessIdentity.capture(os.getpid()),
        )
    )
    other = comms.registry.require("other")
    with Coordination(str(root / "coordination.sqlite3")) as store:
        store.participants.register(
            stable_thread_lookup(other.created_at), "other", "other", committed=True
        )
    for number in range(101):
        message = comms.messaging.send_initial_cohort("sender", "#team", f"@other note-{number}")
        with Coordination(str(root / "coordination.sqlite3")) as store:
            accept_initial_cohort(comms.bus, root_id, message.seq, store)
    # The dedicated cursor scan cannot cross the second bounded page. The
    # already committed original still produces its one fake native input.
    monkeypatch.setattr(SourceCoverage, "page_budget", 1)
    turn = await runtime.SelectedExecution(
        root=root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path
    ).run()
    assert turn is not None and turn.cursor_status == "unavailable" and len(calls) == 1
    with Coordination(str(root / "coordination.sqlite3")) as store:
        assert (
            NativeSourceCursor(comms.bus, store, wire_root_id=root_id).read(owner_name="alpha")
            is None
        )


async def test_unknown_first_source_cannot_be_bridged_by_101_unrelated(tmp_path, monkeypatch):
    root, root_id, comms, _first, _people = _root(tmp_path)
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    bad, _ = _fake_model(decision="IGNORE", digest_override="b" * 64)
    monkeypatch.setattr(TrackedTurnSession, "execute", bad)
    with pytest.raises(IdentityConflict, match="exact bound source prompt equality"):
        await runtime.SelectedExecution(
            root=root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path
        ).run()
    comms.threads.register(
        Thread(
            "other",
            frozenset(),
            str(tmp_path),
            process_identity=ProcessIdentity.capture(os.getpid()),
        )
    )
    for number in range(101):
        comms.messaging.send_initial_cohort("sender", "other", f"unrelated-{number}")
    later = comms.messaging.send_initial_cohort("sender", "alpha", "new exact selected work")
    with Coordination(str(root / "coordination.sqlite3")) as store:
        accept_initial_cohort(comms.bus, root_id, later.seq, store)
    good, calls = _fake_model(decision="IGNORE")
    monkeypatch.setattr(TrackedTurnSession, "execute", good)
    result = await runtime.SelectedExecution(
        root=root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path
    ).run()
    assert result is not None and result.cursor_status == "blocked_gap" and len(calls) == 1
    with Coordination(str(root / "coordination.sqlite3")) as store:
        assert (
            NativeSourceCursor(comms.bus, store, wire_root_id=root_id).read(owner_name="alpha")
            is None
        )


async def test_forged_cross_generation_cursor_reopen_denied_without_mutating_sql(
    tmp_path, monkeypatch
):
    root, root_id, comms, _first, people = _root(tmp_path)
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    fake, calls = _fake_model(decision="IGNORE")
    monkeypatch.setattr(TrackedTurnSession, "execute", fake)
    first = await runtime.SelectedExecution(
        root=root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path
    ).run()
    assert first is not None and first.cursor_status == "proven"
    # Fresh-open same-generation proof remains valid across a reconnect.
    with Coordination(str(root / "coordination.sqlite3")) as reopened:
        valid = NativeSourceCursor(comms.bus, reopened, wire_root_id=root_id).read(
            owner_name="alpha"
        )
        assert valid is not None and valid.input_id == first.input_id

    lookup = stable_thread_lookup(people[1].created_at)
    comms.registry.rename("alpha", "alpha-new")
    with Coordination(str(root / "coordination.sqlite3")) as store:
        store.participants.advance_generation(lookup, "alpha-new", expected_generation=1)
    second_message = comms.messaging.send_initial_cohort("sender", "alpha-new", "new selected")
    with Coordination(str(root / "coordination.sqlite3")) as store:
        accept_initial_cohort(comms.bus, root_id, second_message.seq, store)
    second = await runtime.SelectedExecution(
        root=root, wire_root_id=root_id, owner_name="alpha-new", native_package=tmp_path
    ).run()
    assert second is not None and second.cursor_status == "blocked_gap"
    # Forge current generation identity while borrowing the previous owner's
    # source coverage. Reopening must reject it without repairing or replaying it.
    from agent_comms.native_runtime_input import CurrentNativeCursor, NativeRuntimeInput

    with Coordination(str(root / "coordination.sqlite3")) as store:
        proof = NativeRuntimeInput.one(store.session._connection, input_id=second.input_id)
        assert proof is not None
        store.session._connection.execute(
            f"INSERT INTO {CurrentNativeCursor.declared_name} VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                root_id,
                lookup,
                "alpha-new",
                2,
                proof.sent_owner_admission_generation,
                second_message.seq,
                second_message.seq,
                second.input_id,
                proof.assignment_id,
                proof.stage,
                proof.session_id,
                proof.request_generation,
            ),
        )
    with Coordination(str(root / "coordination.sqlite3")) as reopened:
        before = reopened.session._connection.execute(
            f"SELECT * FROM {CurrentNativeCursor.declared_name} WHERE owner_generation=2"
        ).fetchone()
        assert before is not None
        with pytest.raises(IdentityConflict, match="borrows historical owner source proof"):
            NativeSourceCursor(comms.bus, reopened, wire_root_id=root_id).read(
                owner_name="alpha-new"
            )
        after = reopened.session._connection.execute(
            f"SELECT * FROM {CurrentNativeCursor.declared_name} WHERE owner_generation=2"
        ).fetchone()
        assert tuple(after) == tuple(before)  # No recovery mutation or replay.
    assert len(calls) == 2


async def test_reconnect_rechecks_sql_generation_after_proof_scan(tmp_path, monkeypatch):
    root, root_id, comms, _first, people = _root(tmp_path)
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    fake, calls = _fake_model(decision="IGNORE")
    monkeypatch.setattr(TrackedTurnSession, "execute", fake)
    result = await runtime.SelectedExecution(
        root=root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path
    ).run()
    assert result is not None and result.cursor_status == "proven"
    lookup = stable_thread_lookup(people[1].created_at)
    original = SourceCoverage.evidence

    def advance_generation(*args, **kwargs):
        evidence = original(*args, **kwargs)
        # Supported same-name owner-generation change after initial SQL read,
        # but before second proof snapshot. Registry remains same incarnation.
        args[0].store.participants.advance_generation(lookup, "alpha", expected_generation=1)
        return evidence

    monkeypatch.setattr(SourceCoverage, "evidence", advance_generation)
    with Coordination(str(root / "coordination.sqlite3")) as store:
        with pytest.raises(StaleFence, match="participant generation changed"):
            NativeSourceCursor(comms.bus, store, wire_root_id=root_id).read(owner_name="alpha")
        assert store.participants.get(lookup).participant_generation == 2
        retained = store.session._connection.execute(
            f"SELECT owner_generation,input_id FROM {CurrentNativeCursor.declared_name}"
        ).fetchall()
        assert [tuple(row) for row in retained] == [(1, result.input_id)]
    monkeypatch.setattr(SourceCoverage, "evidence", original)
    with Coordination(str(root / "coordination.sqlite3")) as reopened:
        assert (
            NativeSourceCursor(comms.bus, reopened, wire_root_id=root_id).read(owner_name="alpha")
            is None
        )
    assert len(calls) == 1  # No cursor read replays the old native input.


async def test_replaced_bus_between_coverage_and_commit_omits_cursor(tmp_path, monkeypatch):
    root, root_id, comms, _first, _people = _root(tmp_path)
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    fake, calls = _fake_model(decision="IGNORE")
    monkeypatch.setattr(TrackedTurnSession, "execute", fake)
    original = SourceCoverage.prefix

    def replace_source(*args, **kwargs):
        result = original(*args, **kwargs)
        replacement = root / "bus-replacement.jsonl"
        replacement.write_bytes(comms.bus.log.path.read_bytes())
        replacement.chmod(0o600)
        os.replace(replacement, comms.bus.log.path)
        return result

    monkeypatch.setattr(SourceCoverage, "prefix", replace_source)
    result = await runtime.SelectedExecution(
        root=root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path
    ).run()
    assert result is not None and result.cursor_status == "unavailable" and len(calls) == 1
    with Coordination(str(root / "coordination.sqlite3")) as store:
        assert (
            store.session._connection.execute(
                f"SELECT COUNT(*) FROM {CurrentNativeCursor.declared_name}"
            ).fetchone()[0]
            == 0
        )
