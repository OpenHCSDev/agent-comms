"""Fail-closed ACP input send while a native commit needs exact-ID reconciliation."""

from __future__ import annotations

import asyncio
import os

import pytest

from agent_comms.acp import CommsAgent
from agent_comms.compaction_journal import CompactionJournal
from agent_comms.compaction_send_admission import native_input_admitted
from agent_comms.input_disposition import InputDispositions
from agent_comms.operations import wire

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Durable POSIX compaction journal")


def test_saved_session_barrier_does_not_create_or_repair_journal(tmp_path):
    root = tmp_path / "wire"
    root.mkdir()
    session = root / "session.jsonl"
    session.write_text("{}\n")
    assert native_input_admitted(root, str(session))
    assert not (root / "compaction-commits.sqlite3").exists()
    journal = CompactionJournal(root / "compaction-commits.sqlite3")
    first = journal.begin(str(session), {"source": "pre-summary"})
    assert not native_input_admitted(root, str(session))
    journal.resolve(first, "unknown", {"status": "unknown", "reason": "lost reply"})
    assert not native_input_admitted(root, str(session))
    assert native_input_admitted(root, None)
    journal.resolve(first, "committed", {"status": "committed", "entryId": "entry"})
    assert native_input_admitted(root, str(session))
    assert not native_input_admitted(root, str(root / "missing.jsonl"))
    assert [row.commit_id for row in journal.pending_publications(str(session))] == []
    journal.path.unlink()
    journal.path.symlink_to(root / "missing-db.sqlite3")
    assert not native_input_admitted(root, str(session))


@pytest.mark.asyncio
async def test_acp_original_send_denied_before_input_bind_with_unresolved_commit(
    tmp_path, monkeypatch
):
    comms = wire(tmp_path / "wire")
    agent = CommsAgent(comms, agent_bin="pi", runtime_enabled=True)
    await agent.new_session(str(tmp_path / "project"))
    agent._drain_tasks["project"].cancel()
    await asyncio.gather(agent._drain_tasks["project"], return_exceptions=True)
    session = tmp_path / "saved.jsonl"
    session.write_text("{}\n")
    comms.attach_session("project", str(session), pid=os.getpid())
    journal = CompactionJournal(comms.root / "compaction-commits.sqlite3")
    commit_id = journal.begin(str(session), {"source": "pre-summary"})
    observed = []

    async def events(*args, **kwargs):
        with kwargs["send_boundary"](None, "a" * 32, args[2]) as allowed:
            observed.append(allowed)
        yield {"type": "done", "ok": False, "text": "No provider send"}

    monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", events)
    try:
        await agent._run_owned_input("project", "project", "new correction")
        assert observed == [False]
        rows = InputDispositions(comms.root).unknown(frozenset({"project"}))
        assert len(rows) == 1 and rows[0]["native_id"] is None
        assert journal.get(commit_id).status == "intent"
        journal.resolve(commit_id, "unknown", {"status": "unknown", "reason": "uncertain"})
        await agent._run_owned_input("project", "project", "distinct later input")
        assert observed == [False, False]
        assert journal.get(commit_id).status == "unknown"
    finally:
        await agent.shutdown()
