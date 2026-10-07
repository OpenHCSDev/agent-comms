"""The activity owner retains a real coverage refusal without changing custody."""

import json
import sqlite3
from dataclasses import replace

import pytest

from agent_comms.activity import DrainDiagnostic, StoppedDrainDiagnostic
from agent_comms.compaction_errors import CompactionJournalError
from agent_comms.field_codec import FieldCodec
from agent_comms.threads import Thread
from test_continued_private_session import continued  # noqa: F401


def test_coverage_refusal_keeps_chain_and_original_storage(comms, continued):  # noqa: F811
    journal, session, inputs, source = continued
    journal.private_inputs.reserve(session, "b" * 32)
    before = session.read_bytes(), inputs.path.read_bytes()
    comms.registry.declare(Thread(name="owner", tags=frozenset(), worktree=str(session.parent)))
    owner = comms.registry.snapshot().owner_identity("owner")

    # The original coverage owner refuses this authored fixture's missing
    # coordinator. No replacement coverage result or native process is supplied.
    with pytest.raises(CompactionJournalError) as failed:
        journal.summaries.reserve(str(session), source)
    error = failed.value
    assert isinstance(error.__cause__, FileNotFoundError)
    diagnostic = StoppedDrainDiagnostic(owner, type(error).__name__, str(error))
    assert comms.agents.set_drain_diagnostic(
        "owner", owner, diagnostic, source_error=error,
    )
    observed = comms.agents.activity_of("owner").readiness.source_diagnostic()
    path, = (comms.root / "diagnostics").glob("drain-*.json")
    assert observed.reason == f"{error} · Diagnostic: {path.resolve()}"
    document = json.loads(path.read_text())
    assert "FileNotFoundError" in document["source_error"]
    assert "verify_continued_private_session" in document["source_error"]
    assert "CompactionJournalError" in document["source_error"]
    assert str(journal.path.parent / "coordination.sqlite3") in document["source_error"]
    assert "source_error" not in FieldCodec.encode(observed)
    assert FieldCodec.decode(DrainDiagnostic, FieldCodec.encode(observed)) == observed
    assert path.stat().st_mode & 0o777 == 0o600

    # The same exception neither appends another activity nor creates another
    # diagnostic. A stale owner cannot publish a failure for its successor.
    activity_file = comms.root / "activity.jsonl"
    activity_before = activity_file.read_bytes()
    original_file = path.stat()
    assert not comms.agents.set_drain_diagnostic(
        "owner", owner, diagnostic, source_error=error,
    )
    assert activity_file.read_bytes() == activity_before
    assert len(list(path.parent.glob("drain-*.json"))) == 1
    assert path.stat() == original_file
    assert not comms.agents.set_drain_diagnostic(
        "owner", owner, observed, source_error=error,
    )
    assert len(list(path.parent.glob("drain-*.json"))) == 1
    stale = replace(owner, generation=owner.generation + 1)
    assert not comms.agents.set_drain_diagnostic(
        "owner", stale, replace(diagnostic, owner=stale), source_error=error,
    )
    assert len(list(path.parent.glob("drain-*.json"))) == 1
    original_diagnostic = path.read_bytes()
    coordinator = journal.path.parent / "coordination.sqlite3"
    with sqlite3.connect(coordinator):
        pass
    coordinator.chmod(0o644)
    with pytest.raises(CompactionJournalError) as changed:
        journal.summaries.reserve(str(session), source)
    assert str(changed.value) == str(error)
    assert isinstance(changed.value.__cause__, ValueError)
    assert comms.agents.set_drain_diagnostic(
        "owner", owner, diagnostic, source_error=changed.value,
    )
    successor = comms.agents.activity_of("owner").readiness.source_diagnostic()
    successor_path, = set(path.parent.glob("drain-*.json")) - {path}
    assert successor.reason == f"{changed.value} · Diagnostic: {successor_path.resolve()}"
    assert path.read_bytes() == original_diagnostic
    assert "ValueError" in successor_path.read_text()
    assert before == (session.read_bytes(), inputs.path.read_bytes())
    with journal.transaction() as db:
        assert db.execute("SELECT count(*) FROM selected_summary_attempts").fetchone()[0] == 0


def test_legacy_drain_diagnostic_decodes_without_private_reference(comms):
    comms.registry.declare(Thread(name="owner", tags=frozenset(), worktree=str(comms.root)))
    owner = comms.registry.snapshot().owner_identity("owner")
    legacy = FieldCodec.encode(StoppedDrainDiagnostic(owner, "CompactionJournalError", "refused"))
    assert set(legacy) == {"kind", "owner", "error_type", "reason"}
    decoded = FieldCodec.decode(DrainDiagnostic, legacy)
    assert decoded == StoppedDrainDiagnostic(owner, "CompactionJournalError", "refused")
